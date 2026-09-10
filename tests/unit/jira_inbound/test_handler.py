"""Tests for the inbound Jira spine (plan D6).

D6 performs no writes: its whole job is to decide, safely, whether an inbound
event is one JBI may act on. These tests are therefore mostly about what does
*not* happen.
"""

from unittest import mock

import pytest

from jbi.errors import IgnoreInvalidRequestError
from jbi.jira_inbound.handler import execute_jira_event, is_bot_authored
from jbi.models import Actions


@pytest.fixture
def inbound_actions(action_factory):
    """A single action that has opted its project into inbound sync."""
    return Actions(
        root=[
            action_factory(
                whiteboard_tag="devtest",
                parameters__jira_project_key="JBI",
                parameters__jira_inbound_enabled=True,
            )
        ]
    )


@pytest.fixture
def linked_bug(bug_factory, settings):
    return bug_factory(
        id=654321,
        whiteboard="[devtest]",
        see_also=[f"{settings.jira_base_url}browse/JBI-234"],
    )


@pytest.fixture
def correlated(mocked_jira, mocked_bugzilla, linked_bug):
    """Wire the mocks so JBI-234 correlates to the linked bug."""
    mocked_jira.get_issue_remote_links.return_value = [
        {
            "globalId": "654321",
            "object": {"url": "http://bz.example/show_bug.cgi?id=654321"},
        }
    ]
    mocked_bugzilla.get_bug.return_value = linked_bug
    return mocked_jira, mocked_bugzilla


def test_correlated_and_opted_in_event_is_handled(
    correlated, jira_webhook_event, inbound_actions
):
    details = execute_jira_event(jira_webhook_event, inbound_actions)

    # D6 ships an empty reverse pipeline: the event is accepted but nothing
    # is written yet. The writers arrive in D9/D10.
    assert details == {"steps": {}, "responses": []}


def test_uncorrelated_issue_is_ignored(
    mocked_jira, mocked_bugzilla, jira_webhook_event, inbound_actions
):
    """Invariant B: an issue with no linked bug has no reverse effect at all,
    and in particular never causes a bug to be created."""
    mocked_jira.get_issue_remote_links.return_value = []

    with pytest.raises(IgnoreInvalidRequestError) as exc_info:
        execute_jira_event(jira_webhook_event, inbound_actions)

    assert "no Bugzilla bug linked" in str(exc_info.value)
    assert not mocked_bugzilla.update_bug.called
    assert not mocked_bugzilla.get_bug.called


def test_bot_authored_event_is_ignored(
    correlated, jira_webhook_request_factory, inbound_actions, settings
):
    """Invariant C, Jira side: JBI's own writes must not come back at it."""
    event = jira_webhook_request_factory(user__accountId="jbi-bot-account-id")
    _, mocked_bugzilla = correlated

    with mock.patch(
        "jbi.jira_inbound.handler.settings.jira_bot_account_id", "jbi-bot-account-id"
    ):
        with pytest.raises(IgnoreInvalidRequestError) as exc_info:
            execute_jira_event(event, inbound_actions)

    assert "authored by JBI itself" in str(exc_info.value)
    assert not mocked_bugzilla.update_bug.called


def test_human_authored_event_is_not_suppressed(
    correlated, jira_webhook_request_factory, inbound_actions
):
    event = jira_webhook_request_factory(user__accountId="a-real-person")

    with mock.patch(
        "jbi.jira_inbound.handler.settings.jira_bot_account_id", "jbi-bot-account-id"
    ):
        execute_jira_event(event, inbound_actions)


def test_no_suppression_when_bot_account_is_unconfigured(jira_webhook_event):
    """Unset `jira_bot_account_id` must not suppress anything: an empty
    setting matching an empty actor id would silently drop real events."""
    with mock.patch("jbi.jira_inbound.handler.settings.jira_bot_account_id", None):
        assert is_bot_authored(jira_webhook_event) is False


def test_event_without_issue_key_is_ignored(
    jira_webhook_request_factory, inbound_actions
):
    event = jira_webhook_request_factory(issue=None)

    with pytest.raises(IgnoreInvalidRequestError) as exc_info:
        execute_jira_event(event, inbound_actions)

    assert "no issue key" in str(exc_info.value)


def test_event_for_project_without_opt_in_is_ignored(
    correlated, jira_webhook_event, action_factory
):
    actions = Actions(
        root=[
            action_factory(
                whiteboard_tag="devtest",
                parameters__jira_project_key="JBI",
                parameters__jira_inbound_enabled=False,
            )
        ]
    )

    with pytest.raises(IgnoreInvalidRequestError) as exc_info:
        execute_jira_event(jira_webhook_event, actions)

    assert "jira_inbound_enabled" in str(exc_info.value)


def test_event_for_other_project_is_ignored(
    correlated, jira_webhook_event, action_factory
):
    """An opted-in action only governs its own Jira project."""
    actions = Actions(
        root=[
            action_factory(
                whiteboard_tag="devtest",
                parameters__jira_project_key="OTHER",
                parameters__jira_inbound_enabled=True,
            )
        ]
    )

    with pytest.raises(IgnoreInvalidRequestError):
        execute_jira_event(jira_webhook_event, actions)


def test_private_bug_is_ignored(
    mocked_jira, mocked_bugzilla, bug_factory, jira_webhook_event, inbound_actions
):
    mocked_jira.get_issue_remote_links.return_value = [{"globalId": "654321"}]
    mocked_bugzilla.get_bug.return_value = bug_factory(id=654321, is_private=True)

    with pytest.raises(IgnoreInvalidRequestError) as exc_info:
        execute_jira_event(jira_webhook_event, inbound_actions)

    assert "private" in str(exc_info.value)


def test_bug_linked_to_a_different_issue_is_ignored(
    mocked_jira,
    mocked_bugzilla,
    bug_factory,
    jira_webhook_event,
    inbound_actions,
    settings,
):
    """A one-sided link means the bug was re-pointed at another issue; writing
    the change would land it on the wrong bug."""
    mocked_jira.get_issue_remote_links.return_value = [{"globalId": "654321"}]
    mocked_bugzilla.get_bug.return_value = bug_factory(
        id=654321,
        whiteboard="[devtest]",
        see_also=[f"{settings.jira_base_url}browse/JBI-999"],
    )

    with pytest.raises(IgnoreInvalidRequestError) as exc_info:
        execute_jira_event(jira_webhook_event, inbound_actions)

    assert "JBI-999" in str(exc_info.value)


def test_bug_without_matching_whiteboard_tag_is_ignored(
    mocked_jira, mocked_bugzilla, bug_factory, jira_webhook_event, inbound_actions
):
    mocked_jira.get_issue_remote_links.return_value = [{"globalId": "654321"}]
    mocked_bugzilla.get_bug.return_value = bug_factory(
        id=654321, whiteboard="[unrelated]", see_also=[]
    )

    with pytest.raises(IgnoreInvalidRequestError):
        execute_jira_event(jira_webhook_event, inbound_actions)
