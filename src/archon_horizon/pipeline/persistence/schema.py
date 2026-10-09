"""Normalized PostgreSQL schema for the control plane.

No import creates tables or opens a connection. Run the explicit Alembic
migration against a separately configured database to install this schema.
"""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Column, DateTime, ForeignKey,
    ForeignKeyConstraint, Identity, Index, MetaData, Numeric, String, Table,
    Text, UniqueConstraint, func, text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID


metadata = MetaData(naming_convention={
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
})
tables: dict[str, Table] = {}
IMMUTABLE_TABLES: set[str] = set()


def c(name: str, datatype: object = Text, *, nullable: bool = False, default: object = None) -> Column:
    options: dict = {"nullable": nullable}
    if default is not None:
        options["server_default"] = default
    return Column(name, datatype, **options)


def fk(name: str, target: str, *, nullable: bool = False, primary_key: bool = False, index: bool = True) -> Column:
    return Column(name, UUID(as_uuid=True), ForeignKey(f"{target}.id", ondelete="RESTRICT"),
                  nullable=nullable, primary_key=primary_key, index=index and not primary_key)


def instant(name: str, *, nullable: bool = True) -> Column:
    return c(name, DateTime(timezone=True), nullable=nullable)


def strings(name: str, *, default: str = "'{}'::text[]") -> Column:
    return c(name, ARRAY(Text), default=text(default))


def obj(name: str, *, nullable: bool = False, default: str | None = None) -> Column:
    return c(name, JSONB(none_as_null=True), nullable=nullable, default=text(default) if default else None)


def count(name: str, default: int | None = None) -> Column:
    return c(name, BigInteger, default=str(default) if default is not None else None)


def enum(name: str, values: str, *, default: str | None = None, nullable: bool = False) -> Column:
    options = values.split()
    quoted = ",".join("'" + value + "'" for value in options)
    return Column(name, Text, CheckConstraint(f"{name} IN ({quoted})", name=f"{name}_values"),
                  nullable=nullable, server_default=default)


def check(expression: str, name: str) -> CheckConstraint:
    return CheckConstraint(expression, name=name)


def record(name: str, *fields: object, mutable: bool = True) -> Table:
    base = [Column("id", UUID(as_uuid=True), primary_key=True, default=uuid4),
            c("created_at", DateTime(timezone=True), default=func.now())]
    if mutable:
        base += [c("updated_at", DateTime(timezone=True), default=func.now()),
                 count("revision", 1), check("revision > 0", "positive_revision")]
    else:
        IMMUTABLE_TABLES.add(name)
    result = Table(name, metadata, *base, *fields)
    tables[name] = result
    return result


def join(name: str, *fields: object) -> Table:
    result = Table(name, metadata, *fields)
    tables[name] = result
    return result


def numbered(scope: str | None = None) -> list[object]:
    number = Column("number", BigInteger, Identity(), nullable=False) if scope is None else count("number")
    return [number, check("number > 0", "positive_number"),
            UniqueConstraint(*([scope] if scope else []), "number")]


project = record("project", *numbered(), c("slug", String(64)), c("title"),
                 c("description", default=""), enum("workflow", "graph legacy milestones", default="graph"),
                 instant("archived_at"), UniqueConstraint("slug"))
integration = record("integration", enum("kind", "forge zulip"), c("endpoint"), c("credential_ref"),
                     c("enabled", Boolean, default=text("true")))
repository = record("repository", fk("project_id", "project"), c("slug", String(64)),
                    fk("integration_id", "integration"), c("remote_id"), c("remote_path", nullable=True),
                    c("default_branch"), enum("purpose", "workspace knowledge reference library"),
                    instant("archived_at"), UniqueConstraint("project_id", "slug"),
                    UniqueConstraint("integration_id", "remote_id"))
document = record("document", fk("project_id", "project"), *numbered("project_id"),
                  enum("kind", "roadmap specification notes reference_list"), c("title"),
                  fk("source_repository_id", "repository"), c("source_path"), c("source_commit_oid"),
                  instant("archived_at"), UniqueConstraint("source_repository_id", "source_path"))
node = record("node", fk("project_id", "project"), *numbered("project_id"), c("title"),
              fk("source_repository_id", "repository"), c("source_path"), c("source_commit_oid"),
              instant("archived_at"), UniqueConstraint("source_repository_id", "source_path"))
node_dependency = join("node_dependency", fk("child_node_id", "node", primary_key=True),
                       fk("parent_node_id", "node", primary_key=True),
                       check("child_node_id <> parent_node_id", "not_self"))
Index("ix_node_dependency_parent", node_dependency.c.parent_node_id)
source_projection = join("source_projection", fk("repository_id", "repository", primary_key=True),
                         Column("source_path", Text, primary_key=True), c("source_commit_oid"),
                         obj("metadata"), c("markdown"),
                         c("indexed_at", DateTime(timezone=True), default=func.now()))
mission = record("mission", fk("project_id", "project"), *numbered("project_id"),
                 fk("parent_id", "mission", nullable=True), fk("roadmap_document_id", "document", nullable=True),
                 c("title"), c("objective"), enum("status", "open completed cancelled", default="open"),
                 strings("acceptance_criteria"), c("delegation_note", nullable=True),
                 obj("scope", default="'{}'::jsonb"), count("max_open_children", 8),
                 instant("closed_at"), c("closure_note", nullable=True), instant("archived_at"),
                 check("max_open_children > 0", "child_budget"),
                 check("parent_id IS NULL OR parent_id <> id", "not_self"),
                 check("(status = 'open' AND closed_at IS NULL AND closure_note IS NULL) OR "
                       "(status <> 'open' AND closed_at IS NOT NULL AND closure_note IS NOT NULL)", "closure"))
