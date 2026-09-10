"""Decide whether JBI may write Jira content back onto a Bugzilla bug (R-12).

Comments are the risky direction: Jira issues live in an internal planning
tool, Bugzilla bugs are frequently world-readable, and a comment copied from
one to the other cannot be un-published. The rule is therefore conservative --
JBI writes comments only onto bugs whose audience it is sure of, and stays
out of restricted ones entirely.
"""

import logging

from jbi.bugzilla.models import Bug

logger = logging.getLogger(__name__)


def is_bug_restricted(bug: Bug) -> bool:
    """Return True when the bug is private or limited to Bugzilla groups.

    `groups` is BMO's mechanism for confidential bugs (security, employee-only,
    embargoed). A non-empty list means the bug has a restricted audience.
    """
    if bug.is_private:
        return True
    return bool(bug.groups)


def can_write_comment(bug: Bug) -> bool:
    """Return True when a Jira comment may be copied onto this bug.

    Restricted bugs are skipped rather than handled: JBI has no way to
    reproduce a bug's group restrictions on a comment, and getting that wrong
    in either direction is worse than not syncing the comment at all. The
    skipped comment is logged so it can be surfaced by the reconciliation
    report (R-13).
    """
    if is_bug_restricted(bug):
        logger.info(
            "Bug %s is restricted; not writing Jira content back to it",
            bug.id,
            extra={"bug": {"id": bug.id}},
        )
        return False
    return True
