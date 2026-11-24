"""Example stint schema: workflows with restricted transitions.

Declares the workflow for a project whose permission scheme (see
``permission_scheme.py``) already locks editing down to the service role.
The workflow adds the other half of the story:

- Statuses are tenant-global and referenced by name across workflows.
- ``restrict_to`` on a transition narrows who may use it (project role,
  group, user, or permission). Unrestricted transitions need only the
  permission scheme's ``TRANSITION_ISSUES``.
- ``WorkflowScheme`` binds a default workflow; per-issuetype overrides go
  in ``mappings={issuetype_alias: workflow}``.
- Adoption is non-destructive: an existing workflow by the same name is
  recorded into state without rewriting it — undeclared transitions,
  validators, and postfunctions stay.
- Rebinding an existing project requires explicit ``status_mappings`` in
  the migration; stint never guesses a mapping, and the async switch task
  is polled to completion.

Run `stint validate --schema examples.company_managed.workflow` (no
network) to confirm internal consistency.
"""

from __future__ import annotations

from stint import (
    IssueType,
    Permission,
    Project,
    ProjectRole,
    Status,
    Transition,
    User,
    Workflow,
    WorkflowScheme,
)

# ── Statuses (tenant-global, referenced by name) ─────────────────────
open_ = Status("Open", category="new")
in_progress = Status("In Progress", category="indeterminate")
in_review = Status("In Review", category="indeterminate")
done = Status("Done", category="done")

# ── The workflow ─────────────────────────────────────────────────────
tracker_flow = Workflow(
    alias="tracker_flow",
    name="Tracker Workflow",
    description="Service-driven tracker flow with admin-only close/reopen.",
    statuses=[open_, in_progress, in_review, done],
    initial=open_,
    transitions=[
        # Anyone holding TRANSITION_ISSUES may start and review.
        Transition("Start", from_=[open_], to=in_progress),
        Transition("Review", from_=[in_progress], to=in_review),
        # Closing and reopening are restricted: the service role (project
        # role) or anyone holding ADMINISTER_PROJECTS may do it.
        Transition("Close", from_=[in_review], to=done, restrict_to=[ProjectRole("Administrators")]),
        Transition(
            "Reopen",
            from_=[done],
            to=open_,
            restrict_to=[ProjectRole("Administrators"), Permission("ADMINISTER_PROJECTS")],
        ),
    ],
)

# ── The scheme (default workflow + optional issuetype overrides) ────
tracker_wfs = WorkflowScheme(
    alias="tracker_wfs",
    name="Tracker Workflow Scheme",
    default=tracker_flow,
    # mappings={"bug": other_flow}  # per-issuetype overrides, alias-keyed
)


# ── The project ──────────────────────────────────────────────────────
class Bug(IssueType):
    __alias__ = "bug"


class Tracker(Project):
    __key__ = "TRK"
    __issuetypes__ = [Bug]
    __workflow_scheme__ = tracker_wfs
    __roles__ = {
        "Administrators": [User("svc-automation@example.com")],
    }