mission_node = join("mission_node", fk("mission_id", "mission", primary_key=True), fk("node_id", "node", primary_key=True))
mission_document = join("mission_document", fk("mission_id", "mission", primary_key=True), fk("document_id", "document", primary_key=True))
reference = record("reference", fk("project_id", "project"), c("cite_key", String(64)),
                   enum("kind", "article book inproceedings thesis report webpage dataset other"),
                   c("title"), strings("authors"), c("issued_year", BigInteger, nullable=True),
                   c("venue", nullable=True), obj("identifiers", default="'{}'::jsonb"), strings("urls"),
                   c("abstract", nullable=True), c("metadata_source", nullable=True),
                   enum("status", "active incomplete withdrawn", default="active"), instant("archived_at"),
                   UniqueConstraint("project_id", "cite_key"))
for _identifier in ("doi", "arxiv", "isbn", "pmid"):
    _value = reference.c.identifiers[_identifier].astext
    if _identifier == "arxiv":
        _value = func.regexp_replace(func.regexp_replace(_value, r"v[1-9][0-9]*$", ""), r"\.[A-Z]{2}/", "/")
    Index(f"uq_reference_{_identifier}", reference.c.project_id, _value, unique=True,
          postgresql_where=reference.c.identifiers[_identifier].astext.is_not(None))

review_policy = record("review_policy", fk("project_id", "project"), c("slug", String(64)),
                       enum("specialist_mode", "required advisory", default="required"),
                       fk("maintainer_identity_id", "integration_identity", nullable=True),
                       strings("phases"), c("instructions"), strings("required_checks"),
                       strings("attention_labels", default="ARRAY['awaiting-review']::text[]"),
                       c("enabled", Boolean, default=text("true")), UniqueConstraint("project_id", "slug"),
                       check("cardinality(phases) > 0 AND phases <@ ARRAY['preprocessing','formalization','postprocessing']::text[]", "phases"))
reviewer_descriptor = record("reviewer_descriptor", fk("project_id", "project"), c("slug", String(64)),
                             strings("functions"), c("instructions"), fk("harness_id", "harness", nullable=True),
                             obj("model_options", default="'{}'::jsonb"),
                             enum("invocation", "subrequest assignment", default="subrequest"),
                             fk("integration_identity_id", "integration_identity", nullable=True),
                             c("enabled", Boolean, default=text("true")), UniqueConstraint("project_id", "slug"))
review_policy_repository = join("review_policy_repository", fk("review_policy_id", "review_policy", primary_key=True),
                                fk("repository_id", "repository", primary_key=True))
review_policy_reviewer = join("review_policy_reviewer", fk("review_policy_id", "review_policy", primary_key=True),
                              fk("reviewer_descriptor_id", "reviewer_descriptor", primary_key=True))
reviewer_guidance = join("reviewer_guidance", fk("reviewer_descriptor_id", "reviewer_descriptor", primary_key=True),
                        fk("document_id", "document", primary_key=True))
integration_identity = record("integration_identity", fk("integration_id", "integration"), fk("principal_id", "principal"),
                              c("remote_user_id"), c("credential_ref"), c("enabled", Boolean, default=text("true")),
                              UniqueConstraint("integration_id", "remote_user_id"))
forge_review = record("forge_review", fk("forge_item_id", "forge_item"), c("remote_id"), c("reviewer_remote_id"),
                      fk("reviewer_principal_id", "principal", nullable=True),
                      fk("reviewer_descriptor_id", "reviewer_descriptor", nullable=True),
                      fk("reviewer_descriptor_revision_id", "record_revision", nullable=True),
                      fk("provider_request_id", "provider_request", nullable=True),
                      fk("integration_identity_id", "integration_identity", nullable=True), strings("reviewer_functions"),
                      enum("verdict", "approved changes_requested commented dismissed"), c("summary"), c("commit_oid"),
                      instant("observed_at", nullable=False), UniqueConstraint("forge_item_id", "remote_id"), mutable=False)
review_gate = record("review_gate", fk("forge_item_id", "forge_item"), fk("policy_id", "review_policy"),
                     fk("policy_revision_id", "record_revision"), fk("maintainer_review_id", "forge_review", nullable=True),
                     enum("status", "pending accepted rejected stale", default="pending"),
                     c("accepted_commit_oid", nullable=True), instant("evaluated_at"), UniqueConstraint("forge_item_id"),
                     check("status <> 'accepted' OR (accepted_commit_oid IS NOT NULL AND maintainer_review_id IS NOT NULL AND evaluated_at IS NOT NULL)", "accepted_evidence"))
roadmap_snapshot = record("roadmap_snapshot", fk("project_id", "project"), fk("roadmap_document_id", "document"),
                          c("source_commit_oid"), fk("graph_manifest_artifact_id", "artifact"),
                          fk("acceptance_artifact_id", "artifact", nullable=True),
                          enum("status", "provisional frozen", default="provisional"), instant("frozen_at"),
                          check("(status = 'frozen' AND frozen_at IS NOT NULL AND acceptance_artifact_id IS NOT NULL) OR "
                                "(status = 'provisional' AND frozen_at IS NULL)", "frozen_evidence"), mutable=False)

