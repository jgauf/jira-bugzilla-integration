"""Tests for the reverse (Jira -> BMO) field writers, plan D9.

The interesting cases are the ones section 4.1 of the plan argues about: that
`status_map` is never inverted, that a resolution is never guessed, and that
a value which does not round-trip cannot corrupt the bug.
"""

from unittest import mock

import pytest

from jbi import jira_steps
from jbi.jira_inbound.models import JiraNamedValue
from jbi.identity import UNASSIGNED_EMAIL, IdentityEntry, IdentityMap
from jbi.jira_steps import ReverseContext, ReverseStepStatus


@pytest.fixture
def mocked_service(mocked_bugzilla):
    from jbi import bugzilla

    return bugzilla.service.BugzillaService(mocked_bugzilla)


def make_context(
    action_factory,
    bug_factory,
    jira_webhook_request_factory,
    *,
    bug_kwargs=None,
    action_kwargs=None,
    **event_kwargs,
):
    action = action_factory(
        whiteboard_tag="devtest",
        parameters__jira_project_key="JBI",
        parameters__jira_inbound_enabled=True,
        **(action_kwargs or {}),
    )
    return ReverseContext(
        action=action,
        bug=bug_factory(**(bug_kwargs or {})),
        issue_key="JBI-234",
        event=jira_webhook_request_factory(**event_kwargs),
    )


# --- Status & resolution (plan section 4.1) --------------------------------


@pytest.mark.parametrize(
    "category,bug_status,expected",
    [
        # The three built-in categories, which every project's workflow uses
        # regardless of how its statuses are named.
        ("new", "ASSIGNED", "NEW"),
        ("indeterminate", "NEW", "ASSIGNED"),
        ("done", "ASSIGNED", "RESOLVED"),
        # A bug coming back out of a resolved state is REOPENED, not NEW:
        # writing NEW would erase the fact that it was ever closed.
        ("new", "RESOLVED", "REOPENED"),
        ("new", "VERIFIED", "REOPENED"),
    ],
)
def test_status_category_drives_the_reverse_status(
    action_factory,
    bug_factory,
    jira_webhook_request_factory,
    mocked_service,
    mocked_bugzilla,
    category,
    bug_status,
    expected,
):
    context = make_context(
        action_factory,
        bug_factory,
        jira_webhook_request_factory,
        bug_kwargs={"status": bug_status, "resolution": ""},
        issue__fields__status__statusCategory__key=category,
    )

    status, _ = jira_steps.writeback_status(context, bugzilla_service=mocked_service)

    assert status == ReverseStepStatus.SUCCESS
    written = mocked_bugzilla.update_bug.call_args.kwargs
    assert written["status"] == expected


def test_reverse_status_is_not_derived_from_status_map(
    action_factory,
    bug_factory,
    jira_webhook_request_factory,
    mocked_service,
    mocked_bugzilla,
):
    """The review finding this design answers: a many-to-one `status_map`
    cannot be inverted, so it must not participate in the reverse direction."""
    context = make_context(
        action_factory,
        bug_factory,
        jira_webhook_request_factory,
        # A realistic prod-shaped map: ten BMO values -> one Jira status.
        action_kwargs={
            "parameters__status_map": {
                "RESOLVED": "Done",
                "VERIFIED": "Done",
                "FIXED": "Done",
                "WONTFIX": "Done",
                "DUPLICATE": "Done",
            }
        },
        bug_kwargs={"status": "ASSIGNED", "resolution": ""},
        issue__fields__status__name="Done",
        issue__fields__status__statusCategory__key="done",
    )

    jira_steps.writeback_status(context, bugzilla_service=mocked_service)

    # RESOLVED comes from the status *category*, not from reversing the map
    # (which could equally have produced WONTFIX or DUPLICATE).
    assert mocked_bugzilla.update_bug.call_args.kwargs["status"] == "RESOLVED"


