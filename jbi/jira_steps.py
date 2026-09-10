"""Reverse step functions (Jira -> BMO) and the executor that runs them.

This is the mirror image of `jbi/steps.py`. The asymmetry is deliberate and is
Invariant B of the plan: the forward direction may CREATE or UPDATE a Jira
issue, while the reverse direction is UPDATE-only and can never create a
Bugzilla bug.

Unlike forward steps, reverse steps are not listed per action in config. They
are a fixed pipeline, and each step self-gates on the action's parameters, so
that enabling the inbound path cannot accidentally enable a write the action
did not configure.
"""

from __future__ import annotations

import logging
from enum import Enum, auto
from typing import TYPE_CHECKING, Callable, Optional

from jbi.bugzilla.models import Bug
from jbi.jira_inbound.models import JiraWebhookRequest
from jbi.models import Action, Context

if TYPE_CHECKING:
    from jbi.bugzilla.service import BugzillaService

logger = logging.getLogger(__name__)


class ReverseStepStatus(Enum):
    """Result of executing a reverse step.

    SUCCESS: the step wrote to BMO.
    NOOP: nothing to do (not configured, field unchanged, value already equal).
    INCOMPLETE: an anticipated failure, eg. an unresolvable identity.
    """

    SUCCESS = auto()
    NOOP = auto()
    INCOMPLETE = auto()


class ReverseContext(Context, extra="forbid"):
    """Logging context for the Jira -> BMO direction."""

    action: Action
    bug: Bug
    issue_key: str
    event: JiraWebhookRequest
    current_step: Optional[str] = None
    responses: list = []

    def append_responses(self, *responses):
        """Return a copy with the given responses recorded."""
        return self.update(responses=[*self.responses, *responses])


ReverseStepResult = tuple[ReverseStepStatus, ReverseContext]
ReverseStep = Callable[..., ReverseStepResult]

# The reverse pipeline. Field writers are appended by later deliverables
# (D9/D10); D6 ships the spine with an empty pipeline so the endpoint can be
# merged, exercised and observed before it is able to write anything at all.
REVERSE_STEPS: list[ReverseStep] = []


class ReverseExecutor:
    """Runs the reverse steps for one action against one inbound event."""

    def __init__(self, bugzilla_service: Optional[BugzillaService] = None):
        if bugzilla_service is None:
            from jbi.bugzilla.service import get_service as get_bugzilla_service

            bugzilla_service = get_bugzilla_service()
        self.bugzilla_service = bugzilla_service

    def __call__(self, context: ReverseContext) -> dict:
        results: dict[str, str] = {}
        for step in REVERSE_STEPS:
            context = context.update(current_step=step.__name__)
            status, context = step(
                context=context, bugzilla_service=self.bugzilla_service
            )
            results[step.__name__] = status.name
            logger.info(
                "Reverse step %s -> %s for issue %s / Bug %s",
                step.__name__,
                status.name,
                context.issue_key,
                context.bug.id,
                extra=context.model_dump(),
            )
        return {"steps": results, "responses": context.responses}