milestone_check = record("milestone_check", fk("project_id", "project"), fk("repository_id", "repository"),
                        fk("principal_id", "principal"), c("source_commit_oid"), c("base_commit_oid"),
                        enum("kind", "route contract graph proof library"), obj("report"),
                        c("report_sha256"), UniqueConstraint("repository_id", "report_sha256"), mutable=False)
milestone_acceptance = record("milestone_acceptance", fk("project_id", "project"),
                             fk("snapshot_id", "roadmap_snapshot"), fk("principal_id", "principal"),
                             fk("check_id", "milestone_check"), fk("previous_snapshot_id", "roadmap_snapshot", nullable=True),
                             c("note"), UniqueConstraint("snapshot_id"), mutable=False)
milestone_job = record('milestone_job', fk('project_id', 'project'), fk('host_id', 'host'),
    fk('workspace_id', 'workspace'), fk('principal_id', 'principal'), obj('request'),
    enum('status', 'queued running completed failed', default='queued'), count('attempts', 0),
    c('claim_token', UUID(as_uuid=True), nullable=True), instant('lease_until'),
    fk('check_id', 'milestone_check', nullable=True), c('error', nullable=True))

run = record("run", *numbered(), fk("mission_id", "mission"), obj("phase"),
             fk("objective_id", "document", nullable=True),
             enum("orchestration", "legacy objective", default="legacy"),
             obj("queue_policies", default="'{}'::jsonb"), obj("requested_phases", default="'[]'::jsonb"),
             obj("phase_history", default="'[]'::jsonb"), obj("pending_phase", nullable=True),
             c("auto_advance", Boolean, default=text("false")),
             fk("adopted_roadmap_snapshot_id", "roadmap_snapshot", nullable=True),
             enum("status", "active paused draining stopping completed cancelled", default="active"),
             c("status_note", nullable=True), instant("started_at"), instant("finished_at"),
             c("max_assignments", BigInteger, nullable=True), c("token_budget", BigInteger, nullable=True),
             instant("expires_at"), obj("retry_policy"),
             check("max_assignments IS NULL OR max_assignments > 0", "assignment_budget"),
             check("token_budget IS NULL OR token_budget >= 0", "token_budget"),
             check("coalesce(phase->>'kind' IN ('preprocessing','formalization','postprocessing'), false)", "phase_kind"),
             check("(phase->>'kind' <> 'formalization' AND adopted_roadmap_snapshot_id IS NULL) OR "
                   "(phase->>'kind' = 'formalization' AND "
                   "(adopted_roadmap_snapshot_id IS NOT NULL OR orchestration = 'objective'))", "adopted_baseline"))
run_host = join("run_host", fk("run_id", "run", primary_key=True), fk("host_id", "host", primary_key=True),
                c("enabled", Boolean, default=text("true")))
automation = record("automation", fk("run_id", "run"), c("name", String(64)), fk("mission_id", "mission"),
                     c("frontier_hash", String(128), nullable=True), c("pause_reason", nullable=True),
                     enum("role", "worker maintainer", default="worker"), strings("functions"), c("instructions", nullable=True),
                     c("enabled", Boolean, default=text("true")), obj("start_condition", nullable=True), instant("not_before"),
                     count("cooldown_seconds"), count("no_progress_count", 0),
                     check("cooldown_seconds > 0 AND no_progress_count >= 0", "bounds"), UniqueConstraint("run_id", "name"))
assignment = record("assignment", fk("run_id", "run"), *numbered("run_id"), fk("mission_id", "mission"),
                     c("category", String(64), nullable=True), c("recurrence_key", String(200), nullable=True),
                     c("pause_reason", nullable=True),
                     fk("parent_id", "assignment", nullable=True), fk("automation_id", "automation", nullable=True),
                     fk("reviewer_descriptor_id", "reviewer_descriptor", nullable=True),
                     enum("role", "worker maintainer", default="worker"), strings("functions"), c("instructions", nullable=True),
                     fk("harness_id", "harness", nullable=True), obj("model_options", default="'{}'::jsonb"),
                     enum("status", "pending running stopping completed failed cancelled", default="pending"),
                     c("status_note", nullable=True), c("queue_rank", BigInteger), instant("not_before"), instant("expires_at"),
                     obj("start_condition", nullable=True), instant("checkpoint_requested_at"), instant("retry_at"), instant("started_at"), instant("finished_at"),
                     count("recovery_attempts", 0),
                     check("not_before IS NULL OR expires_at IS NULL OR not_before < expires_at", "start_window"),
                     check("parent_id IS NULL OR parent_id <> id", "not_self"),
                     check("recovery_attempts >= 0", "recovery_attempts"))
# An automation is a recurrence policy, not a mutex.  A backlog can require
# several short-lived maintainer batches at once; admission still enforces the
# actual host/provider limits.
Index("ix_assignment_live_automation", assignment.c.automation_id,
      postgresql_where=text("automation_id IS NOT NULL AND status IN ('pending','running','stopping')"))
Index("ix_assignment_queue", assignment.c.run_id, assignment.c.queue_rank, assignment.c.id,
      postgresql_where=text("status = 'pending'"))
# A ready successor and a running planner use different durable keys. Retries
# retain their key, so scheduler races cannot turn one work item into a backlog.
Index("uq_assignment_recurrence", assignment.c.recurrence_key, unique=True,
      postgresql_where=text("recurrence_key IS NOT NULL AND status IN ('pending','running','stopping')"))
