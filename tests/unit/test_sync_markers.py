"""Content-based loop protection for comments.

The identity gates are the primary defence, but they depend on
configuration. Comments need a second line because, unlike field writes,
each echo *rewrites* the text -- so no duplicate check can catch it and the
comment grows without bound. This was observed live before the breakers
existed.
"""

import pytest

from jbi import Operation, steps
from jbi.jira import JiraService
from jbi.sync_markers import (
    was_written_by_forward_sync,
    was_written_by_reverse_sync,
)

# Captured verbatim from the live runaway.
HOP_1 = (
    "*jgauf@mozilla.com* commented: \nfrom Jira, by John Gauf: testing comment number 2"
)
HOP_2 = (
    "*jgauf@mozilla.com* commented: \nfrom Jira, by John Gauf: "
    "_[mailto:jgauf@mozilla.com]_ commented: from Jira, by John Gauf: "
    "testing comment number 2"
)


@pytest.mark.parametrize("body", [HOP_1, HOP_2])
def test_forward_written_comments_are_recognised(body):
    assert was_written_by_forward_sync(body) is True


def test_reverse_written_comments_are_recognised():
    assert was_written_by_reverse_sync("from Jira, by John Gauf:\nhello") is True


@pytest.mark.parametrize(
    "body",
    [
        "an ordinary human comment",
        "discussion of *emphasis* in the middle",
        "",
        None,
    ],
)
def test_human_comments_are_not_mistaken_for_sync_output(body):
    assert was_written_by_forward_sync(body) is False
    assert was_written_by_reverse_sync(body) is False


def test_forward_sync_skips_a_comment_it_imported_from_jira(
    action_context_factory, mocked_jira, capturelogs
):
    """Bugzilla side of the breaker."""
    import logging

    context = action_context_factory(
        operation=Operation.COMMENT,
        bug__with_comment=True,
        bug__comment__body="from Jira, by John Gauf:\ntesting comment number 2",
        bug__comment__id=7,
        bug__comment__is_private=False,
        event__target="comment",
        jira__issue="JBI-234",
        current_step="create_comment",
    )

    with capturelogs.for_logger("jbi.steps").at_level(logging.INFO):
        result, _ = steps.create_comment(context, jira_service=JiraService(mocked_jira))

    assert result == steps.StepStatus.NOOP
    assert not mocked_jira.issue_add_comment.called
    assert any("reverse sync" in r.message for r in capturelogs.records)
