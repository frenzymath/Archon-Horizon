"""Saved legacy automation profiles; new objective runs use planner/review demand.

This compatibility factory preserves persisted run behavior. Fresh orchestrated
launches are rejected by Scheduler.validate_launch_profile before reaching it.
Rendered instructions are stored on the automation/assignment when created;
editing a template cannot rewrite an existing owner's instructions.
"""

from ..instructions.templates import template


def automation_specs(run, phase, project_id, phase_repository_ids):
    if phase.get("orchestrated", False):
        # The model is a short-lived control episode.  It receives the
        # whole phase contract and current health snapshot, then queues
        # narrow child work itself.  Scheduler safety remains deterministic
        # and the episode is renewed only after its cooldown.
        return (
            ("orchestrator", "maintainer", ["orchestrator"],
             template("legacy/control-pass"),
             None, True),
            ("planner", "worker", ["planner"],
             template("legacy/planning-pass"),
             {"version": 1, "expression": {"op": "planning_needed", "run_id": str(run["id"])}}, False),
            ("maintainer", "maintainer", [],
            template("legacy/review-pass"),
            {"version": 1, "expression": {"op": "forge_actionable_count", "project_id": str(project_id),
                "origin_run_id": str(run["id"]), "review_phase": phase["kind"],
                "repository_ids": phase_repository_ids, "kinds": ["pull_request", "issue"], "labels": [], "match": "any", "at_least": 1}}, False),
        )
    from .root_maintenance import NAME
    return ((NAME, "maintainer", [],
        template("legacy/root-maintenance"),
        {"version": 1, "expression": {"op": "any", "args": [
            {"op": "planning_needed", "run_id": str(run["id"])},
            {"op": "forge_actionable_count", "project_id": str(project_id),
             "origin_run_id": str(run["id"]), "review_phase": phase["kind"],
             "repository_ids": phase_repository_ids, "kinds": ["pull_request", "issue"],
             "labels": [], "match": "any", "at_least": 1}]}}, True),)