Index("uq_run_live_objective", run.c.objective_id, unique=True,
      postgresql_where=text("orchestration = 'objective' AND objective_id IS NOT NULL AND status IN ('active','paused','draining','stopping')"))
# Objective executions pin None for uncapped delegation or the reserved child
# count for a bounded session. Legacy/default rows retain zero reservations.
execution = record("execution", c("native_capacity", BigInteger, nullable=True, default="0"), fk("assignment_id", "assignment"), *numbered("assignment_id"), fk("host_id", "host"),
                   fk("workspace_id", "workspace"), fk("harness_id", "harness"),
                   fk("assignment_revision_id", "record_revision"), fk("mission_revision_id", "record_revision"),
                   fk("harness_revision_id", "record_revision"), fk("skill_bundle_artifact_id", "artifact"),
                   fk("roadmap_snapshot_id", "roadmap_snapshot", nullable=True), fk("sandbox_manifest_artifact_id", "artifact"),
                   enum("status", "starting running stopping succeeded failed cancelled lost", default="starting"),
                   instant("lease_expires_at", nullable=False), instant("heartbeat_at"), instant("started_at"), instant("finished_at"),
                   instant("stop_confirmed_at"), obj("failure", nullable=True),
                   ForeignKeyConstraint(["host_id", "harness_id"], ["host_harness.host_id", "host_harness.harness_id"]))
for _field in ("assignment_id", "workspace_id"):
    Index(f"uq_execution_live_{_field}", execution.c[_field], unique=True,
          postgresql_where=text("status IN ('starting','running','stopping')"))
Index("ix_execution_live_lease", execution.c.lease_expires_at,
      postgresql_where=text("status IN ('starting','running','stopping')"))
Index("ix_execution_unconfirmed_host", execution.c.host_id, execution.c.harness_id, execution.c.workspace_id,
      postgresql_where=text("stop_confirmed_at IS NULL"))
provider_thread = record("provider_thread", fk("assignment_id", "assignment"), *numbered("assignment_id"),
                         c("label", String(256), nullable=True), c("description", nullable=True),
                         enum("kind", "primary child", default="primary"), fk("parent_request_id", "provider_request", nullable=True),
                         fk("workspace_id", "workspace"), fk("harness_revision_id", "record_revision"),
                         fk("skill_bundle_artifact_id", "artifact"), c("provider_thread_id", nullable=True), c("provider_state_ref"),
                         fk("predecessor_id", "provider_thread", nullable=True), c("recovery_note", nullable=True),
                         fk("applied_mission_revision_id", "record_revision", nullable=True),
                         fk("applied_roadmap_snapshot_id", "roadmap_snapshot", nullable=True),
                         c("applied_run_revision", BigInteger, nullable=True), obj("applied_model_options"),
                         enum("status", "creating available unavailable closed", default="creating"),
                         check("(kind = 'child') = (parent_request_id IS NOT NULL)", "child_parent"),
                         check("predecessor_id IS NULL OR (predecessor_id <> id AND recovery_note IS NOT NULL)", "recovery_note"))
Index("uq_provider_thread_live_primary", provider_thread.c.assignment_id, unique=True,
      postgresql_where=text("kind = 'primary' AND status IN ('creating','available')"))
provider_request = record("provider_request", fk("provider_thread_id", "provider_thread"), fk("execution_id", "execution"),
                          *numbered("provider_thread_id"),
                          enum("reason", "assignment continuation notification mission_update native_goal review"),
                          fk("reviewer_descriptor_id", "reviewer_descriptor", nullable=True),
                          fk("reviewer_descriptor_revision_id", "record_revision", nullable=True),
                          fk("guidance_manifest_artifact_id", "artifact", nullable=True),
                          fk("input_artifact_id", "artifact", nullable=True), c("provider_turn_id", nullable=True),
                          c("native_background", Boolean, nullable=True),
                          enum("status", "pending submitted running completed failed interrupted uncertain", default="pending"),
                          instant("submitted_at"), instant("started_at"), instant("finished_at"), obj("failure", nullable=True))
Index("uq_provider_request_active", provider_request.c.provider_thread_id, unique=True,
      postgresql_where=text("status IN ('pending','submitted','running','uncertain')"))

review_work = join("review_work", fk("forge_item_id", "forge_item", primary_key=True),
    Column("head_commit_oid", Text, primary_key=True),
    fk("reviewer_descriptor_revision_id", "record_revision", primary_key=True),
    fk("policy_revision_id", "record_revision", primary_key=True),
    Column("target_branch", Text, primary_key=True),
    fk("provider_request_id", "provider_request", nullable=True),
    fk("assignment_id", "assignment", nullable=True), fk("obligation_id", "obligation"),
    count("attempts", 1), check("attempts >= 1", "positive_attempts"),
    check("(provider_request_id IS NULL) <> (assignment_id IS NULL)", "one_owner"))

obligation = record("obligation", fk("assignment_id", "assignment"), fk("created_by_execution_id", "execution", nullable=True),
                    obj("comments", default="'[]'::jsonb"),
                    *numbered("assignment_id"), enum("kind", "deliverable decision delegation blocker review", default="deliverable"),
                    c("description"), enum("status", "open done handled superseded", default="open"), obj("resolution", nullable=True),
                    check("(status = 'open') = (resolution IS NULL) AND coalesce((status = 'open' AND resolution IS NULL) OR "
                          "(status = 'done' AND resolution->>'kind' = 'completed') OR "
                          "(status = 'handled' AND resolution->>'kind' IN ('delegated','scheduled','reconsider')) OR "
                          "(status = 'superseded' AND resolution->>'kind' = 'superseded'), false)", "resolution_status"))