def test_resolution_comes_from_the_inverted_resolution_map(
    action_factory,
    bug_factory,
    jira_webhook_request_factory,
    mocked_service,
    mocked_bugzilla,
):
    context = make_context(
        action_factory,
        bug_factory,
        jira_webhook_request_factory,
        action_kwargs={
            "parameters__resolution_map": {
                "FIXED": "Done",
                "WONTFIX": "Won't Do",
                "DUPLICATE": "Duplicate",
            }
        },
        bug_kwargs={"status": "ASSIGNED", "resolution": ""},
        issue__fields__status__statusCategory__key="done",
        issue__fields__resolution=JiraNamedValue(name="Won't Do"),
    )

    jira_steps.writeback_status(context, bugzilla_service=mocked_service)

    written = mocked_bugzilla.update_bug.call_args.kwargs
    assert written == {"status": "RESOLVED", "resolution": "WONTFIX"}


def test_default_resolution_is_used_when_jira_has_none(
    action_factory,
    bug_factory,
    jira_webhook_request_factory,
    mocked_service,
    mocked_bugzilla,
):
    context = make_context(
        action_factory,
        bug_factory,
        jira_webhook_request_factory,
        action_kwargs={"parameters__default_reverse_resolution": "FIXED"},
        bug_kwargs={"status": "ASSIGNED", "resolution": ""},
        issue__fields__status__statusCategory__key="done",
        issue__fields__resolution=None,
    )

    jira_steps.writeback_status(context, bugzilla_service=mocked_service)

    assert mocked_bugzilla.update_bug.call_args.kwargs["resolution"] == "FIXED"


def test_resolution_is_left_alone_when_it_cannot_be_determined(
    action_factory,
    bug_factory,
    jira_webhook_request_factory,
    mocked_service,
    mocked_bugzilla,
    capturelogs,
):
    """Never guess a resolution: a wrong DUPLICATE or WONTFIX is a false claim
    about the bug that misleads every later reader."""
    import logging

    context = make_context(
        action_factory,
        bug_factory,
        jira_webhook_request_factory,
        bug_kwargs={"status": "ASSIGNED", "resolution": ""},
        issue__fields__status__statusCategory__key="done",
        issue__fields__resolution=None,
    )

    with capturelogs.for_logger("jbi.jira_steps").at_level(logging.WARNING):
        jira_steps.writeback_status(context, bugzilla_service=mocked_service)

    written = mocked_bugzilla.update_bug.call_args.kwargs
    assert written == {"status": "RESOLVED"}
    assert any("no BMO resolution" in r.message for r in capturelogs.records)


def test_reopening_clears_a_stale_resolution(
    action_factory,
    bug_factory,
    jira_webhook_request_factory,
    mocked_service,
    mocked_bugzilla,
):
    """An open bug that still claims to be FIXED is worse than either state."""
    context = make_context(
        action_factory,
        bug_factory,
        jira_webhook_request_factory,
        bug_kwargs={"status": "RESOLVED", "resolution": "FIXED"},
        issue__fields__status__statusCategory__key="indeterminate",
    )

    jira_steps.writeback_status(context, bugzilla_service=mocked_service)

    written = mocked_bugzilla.update_bug.call_args.kwargs
    assert written == {"status": "ASSIGNED", "resolution": ""}


def test_status_override_wins_over_the_default_category_map(
    action_factory,
    bug_factory,
    jira_webhook_request_factory,
    mocked_service,
    mocked_bugzilla,
):
    context = make_context(
        action_factory,
        bug_factory,
        jira_webhook_request_factory,
        action_kwargs={
            "parameters__reverse_status_overrides": {"indeterminate": "NEW"}
        },
        bug_kwargs={"status": "ASSIGNED"},
        issue__fields__status__statusCategory__key="indeterminate",
    )

    jira_steps.writeback_status(context, bugzilla_service=mocked_service)

    assert mocked_bugzilla.update_bug.call_args.kwargs["status"] == "NEW"


