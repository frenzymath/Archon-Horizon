"""Actionable review work and its existing integration ownership."""

from uuid import UUID

from sqlalchemy import and_, func, or_, select, tuple_

from ..persistence.schema import tables


def _terminal_review_dependencies(condition):
    """Recognize review handoffs without turning arbitrary waits into ownership."""
    if not condition:
        return None
    expression = condition["expression"]

    def visit(expr):
        if expr["op"] == "all":
            dependencies = set()
            for child in expr["args"]:
                found = visit(child)
                if found is None:
                    return None
                dependencies.update(found)
            return dependencies
        if expr["op"] != "status_in":
            return None
        target = expr["target"]
        if target["kind"] == "assignment" and set(expr["values"]) <= {"completed", "failed", "cancelled"}:
            return {UUID(str(target["id"]))}
        if target["kind"] == "milestone_job" and set(expr["values"]) <= {"completed", "failed"}:
            return set()
        return None

    return visit(expression)


def recurring_maintenance_blocker(conn, candidate, automation):
    """Reserve concrete PR handoffs without serializing unrelated maintenance."""
    if (candidate["role"] != "maintainer" or candidate.get("functions")
            or candidate.get("instructions") != automation.get("instructions")
            or candidate.get("start_condition") != automation.get("start_condition")):
        return None
    expression = (candidate.get("start_condition") or {}).get("expression", {})
    if expression.get("op") != "forge_actionable_count":
        return None

    assignment, work = tables["assignment"], tables["review_work"]
    handoffs = []
    for row in conn.execute(select(assignment.c.start_condition).where(
            assignment.c.run_id == candidate["run_id"], assignment.c.role == "maintainer",
            assignment.c.automation_id.is_(None), assignment.c.status.in_(("pending", "running", "stopping")),
            ~assignment.c.functions.contains(["orchestrator"]), ~assignment.c.functions.contains(["planner"]),
            assignment.c.start_condition.is_not(None))).mappings():
        dependencies = _terminal_review_dependencies(row["start_condition"])
        if dependencies:
            handoffs.append(dependencies)
    reviewers = set().union(*handoffs)
    owned = set()
    if reviewers:
        # The durable-review workflow queues integration on its exact reviewer
        # owners. Their canonical work records identify the PR, without parsing
        # instructions or treating repository permission as exclusive ownership.
        items_by_reviewer = {}
        for row in conn.execute(select(work.c.assignment_id, work.c.forge_item_id).where(
                work.c.assignment_id.in_(reviewers))):
            items_by_reviewer.setdefault(row.assignment_id, set()).add(row.forge_item_id)
        for dependencies in handoffs:
            if dependencies <= items_by_reviewer.keys():
                for reviewer in dependencies:
                    owned.update(items_by_reviewer[reviewer])
    if not owned:
        return None
    item, repository = tables["forge_item"], tables["repository"]
    query = select(func.count(), func.count().filter(item.c.id.in_(owned))).select_from(
        item.join(repository, item.c.repository_id == repository.c.id)).where(
            repository.c.project_id == UUID(str(expression["project_id"])), repository.c.archived_at.is_(None),
            item.c.status == "open", item.c.kind.in_(expression["kinds"]), ~current_objection(item))
    if expression["repository_ids"]:
        query = query.where(item.c.repository_id.in_(expression["repository_ids"]))
    if expression.get("origin_run_id"):
        query = query.where(item.c.origin_run_id == UUID(str(expression["origin_run_id"])))
    if expression.get("review_phase"):
        query = query.where(item.c.review_phase == expression["review_phase"])
    if expression["labels"]:
        query = query.where(item.c.labels.contains(expression["labels"]) if expression["match"] == "all"
                            else item.c.labels.overlap(expression["labels"]))
    total, reserved = conn.execute(query).one()
    if reserved and total - reserved < expression["at_least"]:
        return (f"{reserved} actionable Forge items already have explicit integration owners; "
                f"{total - reserved} unowned items remain for this recurring maintainer")
    return None


def current_objection(item):
    review = tables["forge_review"].alias("objection")
    later = tables["forge_review"].alias("later_decision")
    identity = or_(
        and_(review.c.reviewer_descriptor_id.is_not(None),
             later.c.reviewer_descriptor_id == review.c.reviewer_descriptor_id),
        and_(review.c.reviewer_descriptor_id.is_(None), later.c.reviewer_descriptor_id.is_(None),
             later.c.reviewer_remote_id == review.c.reviewer_remote_id))
    superseded = select(later.c.id).where(
        later.c.forge_item_id == review.c.forge_item_id,
        later.c.commit_oid == review.c.commit_oid, identity,
        later.c.verdict.in_(("approved", "changes_requested")),
        ~later.c.summary.contains("**Historical reviewer feedback:**"),
        tuple_(later.c.observed_at, later.c.created_at, later.c.id) >
        tuple_(review.c.observed_at, review.c.created_at, review.c.id),
    ).correlate(review).exists()
    return select(review.c.id).where(
        review.c.forge_item_id == item.c.id, item.c.head_commit_oid.is_not(None),
        review.c.commit_oid == item.c.head_commit_oid, review.c.verdict == "changes_requested",
        ~review.c.summary.contains("**Historical reviewer feedback:**"), ~superseded,
    ).correlate(item).exists()