artifact = record("artifact", fk("project_id", "project"), fk("created_by_execution_id", "execution", nullable=True),
                  enum("kind", "commit blob external"), obj("content"), mutable=False)
reference_file = record("reference_file", fk("reference_id", "reference"), fk("artifact_id", "artifact"),
                        c("filename", String(255)), c("description", default=""),
                        c("source_url", nullable=True), instant("archived_at"),
                        UniqueConstraint("reference_id", "artifact_id", "filename"))
run_coordination = join("run_coordination", fk("run_id", "run", primary_key=True),
                        c("frontier_hash"), instant("last_progress_at", nullable=False),
                        instant("checked_at", nullable=False),
                        fk("audit_obligation_id", "obligation", nullable=True),
                        c("audit_frontier_hash", nullable=True))

# Orchestration state is durable and intentionally separate from provider
# activity.  Issues are mutable (repeated observations increment occurrences),
# snapshots are immutable evidence, and plans are revisioned CAS proposals.
health_issue = record("health_issue", fk("project_id", "project", nullable=True),
                      fk("run_id", "run", nullable=True), fk("host_id", "host", nullable=True),
                      fk("assignment_id", "assignment", nullable=True), c("dedupe_key", String(256)),
                      c("code", String(64)), enum("severity", "info warning error critical", default="warning"),
                      enum("status", "open acknowledged resolved suppressed", default="open"),
                      c("summary"), obj("details", default="'{}'::jsonb"),
                      instant("first_seen_at", nullable=False), instant("last_seen_at", nullable=False),
                      instant("resolved_at"), count("occurrences", 1),
                      check("num_nonnulls(project_id,run_id,host_id,assignment_id) > 0", "scope"),
                      check("occurrences > 0", "occurrences"), UniqueConstraint("dedupe_key"))
Index("ix_health_issue_run_status", health_issue.c.run_id, health_issue.c.status)
Index("ix_health_issue_host_status", health_issue.c.host_id, health_issue.c.status)

health_snapshot = record("health_snapshot", fk("project_id", "project", nullable=True),
                         fk("run_id", "run", nullable=True), fk("host_id", "host", nullable=True),
                         enum("kind", "system run host", default="run"), c("dedupe_key", String(256)),
                         c("frontier_hash", String(128)), c("health_hash", String(128)), obj("payload"),
                         instant("captured_at", nullable=False),
                         check("num_nonnulls(project_id,run_id,host_id) > 0", "scope"),
                         UniqueConstraint("dedupe_key"), mutable=False)
Index("ix_health_snapshot_run_captured", health_snapshot.c.run_id, health_snapshot.c.captured_at)

control_plan = record("control_plan", fk("project_id", "project"), fk("run_id", "run"),
                      fk("created_by_execution_id", "execution", nullable=True), c("dedupe_key", String(256)),
                      enum("status", "proposed accepted applied rejected superseded expired", default="proposed"),
                      count("expected_run_revision"), c("expected_frontier_hash", String(128), nullable=True),
                      obj("actions"), c("rationale"), instant("applied_at"), instant("rejected_at"),
                      c("decision_note", nullable=True),
                      check("expected_run_revision > 0", "expected_run_revision"),
                      UniqueConstraint("dedupe_key"))
Index("ix_control_plan_run_status", control_plan.c.run_id, control_plan.c.status)
artifact_location = record("artifact_location", fk("artifact_id", "artifact"), fk("host_id", "host", nullable=True), c("locator"),
                           instant("verified_at"), instant("missing_since"), instant("removed_at"),
                           enum("removal_reason", "retention operator", nullable=True),
                           check("(removed_at IS NULL) = (removal_reason IS NULL)", "removal_reason"))
Index("uq_artifact_location_local", artifact_location.c.artifact_id, artifact_location.c.host_id, artifact_location.c.locator,
      unique=True, postgresql_where=text("host_id IS NOT NULL"))
Index("uq_artifact_location_shared", artifact_location.c.artifact_id, artifact_location.c.locator,
      unique=True, postgresql_where=text("host_id IS NULL"))
assignment_artifact = join("assignment_artifact", fk("assignment_id", "assignment", primary_key=True),
                           fk("artifact_id", "artifact", primary_key=True),
                           enum("purpose", "evidence reviewer_manifest", default="evidence"))
Index("uq_assignment_reviewer_manifest", assignment_artifact.c.assignment_id, unique=True,
      postgresql_where=text("purpose = 'reviewer_manifest'"))
publication = record("publication", fk("artifact_id", "artifact"), fk("requested_by_assignment_id", "assignment"), obj("target"),
                     fk("review_gate_id", "review_gate", nullable=True),
                     enum("status", "pending running verified failed cancelled", default="pending"), count("retry_count", 0),
                     instant("retry_at"), fk("lease_owner_host_id", "host", nullable=True), count("lease_epoch", 0),
                     instant("lease_expires_at"), instant("verified_at"), obj("failure", nullable=True),
                     check("retry_count >= 0 AND lease_epoch >= 0", "counters"),
                     check("status <> 'verified' OR verified_at IS NOT NULL", "verified_evidence"))