def test_status_writeback_is_skipped_when_status_did_not_change(
    action_factory,
    bug_factory,
    jira_webhook_request_factory,
    mocked_service,
    mocked_bugzilla,
    jira_changelog_item_factory,
):
    """A Jira edit of one field must not overwrite BMO fields a human just
    changed by hand."""
    context = make_context(
        action_factory,
        bug_factory,
        jira_webhook_request_factory,
        changelog__items=[jira_changelog_item_factory(field="description")],
    )

    status, _ = jira_steps.writeback_status(context, bugzilla_service=mocked_service)

    assert status == ReverseStepStatus.NOOP
    assert not mocked_bugzilla.update_bug.called


def test_unchanged_status_issues_no_request(
    action_factory,
    bug_factory,
    jira_webhook_request_factory,
    mocked_service,
    mocked_bugzilla,
):
    """Invariant C backstop: an echoed value terminates instead of looping."""
    context = make_context(
        action_factory,
        bug_factory,
        jira_webhook_request_factory,
        bug_kwargs={"status": "ASSIGNED", "resolution": ""},
        issue__fields__status__statusCategory__key="indeterminate",
    )

    status, _ = jira_steps.writeback_status(context, bugzilla_service=mocked_service)

    assert status == ReverseStepStatus.NOOP
    assert not mocked_bugzilla.update_bug.called


# --- Priority ---------------------------------------------------------------


def test_priority_is_written_through_the_inverted_priority_map(
    action_factory,
    bug_factory,
    jira_webhook_request_factory,
    mocked_service,
    mocked_bugzilla,
    jira_changelog_item_factory,
):
    context = make_context(
        action_factory,
        bug_factory,
        jira_webhook_request_factory,
        bug_kwargs={"priority": "P3"},
        changelog__items=[jira_changelog_item_factory(field="priority")],
        issue__fields__priority=JiraNamedValue(name="P1"),
    )

    status, _ = jira_steps.writeback_priority(context, bugzilla_service=mocked_service)

    assert status == ReverseStepStatus.SUCCESS
    mocked_bugzilla.update_bug.assert_called_once_with(context.bug.id, priority="P1")


def test_unmapped_priority_writes_nothing(
    action_factory,
    bug_factory,
    jira_webhook_request_factory,
    mocked_service,
    mocked_bugzilla,
    jira_changelog_item_factory,
):
    context = make_context(
        action_factory,
        bug_factory,
        jira_webhook_request_factory,
        changelog__items=[jira_changelog_item_factory(field="priority")],
        issue__fields__priority=JiraNamedValue(name="Blocker"),
    )

    status, _ = jira_steps.writeback_priority(context, bugzilla_service=mocked_service)

    assert status == ReverseStepStatus.INCOMPLETE
    assert not mocked_bugzilla.update_bug.called


# --- Assignee ---------------------------------------------------------------


def test_assignee_resolves_through_the_identity_map(
    action_factory,
    bug_factory,
    jira_webhook_request_factory,
    mocked_service,
    mocked_bugzilla,
    jira_changelog_item_factory,
    jira_user_factory,
):
    identity_map = IdentityMap(
        users=[
            IdentityEntry(
                bmo_email="mismatch@mozilla.com", jira_account_id="account-id-x"
            )
        ]
    )
    context = make_context(
        action_factory,
        bug_factory,
        jira_webhook_request_factory,
        changelog__items=[jira_changelog_item_factory(field="assignee")],
        issue__fields__assignee=jira_user_factory(
            accountId="account-id-x", emailAddress=None
        ),
    )

    with mock.patch("jbi.jira_steps.get_identity_map", return_value=identity_map):
        status, _ = jira_steps.writeback_assignee(
            context, bugzilla_service=mocked_service
        )

    assert status == ReverseStepStatus.SUCCESS
    mocked_bugzilla.update_bug.assert_called_once_with(
        context.bug.id, assigned_to="mismatch@mozilla.com"
    )


