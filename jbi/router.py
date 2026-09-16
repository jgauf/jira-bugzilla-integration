"""
Core FastAPI app (setup, middleware)
"""

import logging
import secrets
from pathlib import Path
from typing import Annotated, Any, Optional

from dockerflow.logging import request_id_context
from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse
from fastapi.security import APIKeyHeader, HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from jbi import jira
from jbi.bugzilla import models as bugzilla_models
from jbi.bugzilla import service as bugzilla_service
from jbi.configuration import get_actions
from jbi.environment import Settings, get_settings
from jbi.ingest import EventSource, InboundEvent, IngestOutcome, ingest_event
from jbi.jira_inbound import models as jira_inbound_models
from jbi.models import Actions
from jbi.pubsub import PubSubPushRequest, UndecodableMessage, build_inbound_event
from jbi.queue import DeadLetterQueue, get_dl_queue

SettingsDep = Annotated[Settings, Depends(get_settings)]
ActionsDep = Annotated[Actions, Depends(get_actions)]
BugzillaServiceDep = Annotated[
    bugzilla_service.BugzillaService, Depends(bugzilla_service.get_service)
]
JiraServiceDep = Annotated[jira.JiraService, Depends(jira.get_service)]

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/", include_in_schema=False)
def root(request: Request, settings: SettingsDep):
    """Expose key configuration"""
    return {
        "title": request.app.title,
        "description": request.app.description,
        "version": request.app.version,
        "documentation": request.app.docs_url,
        "configuration": {
            "jira_base_url": settings.jira_base_url,
            "bugzilla_base_url": settings.bugzilla_base_url,
        },
    }


header_scheme = APIKeyHeader(name="X-Api-Key", auto_error=False)
basicauth_scheme = HTTPBasic(auto_error=False)


def api_key_auth(
    settings: SettingsDep,
    api_key: Annotated[str, Depends(header_scheme)],
    basic_auth: Annotated[HTTPBasicCredentials, Depends(basicauth_scheme)],
    token: Optional[str] = None,
):
    """Authenticate a request by header, basic auth, or `?token=`.

    The query-string form exists because some producers cannot set request
    headers at all -- a Bugzilla webhook takes only a URL, and a Pub/Sub push
    subscription signs requests rather than letting you add headers. It is
    the weakest of the three (a URL can reach access logs and browser
    history), so prefer the header wherever the producer supports it.
    """
    if not api_key and basic_auth:
        api_key = basic_auth.password
    if not api_key and token:
        api_key = token
    if not api_key or not secrets.compare_digest(api_key, settings.jbi_api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect API Key",
            headers={"WWW-Authenticate": "Basic"},
        )


@router.post(
    "/pubsub_push",
    # Same auth as every other endpoint; a push subscription cannot set
    # headers, so it uses the `?token=` form. Pub/Sub OIDC push auth is the
    # stronger production answer -- see ADR 005.
    dependencies=[Depends(api_key_auth)],
)
async def pubsub_push(
    actions: ActionsDep,
    push: PubSubPushRequest = Body(..., embed=False),
):
    """Receive one Pub/Sub push delivery.

    The HTTP status *is* the acknowledgement: 2xx acks the message, anything
    else asks the subscription to redeliver. So events JBI deliberately
    ignores, and payloads it can never process, both return 200 -- redelivery
    would fail identically and eventually expire.

    No dead-letter queue is passed: the subscription owns retries, and
    stacking JBI's queue on top would multiply redeliveries.
    """
    try:
        event = build_inbound_event(push, rid=request_id_context.get())
    except UndecodableMessage as exc:
        logger.error(
            "Dropping undecodable Pub/Sub message %s: %s",
            push.message.messageId,
            exc,
        )
        return {"status": "dropped", "reason": str(exc)}

    result = await ingest_event(event, actions)

    if result.outcome == IngestOutcome.RETRY:
        # 5xx is how a push subscription is told to redeliver.
        raise HTTPException(status_code=503, detail=result.reason)

    return {
        "status": str(result.outcome),
        "reason": result.reason,
        "message_id": push.message.messageId,
    }