Index("uq_publication_target", publication.c.artifact_id, publication.c.target, unique=True)
forge_item = record("forge_item", fk("repository_id", "repository"), count("remote_number"),
                    enum("kind", "pull_request issue"), fk("origin_run_id", "run", nullable=True),
                    enum("review_phase", "preprocessing formalization postprocessing", nullable=True),
                    c("target_branch", nullable=True), c("title"), enum("status", "open merged closed resolved"),
                    c("head_commit_oid", nullable=True), c("author_remote_id", nullable=True), strings("labels"),
                    instant("observed_at", nullable=False), UniqueConstraint("repository_id", "kind", "remote_number"),
                    check("remote_number > 0", "remote_number"),
                    check("kind <> 'issue' OR (head_commit_oid IS NULL AND target_branch IS NULL AND status <> 'merged')", "issue_shape"))
review_demand = join("review_demand", fk("forge_item_id", "forge_item", primary_key=True),
                     fk("run_id", "run"), count("generation", 1), c("fingerprint", String(128)),
                     c("attention", Boolean, default=text("true")), c("observed_label", Boolean, default=text("false")),
                     fk("assignment_id", "assignment", nullable=True), count("handled_generation", 0),
                     count("owner_generation", 0),
                     c("note", nullable=True), instant("requested_at", nullable=False),
                     check("generation > 0 AND handled_generation >= 0 AND handled_generation <= generation", "generations"))
verification = record("verification", fk("artifact_id", "artifact"), fk("execution_id", "execution"),
                      enum("kind", "lean_build kernel_check test review"), enum("result", "passed failed inconclusive"),
                      c("description"), fk("log_artifact_id", "artifact", nullable=True), mutable=False)
usage_record = record("usage_record", fk("execution_id", "execution"), fk("provider_thread_id", "provider_thread", nullable=True),
                      c("provider_record_id"), c("input_tokens", BigInteger, nullable=True),
                      c("cached_input_tokens", BigInteger, nullable=True), c("output_tokens", BigInteger, nullable=True),
                      c("cost_usd", Numeric(24, 12), nullable=True),
                      c("accounting", JSONB, nullable=True),
                      check("input_tokens >= 0 AND cached_input_tokens >= 0 AND output_tokens >= 0 AND cost_usd >= 0", "nonnegative"), mutable=False)
Index("uq_usage_thread", usage_record.c.provider_thread_id, usage_record.c.provider_record_id, unique=True,
      postgresql_where=text("provider_thread_id IS NOT NULL"))
Index("uq_usage_execution", usage_record.c.execution_id, usage_record.c.provider_record_id, unique=True,
      postgresql_where=text("provider_thread_id IS NULL"))
Index("ix_usage_counter_identity", usage_record.c.accounting["identity"].astext,
      usage_record.c.accounting["epoch"].astext)
activity = record("activity", fk("assignment_id", "assignment"), fk("execution_id", "execution"),
                  fk("provider_thread_id", "provider_thread", nullable=True), fk("provider_request_id", "provider_request", nullable=True),
                  enum("kind", "checkpoint progress tool_use completion failure"), c("summary", nullable=True), strings("skills_used"),
                  fk("usage_record_id", "usage_record", nullable=True), instant("occurred_at", nullable=False), mutable=False)
activity_obligation = join("activity_obligation", fk("activity_id", "activity", primary_key=True), fk("obligation_id", "obligation", primary_key=True))
activity_artifact = join("activity_artifact", fk("activity_id", "activity", primary_key=True), fk("artifact_id", "artifact", primary_key=True))

host = record("host", c("slug", String(64)), c("display_name"), c("workspace_root"), c("scratch_root"), obj("sandbox"),
              enum("mode", "enabled draining disabled", default="enabled"), instant("heartbeat_at"), c("agent_version", nullable=True),
              obj("health", nullable=True),
              UniqueConstraint("slug"))
harness = record("harness", c("slug", String(64)), enum("adapter", "codex_app_server codex_exec claude_exec"),
                 c("adapter_version"), c("provider_version"), obj("model_options"), obj("settings"),
                 c("enabled", Boolean, default=text("true")), UniqueConstraint("slug"))
host_harness = join("host_harness", fk("host_id", "host", primary_key=True), fk("harness_id", "harness", primary_key=True),
                    c("executable_path"), c("provider_home"), c("credential_ref"), count("execution_slots"),
                    c("max_parallel_subagents", BigInteger, nullable=True),
                    c("enabled", Boolean, default=text("true")), c("updated_at", DateTime(timezone=True), default=func.now()),
                    count("revision", 1), check("execution_slots > 0 AND max_parallel_subagents >= 0 AND revision > 0", "bounds"))
workspace = record("workspace", instant("cleaned_at"), c("cleanup_error", nullable=True), fk("project_id", "project"), fk("host_id", "host"), fk("repository_id", "repository"),
                   c("path"), c("branch_name"), c("base_commit_oid"), c("head_commit_oid", nullable=True),
                   enum("status", "preparing ready unavailable retired", default="preparing"), UniqueConstraint("host_id", "path"))
# Automatic provider guards track outages without imposing a concurrency quota.
# Operator-created quotas and build pools retain positive finite bounds.
resource_limit = record("resource_limit", enum("kind", "provider_account build_pool"), c("slug", String(64)), c("max_concurrent", BigInteger, nullable=True),
                        instant("cooldown_until"), count("failure_count", 0), c("circuit_open", Boolean, default=text("false")),
                        c("circuit_reason", nullable=True), UniqueConstraint("kind", "slug"),
                        check("max_concurrent > 0 AND failure_count >= 0", "bounds"),
                        check("kind = 'provider_account' OR max_concurrent IS NOT NULL", "build_capacity"))
