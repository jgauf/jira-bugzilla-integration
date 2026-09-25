"""Recognise text JBI itself wrote, in either system.

The identity gates (Invariant C) are the primary loop protection, but they
depend on configuration: if `bugzilla_bot_login` or `jira_bot_account_id` is
unset or wrong, JBI's own writes come back at it. For *fields* that is
harmless -- read-before-write makes the second write a no-op. For *comments*
it is not, because each hop rewraps the text in another attribution layer:

    testing comment
    -> *someone* commented: from Jira, by Someone: testing comment
    -> from Jira, by Someone: *someone* commented: from Jira, by Someone: ...

The text differs every time, so no duplicate check can catch it, and the
comment grows without bound. Observed live.

So comments carry a marker and each direction refuses to re-import the
other's. This is defence in depth, independent of identity config.
"""

import re
from typing import Optional

# What the reverse direction writes onto a Bugzilla bug.
REVERSE_COMMENT_PREFIX = "from Jira, by "

# What the forward direction writes onto a Jira issue
# (`JiraService.add_jira_comment`: "*<login>* commented: \n<body>").
FORWARD_COMMENT_RE = re.compile(r"^\*[^*]+\*\s+commented:", re.MULTILINE)


def was_written_by_reverse_sync(body: Optional[str]) -> bool:
    """True when this Bugzilla comment was copied from Jira by JBI."""
    if not body:
        return False
    return body.lstrip().startswith(REVERSE_COMMENT_PREFIX)


def was_written_by_forward_sync(body: Optional[str]) -> bool:
    """True when this Jira comment was copied from Bugzilla by JBI."""
    if not body:
        return False
    return bool(FORWARD_COMMENT_RE.match(body.lstrip()))