@router.post(
    "/bugzilla_webhook",
    dependencies=[Depends(api_key_auth)],
)
async def bugzilla_webhook(
    request: Request,
    actions: ActionsDep,
    queue: Annotated[DeadLetterQueue, Depends(get_dl_queue)],
    webhook_request: bugzilla_models.WebhookRequest = Body(..., embed=False),
):
    """API endpoint that Bugzilla Webhook Events request"""
    result = await ingest_event(
        InboundEvent(
            source=EventSource.BUGZILLA,
            payload=webhook_request,
            rid=request_id_context.get(),
        ),
        actions,
        queue=queue,
    )
    # The response shape is preserved for compatibility with the existing
    # webhook contract; the outcome is what transports act on.
    return result.details or {"status": str(result.outcome), "error": result.reason}


@router.post(
    "/jira_webhook",
    dependencies=[Depends(api_key_auth)],
)
async def jira_webhook(
    actions: ActionsDep,
    jira_event: jira_inbound_models.JiraWebhookRequest = Body(..., embed=False),
):
    """API endpoint that the central Jira Automation rule posts to.

    Isolated from `/bugzilla_webhook` on purpose (plan constraint 6.2): the
    reverse direction cannot destabilize the forward one. Events JBI does not
    act on -- the overwhelming majority -- are reported as `ignored` rather
    than as errors, since Jira Automation forwards far more than JBI handles.
    """
    result = await ingest_event(
        InboundEvent(
            source=EventSource.JIRA,
            payload=jira_event,
            rid=request_id_context.get(),
        ),
        actions,
    )
    if result.outcome == IngestOutcome.IGNORED:
        logger.info("Ignore inbound Jira event: %s", result.reason)
        return {"status": "ignored", "reason": result.reason}
    if result.outcome == IngestOutcome.RETRY:
        raise HTTPException(status_code=500, detail=result.reason)
    return {"status": "handled", "details": result.details}


@router.get(
    "/dl_queue/",
    dependencies=[Depends(api_key_auth)],
)
async def inspect_dl_queue(queue: Annotated[DeadLetterQueue, Depends(get_dl_queue)]):
    """API for viewing queue content"""
    bugs = await queue.retrieve()
    results = []
    fields: dict[str, Any] = {
        "identifier": True,
        "rid": True,
        "error": True,
        "version": True,
        "payload": {
            "bug": {"id", "whiteboard", "product", "component"},
            "event": {"action", "time"},
        },
    }
    for items in bugs.values():
        async for item in items:
            results.append(item.model_dump(include=fields))
    return results


@router.delete("/dl_queue/{item_id}", dependencies=[Depends(api_key_auth)])
async def delete_queue_item_by_id(
    item_id: str, queue: Annotated[DeadLetterQueue, Depends(get_dl_queue)]
):
    item_exists = await queue.exists(item_id)
    if item_exists:
        await queue.delete(item_id)
    else:
        raise HTTPException(
            status_code=404, detail=f"Item {item_id} not found in queue"
        )


@router.get(
    "/whiteboard_tags/",
    dependencies=[Depends(api_key_auth)],
)
def get_whiteboard_tags(
    actions: ActionsDep,
    whiteboard_tag: Optional[str] = None,
):
    """API for viewing whiteboard_tags and associated data"""
    if existing := actions.get(whiteboard_tag):
        return {whiteboard_tag: existing}
    return actions.by_tag


@router.get(
    "/bugzilla_webhooks/",
    dependencies=[Depends(api_key_auth)],
)
def get_bugzilla_webhooks(bugzilla_service: BugzillaServiceDep):
    """API for viewing webhooks details"""
    return bugzilla_service.list_webhooks()


@router.get(
    "/jira_projects/",
    dependencies=[Depends(api_key_auth)],
)
def get_jira_projects(jira_service: JiraServiceDep):
    """API for viewing projects that are currently accessible by API"""
    return jira_service.fetch_visible_projects()


SRC_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=SRC_DIR / "templates")


@router.get(
    "/powered_by_jbi/",
    dependencies=[Depends(api_key_auth)],
    response_class=HTMLResponse,
)
def powered_by_jbi(
    request: Request,
    actions: ActionsDep,
    enabled: Optional[bool] = None,
):
    """API for `Powered By` endpoint"""
    context = {
        "request": request,
        "title": "Powered by JBI",
        "actions": jsonable_encoder(actions),
        "enable_query": enabled,
    }
    return templates.TemplateResponse("powered_by_template.html", context)