host_harness_limit = join("host_harness_limit", fk("host_id", "host", primary_key=True), fk("harness_id", "harness", primary_key=True),
                          fk("resource_limit_id", "resource_limit", primary_key=True),
                          ForeignKeyConstraint(["host_id", "harness_id"], ["host_harness.host_id", "host_harness.harness_id"]))
resource_claim = record("resource_claim", fk("resource_limit_id", "resource_limit"), fk("execution_id", "execution"),
                        fk("provider_request_id", "provider_request", nullable=True),
                        count("units", 1), instant("released_at"), check("units > 0", "positive_units"))
Index("uq_resource_claim_active", resource_claim.c.resource_limit_id, resource_claim.c.execution_id,
      unique=True, postgresql_where=text("released_at IS NULL AND provider_request_id IS NULL"))
Index("uq_resource_claim_child", resource_claim.c.resource_limit_id, resource_claim.c.provider_request_id,
      unique=True, postgresql_where=text("released_at IS NULL AND provider_request_id IS NOT NULL"))

discussion = record("discussion", fk("project_id", "project"), fk("integration_id", "integration"), c("channel_remote_id"), c("topic"),
                    enum("sync_status", "current reconciling unavailable", default="current"), instant("observed_at", nullable=False),
                    UniqueConstraint("project_id", "integration_id", "channel_remote_id", "topic"))
message = record("message", fk("discussion_id", "discussion"), c("remote_id"), c("remote_author_id"),
                 fk("author_principal_id", "principal", nullable=True), fk("source_assignment_id", "assignment", nullable=True),
                 c("body"), instant("posted_at", nullable=False), instant("edited_at"), instant("deleted_at"),
                 UniqueConstraint("discussion_id", "remote_id"))
subscription = record("subscription", fk("assignment_id", "assignment"), fk("subject_id", "object_reference"),
                      enum("mode", "digest prompt muted", default="digest"), enum("origin", "assignment explicit"), instant("expires_at"),
                      UniqueConstraint("assignment_id", "subject_id"))
notification = record("notification", fk("assignment_id", "assignment"), fk("event_id", "event"),
                      enum("urgency", "routine direct control"), instant("delivered_at"),
                      enum("disposition", "pending handled dismissed", default="pending"), c("disposition_note", nullable=True),
                      fk("obligation_id", "obligation", nullable=True), UniqueConstraint("assignment_id", "event_id"))
message_read = join("message_read", fk("assignment_id", "assignment", primary_key=True), fk("message_id", "message", primary_key=True),
                    Column("message_revision", BigInteger, primary_key=True), instant("read_at", nullable=False),
                    check("message_revision > 0", "positive_revision"))
IMMUTABLE_TABLES.add("message_read")
discussion_digest = record("discussion_digest", fk("assignment_id", "assignment"), fk("discussion_id", "discussion"),
                           fk("body_artifact_id", "artifact"), mutable=False)
digest_message = join("digest_message", fk("discussion_digest_id", "discussion_digest", primary_key=True),
                      fk("message_id", "message", primary_key=True), Column("message_revision", BigInteger, primary_key=True),
                      check("message_revision > 0", "positive_revision"))
IMMUTABLE_TABLES.add("digest_message")
connector_cursor = record("connector_cursor", fk("integration_id", "integration"), c("consumer", String(64)),
                          c("queue_remote_id", nullable=True), c("last_event_remote_id", nullable=True),
                          c("last_message_remote_id", nullable=True), enum("status", "current reconciling unavailable", default="reconciling"),
                          instant("last_synced_at"), UniqueConstraint("integration_id", "consumer"))

principal = record("principal", enum("kind", "human host agent service"), c("display_name"),
                   c("username", String(64), nullable=True), fk("host_id", "host", nullable=True),
                   fk("execution_id", "execution", nullable=True), c("service_name", String(64), nullable=True), instant("disabled_at"),
                   UniqueConstraint("username"), UniqueConstraint("host_id"), UniqueConstraint("execution_id"), UniqueConstraint("service_name"),
                   check("num_nonnulls(username,host_id,execution_id,service_name) = 1", "one_owner"),
                   check("(kind='human' AND username IS NOT NULL) OR (kind='host' AND host_id IS NOT NULL) OR "
                         "(kind='agent' AND execution_id IS NOT NULL) OR (kind='service' AND service_name IS NOT NULL)", "owner_kind"))
project_grant = join("project_grant", fk("principal_id", "principal", primary_key=True), fk("project_id", "project", primary_key=True),
                     enum("role", "viewer worker maintainer"), c("updated_at", DateTime(timezone=True), default=func.now()), count("revision", 1),
                     check("revision > 0", "positive_revision"))
system_grant = join("system_grant", fk("principal_id", "principal", primary_key=True),
                    Column("permission", Text, primary_key=True), check("permission = 'administer_installation'", "permission"))
password_identity = join("password_identity", fk("principal_id", "principal", primary_key=True), c("password_hash"),
                         c("updated_at", DateTime(timezone=True), default=func.now()))
credential = record("credential", fk("principal_id", "principal"), enum("kind", "browser_session api_key host_key execution_token"),
                    c("name"), c("token_hash"), c("display_prefix"), instant("expires_at"), instant("revoked_at"), instant("last_used_at"),
                    UniqueConstraint("token_hash"), check("kind NOT IN ('browser_session','execution_token') OR expires_at IS NOT NULL", "expiry"))