def test_assignee_falls_back_to_the_jira_email(
    action_factory,
    bug_factory,
    jira_webhook_request_factory,
    mocked_service,
    mocked_bugzilla,
    jira_changelog_item_factory,
    jira_user_factory,
):
    context = make_context(
        action_factory,
        bug_factory,
        jira_webhook_request_factory,
        changelog__items=[jira_changelog_item_factory(field="assignee")],
        issue__fields__assignee=jira_user_factory(
            accountId="account-id-y", emailAddress="person@mozilla.com"
        ),
    )

    with mock.patch("jbi.jira_steps.get_identity_map", return_value=IdentityMap()):
        jira_steps.writeback_assignee(context, bugzilla_service=mocked_service)

    mocked_bugzilla.update_bug.assert_called_once_with(
        context.bug.id, assigned_to="person@mozilla.com"
    )


def test_unresolvable_assignee_leaves_the_bug_alone(
    action_factory,
    bug_factory,
    jira_webhook_request_factory,
    mocked_service,
    mocked_bugzilla,
    jira_changelog_item_factory,
    jira_user_factory,
):
    """Hidden email and no override: never guess who this is."""
    context = make_context(
        action_factory,
        bug_factory,
        jira_webhook_request_factory,
        changelog__items=[jira_changelog_item_factory(field="assignee")],
        issue__fields__assignee=jira_user_factory(
            accountId="account-id-z", emailAddress=None
        ),
    )

    with mock.patch("jbi.jira_steps.get_identity_map", return_value=IdentityMap()):
        status, _ = jira_steps.writeback_assignee(
            context, bugzilla_service=mocked_service
        )

    assert status == ReverseStepStatus.INCOMPLETE
    assert not mocked_bugzilla.update_bug.called


def test_unassigning_in_jira_uses_the_bmo_sentinel(
    action_factory,
    bug_factory,
    jira_webhook_request_factory,
    mocked_service,
    mocked_bugzilla,
    jira_changelog_item_factory,
):
    context = make_context(
        action_factory,
        bug_factory,
        jira_webhook_request_factory,
        bug_kwargs={"assigned_to": "person@mozilla.com"},
        changelog__items=[jira_changelog_item_factory(field="assignee")],
        issue__fields__assignee=None,
    )

    jira_steps.writeback_assignee(context, bugzilla_service=mocked_service)

    mocked_bugzilla.update_bug.assert_called_once_with(
        context.bug.id, assigned_to=UNASSIGNED_EMAIL
    )


# --- Summary ----------------------------------------------------------------


def test_summary_is_written_back(
    action_factory,
    bug_factory,
    jira_webhook_request_factory,
    mocked_service,
    mocked_bugzilla,
    jira_changelog_item_factory,
):
    context = make_context(
        action_factory,
        bug_factory,
        jira_webhook_request_factory,
        bug_kwargs={"summary": "Old title"},
        changelog__items=[jira_changelog_item_factory(field="summary")],
        issue__fields__summary="New title",
    )

    status, _ = jira_steps.writeback_summary(context, bugzilla_service=mocked_service)

    assert status == ReverseStepStatus.SUCCESS
    mocked_bugzilla.update_bug.assert_called_once_with(
        context.bug.id, summary="New title"
    )


def test_summary_writeback_skipped_when_unchanged_in_jira(
    action_factory,
    bug_factory,
    jira_webhook_request_factory,
    mocked_service,
    mocked_bugzilla,
    jira_changelog_item_factory,
):
    context = make_context(
        action_factory,
        bug_factory,
        jira_webhook_request_factory,
        changelog__items=[jira_changelog_item_factory(field="status")],
        issue__fields__summary="New title",
    )

    status, _ = jira_steps.writeback_summary(context, bugzilla_service=mocked_service)

    assert status == ReverseStepStatus.NOOP
    assert not mocked_bugzilla.update_bug.called
