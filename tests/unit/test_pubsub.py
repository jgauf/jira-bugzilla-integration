"""Tests for Pub/Sub push delivery."""

import base64
import json

import pytest

from jbi.ingest import EventSource, reset_delivery_cache
from jbi.pubsub import (
    PubSubMessage,
    PubSubPushRequest,
    UndecodableMessage,
    build_inbound_event,
    decode_message_data,
    detect_source,
)


@pytest.fixture(autouse=True)
def clean_delivery_cache():
    reset_delivery_cache()
    yield
    reset_delivery_cache()


def envelope(payload, *, attributes=None, message_id="m-1", attempt=1):
    return PubSubPushRequest(
        message=PubSubMessage(
            data=base64.b64encode(json.dumps(payload).encode()).decode(),
            messageId=message_id,
            attributes=attributes or {},
            deliveryAttempt=attempt,
        ),
        subscription="projects/p/subscriptions/s",
    )


def bugzilla_payload(bug_id=654321):
    return {
        "webhook_id": 1,
        "webhook_name": "pubsub",
        "event": {"action": "create", "time": "2026-09-16T00:00:00Z", "target": "bug"},
        "bug": {"id": bug_id, "whiteboard": "[devtest]"},
    }


def jira_payload():
    return {
        "webhookEvent": "jira:issue_updated",
        "issue": {"key": "JBI-234", "fields": {"project": {"key": "JBI"}}},
    }


# --- Decoding ---------------------------------------------------------------


def test_data_is_base64_decoded():
    push = envelope({"hello": "world"})

    assert decode_message_data(push.message) == {"hello": "world"}


@pytest.mark.parametrize(
    "data,expected",
    [
        (None, "no data"),
        ("!!!not base64!!!", "base64"),
        (base64.b64encode(b"not json").decode(), "JSON"),
        (base64.b64encode(b'"a string"').decode(), "JSON object"),
    ],
)
def test_undecodable_data_is_permanent(data, expected):
    """Garbage cannot be fixed by redelivery, so it is distinguished from an
    outage and acknowledged rather than retried."""
    message = PubSubMessage(data=data, messageId="m-1")

    with pytest.raises(UndecodableMessage) as exc_info:
        decode_message_data(message)

    assert expected in str(exc_info.value)


# --- Source detection -------------------------------------------------------


def test_source_attribute_is_authoritative():
    assert detect_source({}, {"source": "bugzilla"}) == EventSource.BUGZILLA
    assert detect_source({}, {"source": "JIRA"}) == EventSource.JIRA


def test_unknown_source_attribute_is_rejected():
    """Better to drop loudly than to guess between two systems that both
    accept writes."""
    with pytest.raises(UndecodableMessage):
        detect_source(bugzilla_payload(), {"source": "github"})


def test_shape_sniffing_when_no_attribute():
    assert detect_source(bugzilla_payload(), {}) == EventSource.BUGZILLA
    assert detect_source(jira_payload(), {}) == EventSource.JIRA


def test_unidentifiable_payload_is_rejected():
    with pytest.raises(UndecodableMessage) as exc_info:
        detect_source({"something": "else"}, {})

    assert "source" in str(exc_info.value)


# --- Envelope building ------------------------------------------------------


def test_envelope_carries_broker_metadata():
    push = envelope(bugzilla_payload(), message_id="msg-9", attempt=3)

    event = build_inbound_event(push)

    assert event.source == EventSource.BUGZILLA
    assert event.message_id == "msg-9"
    assert event.delivery_attempt == 3


def test_payload_not_matching_the_schema_is_permanent():
    push = envelope(
        {"bug": {"no_id": True}, "event": {}}, attributes={"source": "bugzilla"}
    )

    with pytest.raises(UndecodableMessage) as exc_info:
        build_inbound_event(push)

    assert "schema" in str(exc_info.value)


# --- The endpoint -----------------------------------------------------------


def post_push(client, payload, *, attributes=None, message_id="m-1", token=None):
    body = envelope(payload, attributes=attributes, message_id=message_id)
    url = "/pubsub_push" + (f"?token={token}" if token else "")
    return client.post(
        url,
        content=body.model_dump_json(),
        headers={"Content-Type": "application/json"},
    )


def test_push_requires_authentication(anon_client):
    response = post_push(anon_client, jira_payload())

    assert response.status_code == 401


def test_push_accepts_a_url_token(anon_client, test_api_key, mocked_jira):
    """A push subscription cannot set request headers, so the shared secret
    has to be acceptable in the query string."""
    mocked_jira.get_issue_remote_links.return_value = []

    response = post_push(anon_client, jira_payload(), token=test_api_key)

    assert response.status_code == 200


def test_ignored_event_acknowledges(authenticated_client, mocked_jira):
    """200 is how a push subscription is told the message is finished with.
    An error here would redeliver an out-of-scope event until it expired."""
    mocked_jira.get_issue_remote_links.return_value = []

    response = post_push(authenticated_client, jira_payload())

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"


def test_undecodable_message_acknowledges(authenticated_client):
    body = PubSubPushRequest(message=PubSubMessage(data="!!!", messageId="m-bad"))

    response = authenticated_client.post(
        "/pubsub_push",
        content=body.model_dump_json(),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "dropped"


def test_transient_failure_asks_for_redelivery(authenticated_client, mocked_jira):
    mocked_jira.get_issue_remote_links.side_effect = RuntimeError("jira down")

    response = post_push(authenticated_client, jira_payload())

    assert response.status_code == 503


def test_duplicate_delivery_is_acknowledged_once(authenticated_client, mocked_jira):
    mocked_jira.get_issue_remote_links.return_value = []

    first = post_push(authenticated_client, jira_payload(), message_id="dupe-1")
    second = post_push(authenticated_client, jira_payload(), message_id="dupe-1")

    assert first.status_code == second.status_code == 200
    assert second.json()["reason"] == "duplicate delivery"


def test_bugzilla_event_arrives_through_pubsub(
    authenticated_client, mocked_bugzilla, mocked_jira, bug_factory
):
    """Both sources share one transport, so a Bugzilla event must reach the
    forward pipeline through the broker just as it does over the webhook."""
    mocked_bugzilla.get_bug.return_value = bug_factory(
        id=654321, whiteboard="[devtest]", see_also=[]
    )

    response = post_push(
        authenticated_client, bugzilla_payload(), attributes={"source": "bugzilla"}
    )

    assert response.status_code == 200
    assert response.json()["status"] == "handled"
    assert mocked_jira.create_issue.called