event = record("event", Column("sequence", BigInteger, Identity(), nullable=False, unique=True), fk("project_id", "project", nullable=True),
               fk("subject_id", "object_reference", nullable=True), fk("actor_principal_id", "principal", nullable=True),
               fk("execution_id", "execution", nullable=True), c("kind", String(64)), count("schema_version"), c("source"), c("source_event_id"),
               instant("occurred_at", nullable=False), obj("payload"), UniqueConstraint("source", "source_event_id"),
               check("schema_version > 0", "positive_schema_version"), mutable=False)
Index("ix_event_project_sequence", event.c.project_id, event.c.sequence)
outbox_operation = record("outbox_operation", fk("project_id", "project", nullable=True), fk("actor_principal_id", "principal"),
                          enum("kind", "provider_input goal_update zulip_post forge_label forge_review forge_merge forge_create forge_comment forge_change forge_edit publication worker_event"), c("idempotency_key"),
                          obj("payload"), count("schema_version"), enum("status", "pending running completed failed uncertain cancelled", default="pending"),
                          count("retry_count", 0), instant("retry_at"), c("lease_owner", nullable=True), count("lease_epoch", 0),
                          instant("lease_expires_at"), fk("result_ref_id", "object_reference", nullable=True), obj("failure", nullable=True),
                          UniqueConstraint("actor_principal_id", "kind", "idempotency_key"),
                          check("schema_version > 0 AND retry_count >= 0 AND lease_epoch >= 0", "counters"))
Index("ix_outbox_ready", outbox_operation.c.retry_at, outbox_operation.c.created_at,
      postgresql_where=text("status = 'pending'"))
api_request = record("api_request", fk("principal_id", "principal"), fk("project_id", "project", nullable=True),
                     c("operation", String(64)), c("idempotency_key"), c("request_sha256"),
                     obj("response", nullable=True), fk("response_artifact_id", "artifact", nullable=True),
                     enum("status", "pending completed failed", default="pending"), instant("expires_at", nullable=False),
                     UniqueConstraint("principal_id", "operation", "idempotency_key"),
                     check("num_nonnulls(response,response_artifact_id) <= 1 AND "
                           "(status <> 'completed' OR num_nonnulls(response,response_artifact_id) = 1)", "response_storage"))

# A single canonical, FK-backed selector replaces unvalidated polymorphic IDs.
REFERENCE_KINDS = (
    "project", "repository", "mission", "run", "assignment", "execution", "provider_thread", "provider_request",
    "automation", "obligation", "activity", "node", "document", "reference", "roadmap_snapshot", "review_policy",
    "reviewer_descriptor", "integration_identity", "review_gate", "forge_review", "forge_item", "discussion", "message",
    "artifact", "publication", "host", "harness", "workspace", "verification",
)
_reference_fields = [fk(f"{name}_id", name, nullable=True, index=name == "repository") for name in REFERENCE_KINDS]
object_reference = record("object_reference", enum("kind", " ".join(REFERENCE_KINDS) + " file directory"),
                          *_reference_fields, c("path", nullable=True),
                          check("num_nonnulls(" + ",".join(f"{name}_id" for name in REFERENCE_KINDS) + ") = 1", "one_target"),
                          check(" OR ".join(f"(kind = '{name}' AND {name}_id IS NOT NULL AND path IS NULL)" for name in REFERENCE_KINDS)
                                + " OR (kind IN ('file','directory') AND repository_id IS NOT NULL AND path IS NOT NULL)", "target_kind"),
                          check("path IS NULL OR (path <> '' AND path !~ '(^/|(^|/)\\.\\.(/|$))')", "relative_path"), mutable=False)
for _kind in REFERENCE_KINDS:
    Index(f"uq_object_reference_{_kind}", object_reference.c[f"{_kind}_id"], unique=True,
          postgresql_where=text("kind = 'repository'" if _kind == "repository" else f"{_kind}_id IS NOT NULL"))
Index("uq_object_reference_path", object_reference.c.kind, object_reference.c.repository_id, object_reference.c.path,
      unique=True, postgresql_where=text("kind IN ('file','directory')"))
record_revision = record("record_revision", fk("object_id", "object_reference"), count("object_revision"), count("schema_version"),
                         fk("actor_principal_id", "principal"), obj("content"), UniqueConstraint("object_id", "object_revision"),
                         check("object_revision > 0 AND schema_version > 0", "positive_versions"), mutable=False)
reference_usage = record("reference_usage", fk("reference_id", "reference"), fk("subject_id", "object_reference"),
                         c("locator", nullable=True), c("cited_as", String(64)), mutable=False)
discussion_subject = join("discussion_subject", fk("discussion_id", "discussion", primary_key=True),
                          fk("subject_id", "object_reference", primary_key=True))
message_reference = join("message_reference", fk("message_id", "message", primary_key=True),
                         fk("subject_id", "object_reference", primary_key=True),
                         Column("purpose", Text, primary_key=True), check("purpose IN ('link','mention')", "purpose"))

# Reject malformed generic JSON values even for privileged SQL writers. More
# specific discriminated validation and cross-project access remain services.
for _table in tables.values():
    for _column in _table.c:
        if isinstance(_column.type, JSONB):
            shape = "array" if (_table.name, _column.name) in {("run", "requested_phases"), ("run", "phase_history"), ("obligation", "comments")} else "object"
            _table.append_constraint(check(f"{_column.name} IS NULL OR jsonb_typeof({_column.name}) = '{shape}'", f"{_column.name}_{shape}"))
