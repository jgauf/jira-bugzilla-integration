"""Pub/Sub push delivery (`POST /pubsub_push`).

A push subscription POSTs each message to an HTTP endpoint, so JBI stays a
stateless web service with no long-running consumer: retries, backoff and
dead-lettering all become subscription configuration rather than code.

The envelope Pub/Sub sends looks like::

    {"message": {"data": "<base64 of the original payload>",
                 "messageId": "123", "publishTime": "...",
                 "attributes": {"source": "bugzilla"}},
     "subscription": "projects/p/subscriptions/s"}

**Acknowledgement is expressed as the HTTP status**: 2xx acks, anything else
redelivers. So an event JBI deliberately ignores must return 200 -- returning
an error for "out of scope" would have the subscription redeliver it until it
expired.

Identifying the source: the `source` attribute is authoritative when the
publisher sets it. Failing that the payload shape decides, because the two
payloads are structurally unmistakable -- a Bugzilla webhook has `bug` and
`event` keys, a Jira event has `issue` or `webhookEvent`.
"""

import base64
import binascii
import json
import logging
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, ValidationError

from jbi.bugzilla import models as bugzilla_models
from jbi.ingest import EventSource, InboundEvent
from jbi.jira_inbound.models import JiraWebhookRequest

logger = logging.getLogger(__name__)


class PubSubMessage(BaseModel):
    """The `message` object inside a push request."""

    model_config = ConfigDict(extra="ignore")

    data: Optional[str] = None
    messageId: Optional[str] = None
    publishTime: Optional[str] = None
    attributes: dict[str, str] = {}
    deliveryAttempt: Optional[int] = None


class PubSubPushRequest(BaseModel):
    """A Pub/Sub push delivery."""

    model_config = ConfigDict(extra="ignore")

    message: PubSubMessage
    subscription: Optional[str] = None


class UndecodableMessage(Exception):
    """The message body is not something JBI can ever process.

    Raised for garbage rather than for outages, so the caller can acknowledge
    instead of asking for a redelivery that would fail identically.
    """


def decode_message_data(message: PubSubMessage) -> dict[str, Any]:
    """Return the decoded JSON payload carried by a push message."""
    if not message.data:
        raise UndecodableMessage("message has no data")
    try:
        raw = base64.b64decode(message.data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise UndecodableMessage(f"data is not valid base64: {exc}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise UndecodableMessage(f"data is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise UndecodableMessage("data is not a JSON object")
    return payload


def detect_source(payload: dict[str, Any], attributes: dict[str, str]) -> EventSource:
    """Decide which system a decoded payload came from."""
    declared = (attributes.get("source") or "").strip().lower()
    if declared == EventSource.BUGZILLA:
        return EventSource.BUGZILLA
    if declared == EventSource.JIRA:
        return EventSource.JIRA
    if declared:
        raise UndecodableMessage(f"unknown source attribute {declared!r}")

    # Shape sniffing, in publisher-agnostic order of certainty.
    if "bug" in payload and "event" in payload:
        return EventSource.BUGZILLA
    if "issue" in payload or "webhookEvent" in payload:
        return EventSource.JIRA
    raise UndecodableMessage(
        "cannot tell whether this is a Bugzilla or Jira event; "
        "publish a `source` attribute"
    )


def build_inbound_event(
    push: PubSubPushRequest, rid: Optional[str] = None
) -> InboundEvent:
    """Turn a push delivery into the envelope the core ingests."""
    payload = decode_message_data(push.message)
    source = detect_source(payload, push.message.attributes)

    try:
        if source == EventSource.BUGZILLA:
            parsed: Any = bugzilla_models.WebhookRequest.model_validate(payload)
        else:
            parsed = JiraWebhookRequest.model_validate(payload)
    except ValidationError as exc:
        # Malformed beyond use: redelivery cannot fix a payload that does not
        # match the schema, so this is permanent, not transient.
        raise UndecodableMessage(f"payload does not match {source} schema: {exc}")

    return InboundEvent(
        source=source,
        payload=parsed,
        message_id=push.message.messageId,
        delivery_attempt=push.message.deliveryAttempt or 1,
        rid=rid,
    )
