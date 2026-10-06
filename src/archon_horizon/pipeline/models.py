from __future__ import annotations

"""Closed value contracts shared by the API, scheduler and worker journal.

Database ownership and authority checks belong to the transaction service;
these models reject malformed and ambiguous values before a transaction starts.
"""

import re
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Annotated, Literal, Union
from uuid import UUID

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    TypeAdapter,
    field_validator,
    model_serializer,
    model_validator,
)

from .review_contracts import ReviewAssessment


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


def _nonblank(value: str) -> str:
    if not value.strip() or "\x00" in value:
        raise ValueError("text must be nonblank and contain no NUL")
    return value


def _relative_path(value: str) -> str:
    if value.startswith("/") or "\\" in value or ".." in value.split("/"):
        raise ValueError("path must remain within its repository")
    if str(PurePosixPath(value)) != value or value == ".":
        raise ValueError("path must be normalized and name a repository entry")
    return value


def _absolute_path(value: str) -> str:
    if not value.startswith("/") or ".." in value.split("/"):
        raise ValueError("path must be absolute without parent traversal")
    return value


def _timestamp(value: object) -> object:
    if isinstance(value, (int, float, bool)):
        raise ValueError("timestamp must be an aware datetime or RFC3339 string")
    return value


Text = Annotated[StrictStr, AfterValidator(_nonblank)]
Slug = Annotated[Text, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
Count = Annotated[StrictInt, Field(ge=0, le=2**63 - 1)]
Positive = Annotated[StrictInt, Field(gt=0, le=2**63 - 1)]
Instant = Annotated[
    AwareDatetime,
    BeforeValidator(_timestamp),
    AfterValidator(lambda value: value.astimezone(timezone.utc)),
]
RelativePath = Annotated[Text, AfterValidator(_relative_path)]
AbsolutePath = Annotated[Text, AfterValidator(_absolute_path)]
PhaseKind = Literal["preprocessing", "formalization", "postprocessing"]
Role = Literal["worker", "maintainer"]
RecordKind = Literal[
    "project", "repository", "mission", "run", "assignment", "execution",
    "provider_thread", "provider_request", "automation", "obligation", "activity",
    "node", "document", "reference", "roadmap_snapshot", "review_policy",
    "reviewer_descriptor", "integration_identity", "review_gate", "forge_review",
    "forge_item", "discussion", "message", "artifact", "publication", "host",
    "harness", "workspace", "verification",
]


class RecordRef(Contract):
    kind: RecordKind | Literal["milestone_job"]
    id: UUID


class StoredRecordRef(RecordRef):
    # Conditions can observe host jobs without adding persistent FK selectors.
    kind: RecordKind


class PathRef(Contract):
    kind: Literal["file", "directory"]
    repository_id: UUID
    path: RelativePath


ObjectRef = Annotated[Union[StoredRecordRef, PathRef], Field(discriminator="kind")]
object_ref_adapter = TypeAdapter(ObjectRef)


class All(Contract):
    op: Literal["all", "any"]
    args: Annotated[list["Expr"], Field(min_length=1, max_length=128)]


class Not(Contract):
    op: Literal["not"]
    arg: "Expr"


_STATUSES = {
    "mission": {"open", "completed", "cancelled"},
    "run": {"active", "paused", "draining", "stopping", "completed", "cancelled"},
    "assignment": {"pending", "running", "stopping", "completed", "failed", "cancelled"},
    "publication": {"pending", "running", "verified", "failed", "cancelled"},
    # Workspace readiness is a first-class admission dependency.  Planners
    # commonly gate verification or implementation on a prepared checkout;
    # omitting this lifecycle made valid child assignments impossible to queue.
    "workspace": {"preparing", "ready", "unavailable", "retired"},
    "milestone_job": {"queued", "running", "completed", "failed"},
}


class StatusIn(Contract):
    op: Literal["status_in"]
    target: RecordRef
    values: Annotated[list[Text], Field(min_length=1)]

    @model_validator(mode="after")
    def valid_statuses(self) -> "StatusIn":
        allowed = _STATUSES.get(self.target.kind)
        if allowed is None or not set(self.values).issubset(allowed):
            raise ValueError("condition status values must match the target lifecycle")
        return self


class ObligationAccounted(Contract):
    op: Literal["obligation_accounted"]
    obligation_id: UUID


class PublicationVerified(Contract):
    op: Literal["publication_verified"]
    publication_id: UUID


class After(Contract):
    op: Literal["after"]
    at: Instant


class QueueBelow(Contract):
    op: Literal["queue_below"]
    run_id: UUID
    count: Count


class RevisionAfter(Contract):
    op: Literal["revision_after"]
    target: RecordRef
    revision: Positive


class PlanningNeeded(Contract):
    op: Literal["planning_needed"]
    run_id: UUID


class DiscussionChanged(Contract):
    op: Literal["discussion_changed"]
    discussion_id: UUID
    fingerprint: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]


class ForgeOpenCount(Contract):
    op: Literal["forge_open_count"]
    project_id: UUID
    # Optional selectors preserve legacy conditions while allowing recurring
    # profiles to observe only work produced by their current phase run.
    origin_run_id: UUID | None = None
    review_phase: PhaseKind | None = None
    repository_ids: list[UUID] = Field(default_factory=list)
    kinds: Annotated[list[Literal["pull_request", "issue"]], Field(min_length=1)]
    labels: list[Text] = Field(default_factory=list)
    match: Literal["any", "all"] = "any"
    at_least: Count


class ForgeActionableCount(Contract):
    op: Literal["forge_actionable_count"]
    project_id: UUID
    origin_run_id: UUID | None = None
    review_phase: PhaseKind | None = None
    repository_ids: list[UUID] = Field(default_factory=list)
    kinds: Annotated[list[Literal["pull_request", "issue"]], Field(min_length=1)]
    labels: list[Text] = Field(default_factory=list)
    match: Literal["any", "all"] = "any"
    at_least: Count


Expr = Annotated[
    Union[All, Not, StatusIn, ObligationAccounted, PublicationVerified, After, QueueBelow, ForgeOpenCount,
          ForgeActionableCount,
          RevisionAfter, DiscussionChanged, PlanningNeeded],
    Field(discriminator="op"),
]
All.model_rebuild()
Not.model_rebuild()


class Condition(Contract):
    version: Literal[1] = 1
    expression: Expr

    @model_validator(mode="after")
    def bounded_tree(self) -> "Condition":
        pending: list[tuple[Expr, int]] = [(self.expression, 1)]
        count = 0
        while pending:
            node, depth = pending.pop()
            count += 1
            if count > 128 or depth > 8:
                raise ValueError("condition exceeds 128 nodes or depth 8")
            if isinstance(node, All):
                pending.extend((child, depth + 1) for child in node.args)
            elif isinstance(node, Not):
                pending.append((node.arg, depth + 1))
        return self


class Preprocessing(Contract):
    kind: Literal["preprocessing"]
    roadmap_document_id: UUID
    # False starts one root maintainer; True retains the explicit supervisor
    # workflow. Existing runs keep their persisted automations.
    orchestrated: StrictBool = False


class Formalization(Contract):
    kind: Literal["formalization"]
    roadmap_snapshot_id: UUID
    orchestrated: StrictBool = False


class Postprocessing(Contract):
    kind: Literal["postprocessing"]
    source_workspace_id: UUID
    source_commit_oid: Text
    target_repository_id: UUID
    orchestrated: StrictBool = False


RunPhase = Annotated[Union[Preprocessing, Formalization, Postprocessing], Field(discriminator="kind")]
run_phase_adapter = TypeAdapter(RunPhase)


class ModelOptions(Contract):
    model: Text | None = None
    reasoning_effort: Text | None = None

    @model_validator(mode="before")
    @classmethod
    def no_explicit_null(cls, values: object) -> object:
        if isinstance(values, dict) and any(value is None for value in values.values()):
            raise ValueError("omit inherited model options instead of passing null")
        return values

    @model_serializer
    def supplied_options(self) -> dict[str, str]:
        return {key: value for key in ("model", "reasoning_effort") if (value := getattr(self, key)) is not None}


class RetryPolicy(Contract):
    max_recovery_attempts: Positive = 5
    initial_delay_seconds: Positive = 5
    max_delay_seconds: Positive = 300
    max_no_progress_requests: Positive = 3

    @model_validator(mode="after")
    def ordered_delay(self) -> "RetryPolicy":
        if self.max_delay_seconds < self.initial_delay_seconds:
            raise ValueError("maximum delay cannot be smaller than initial delay")
        return self


class Failure(Contract):
    kind: Literal["provider", "transport", "host", "storage", "configuration", "execution"]
    code: Slug
    message: Text
    diagnostic_artifact_id: UUID | None = None


class Completed(Contract):
    kind: Literal["completed"]
    note: Text
    evidence: list[ObjectRef] = Field(default_factory=list)


class Delegated(Contract):
    kind: Literal["delegated"]
    note: Text
    assignment_ids: Annotated[list[UUID], Field(min_length=1)]


class Scheduled(Contract):
    kind: Literal["scheduled"]
    note: Text
    assignment_id: UUID


class Reconsider(Contract):
    kind: Literal["reconsider"]
    note: Text
    assignment_id: UUID
    evidence: list[ObjectRef] = Field(default_factory=list)


class Superseded(Contract):
    kind: Literal["superseded"]
    note: Text
    replacement_obligation_ids: list[UUID] = Field(default_factory=list)


Resolution = Annotated[Union[Completed, Delegated, Scheduled, Reconsider, Superseded], Field(discriminator="kind")]
resolution_adapter = TypeAdapter(Resolution)


class CommitContent(Contract):
    repository_id: UUID
    commit_oid: Text


class BlobContent(Contract):
    sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    size_bytes: Count
    media_type: Text


class ExternalContent(Contract):
    url: Text
    description: Text

    @model_validator(mode="after")
    def https_reference(self) -> "ExternalContent":
        from urllib.parse import urlsplit

        parsed = urlsplit(self.url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("external references require an HTTPS URL without credentials")
        return self


class GitTarget(Contract):
    kind: Literal["git"]
    repository_id: UUID
    ref_name: Text
    expected_old_oid: Text | None = None

    @model_validator(mode="after")
    def valid_ref(self) -> "GitTarget":
        ref = self.ref_name
        if (
            not ref.startswith("refs/") or ref.endswith(("/", ".")) or ".." in ref
            or "@{" in ref or "//" in ref
            or re.search(r"[\x00-\x20\x7f~^:?*\[\\]", ref)
            or any(part.startswith(".") or part.endswith(".lock") for part in ref.split("/"))
        ):
            raise ValueError("publication requires a valid full Git ref name")
        return self


class ArtifactStoreTarget(Contract):
    kind: Literal["artifact_store"]


PublicationTarget = Annotated[Union[GitTarget, ArtifactStoreTarget], Field(discriminator="kind")]


class AdapterSettings(Contract):
    schema_version: Literal[1] = 1
    approval_mode: Literal["deny", "automatic_review", "preauthorized"] = "deny"
    sandbox_mode: Literal["read_only", "workspace_write", "externally_isolated"] = "workspace_write"
    tool_names: list[Text] = Field(default_factory=list)
    auto_compaction: StrictBool = True


class SandboxMount(Contract):
    source: AbsolutePath
    target: AbsolutePath
    access: Literal["read_only", "read_write"] = "read_only"


class SandboxPolicy(Contract):
    schema_version: Literal[1] = 1
    mode: Literal["rootless_container", "unrestricted"] = "rootless_container"
    image_digest: Text | None = None
    network: Literal["outbound", "none"] = "outbound"
    extra_mounts: list[SandboxMount] = Field(default_factory=list)
    memory_limit_bytes: Positive | None = None
    cpu_limit: Positive | None = None
    process_limit: Positive | None = None

    @model_validator(mode="after")
    def pinned_image(self) -> "SandboxPolicy":
        if self.mode == "rootless_container" and (
            not self.image_digest or not re.search(r"@sha256:[0-9a-f]{64}$", self.image_digest)
        ):
            raise ValueError("container images must be pinned by sha256 digest")
        if len({mount.target for mount in self.extra_mounts}) != len(self.extra_mounts):
            raise ValueError("sandbox mount targets must be unique")
        return self


class ReferenceIdentifiers(Contract):
    doi: Text | None = None
    arxiv: Text | None = None
    isbn: Text | None = None
    pmid: Text | None = None

    @field_validator("doi", "arxiv", "isbn", "pmid")
    @classmethod
    def canonical_identifier(cls, value, info):
        from .reference_identifiers import normalize_identifier
        return normalize_identifier(info.field_name, value) if value is not None else None

    @model_serializer
    def present_identifiers(self):
        return {key: getattr(self, key) for key in ("doi", "arxiv", "isbn", "pmid") if getattr(self, key) is not None}


class ProjectCreate(Contract):
    slug: Slug
    title: Text
    description: StrictStr = ""
    workflow: Literal["legacy", "milestones"] = "milestones"


class MissionRepositoryScope(Contract):
    repository_id: UUID
    path: RelativePath | None = None


class MissionScope(Contract):
    node_ids: list[UUID] = Field(default_factory=list, max_length=256)
    document_ids: list[UUID] = Field(default_factory=list, max_length=256)
    repository_paths: list[MissionRepositoryScope] = Field(default_factory=list, max_length=256)


class MissionCreate(Contract):
    project_id: UUID
    parent_id: UUID | None = None
    expected_parent_revision: Positive | None = None
    roadmap_document_id: UUID | None = None
    title: Text
    objective: Text
    acceptance_criteria: list[Text] = Field(default_factory=list, max_length=64)
    delegation_note: Text | None = None
    scope: MissionScope | None = None
    max_open_children: Positive = 8
    node_ids: list[UUID] = Field(default_factory=list)
    document_ids: list[UUID] = Field(default_factory=list)


class MissionUpdate(Contract):
    expected_revision: Positive
    parent_id: UUID | None = None
    expected_parent_revision: Positive | None = None
    title: Text | None = None
    objective: Text | None = None
    acceptance_criteria: list[Text] | None = Field(default=None, max_length=64)
    delegation_note: Text | None = None
    scope: MissionScope | None = None
    max_open_children: Positive | None = None
    roadmap_document_id: UUID | None = None

    @model_validator(mode="before")
    @classmethod
    def preserve_required_text(cls, value: object) -> object:
        required = ("title", "objective", "acceptance_criteria", "scope", "max_open_children")
        if isinstance(value, dict) and any(key in value and value[key] is None for key in required):
            raise ValueError("mission contract fields cannot be cleared")
        return value


class RunCreate(Contract):
    mission_id: UUID
    phase: RunPhase
    host_ids: Annotated[list[UUID], Field(min_length=1)]
    max_assignments: Positive | None = None
    token_budget: Count | None = None
    expires_at: Instant | None = None
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)


class AssignmentCreate(Contract):
    run_id: UUID
    mission_id: UUID
    parent_id: UUID | None = None
    reviewer_descriptor_id: UUID | None = None
    role: Role = "worker"
    functions: list[Slug] = Field(default_factory=list)
    instructions: Text | None = None
    harness_id: UUID | None = None
    model_options: ModelOptions = Field(default_factory=ModelOptions)
    not_before: Instant | None = None
    expires_at: Instant | None = None
    start_condition: Condition | None = None

    @model_validator(mode="after")
    def valid_window(self) -> "AssignmentCreate":
        if self.not_before and self.expires_at and self.not_before >= self.expires_at:
            raise ValueError("not_before must precede expires_at")
        if len(set(self.functions)) != len(self.functions):
            raise ValueError("functions must be unique")
        return self


class ObligationCreate(Contract):
    assignment_id: UUID
    created_by_execution_id: UUID | None = None
    kind: Literal["deliverable", "decision", "delegation", "blocker", "review"] = "deliverable"
    description: Text


class ObligationResolve(Contract):
    expected_revision: Positive
    status: Literal["done", "handled", "superseded"]
    resolution: Resolution

    @model_validator(mode="after")
    def consistent_resolution(self) -> "ObligationResolve":
        expected = "done" if self.resolution.kind == "completed" else (
            "superseded" if self.resolution.kind == "superseded" else "handled"
        )
        if self.status != expected:
            raise ValueError("obligation status must agree with its resolution")
        return self


class AutomationCreate(Contract):
    run_id: UUID
    name: Slug
    mission_id: UUID
    role: Role = "worker"
    functions: list[Slug] = Field(default_factory=list)
    instructions: Text | None = None
    start_condition: Condition | None = None
    not_before: Instant | None = None
    cooldown_seconds: Positive = 60


class HostCapabilities(Contract):
    workspace_preparation: StrictInt | None = Field(default=None, ge=1, le=1)
    milestone_verification: StrictInt | None = Field(default=None, ge=1, le=1)


class StorageRootHealth(Contract):
    name: Text
    path: AbsolutePath
    filesystem: AbsolutePath
    total_bytes: StrictInt = Field(ge=1, le=2**63 - 1)
    free_bytes: StrictInt = Field(ge=0, le=2**63 - 1)
    required_free_bytes: StrictInt = Field(ge=0, le=2**63 - 1)
    cleanup_target_percent: StrictInt = Field(ge=0, le=90)
    cleanup_target_bytes: StrictInt = Field(default=0, ge=0, le=2**63 - 1)
    cleanup_recommended: StrictBool = False
    status: Literal["ready", "storage_pressure"]


class StorageHealth(Contract):
    status: Literal["ready", "storage_pressure"]
    cleanup_target_percent: StrictInt = Field(ge=0, le=90)
    cleanup_target_bytes: StrictInt = Field(default=0, ge=0, le=2**63 - 1)
    cleanup_recommended: StrictBool = False
    roots: list[StorageRootHealth] = Field(default_factory=list, max_length=128)
    free_bytes: StrictInt = Field(ge=0, le=2**63 - 1)
    required_free_bytes: StrictInt = Field(ge=0, le=2**63 - 1)


class HostHealth(Contract):
    capabilities: HostCapabilities = Field(default_factory=HostCapabilities)
    status: Literal["ready", "storage_pressure"]
    free_bytes: StrictInt = Field(ge=0, le=2**63 - 1)
    required_free_bytes: StrictInt = Field(ge=0, le=2**63 - 1)
    storage: StorageHealth | None = None


class HostHeartbeat(Contract):
    health: HostHealth


class MessageRevision(Contract):
    id: UUID
    revision: Positive


class ProviderInput(Contract):
    provider_request_id: UUID


class GoalUpdate(Contract):
    provider_thread_id: UUID
    mission_revision_id: UUID
    run_revision: Positive
    roadmap_snapshot_id: UUID | None = None


class ZulipPost(Contract):
    discussion_id: UUID
    body: Text
    read_revisions: list[MessageRevision] = Field(default_factory=list)


class ForgeLabel(Contract):
    forge_item_id: UUID
    add: list[Annotated[Text, Field(max_length=200)]] = Field(default_factory=list, max_length=100)
    remove: list[Annotated[Text, Field(max_length=200)]] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def disjoint(self) -> "ForgeLabel":
        if set(self.add) & set(self.remove):
            raise ValueError("cannot add and remove the same label")
        return self


class Publish(Contract):
    publication_id: UUID


class ForgeReviewComment(Contract):
    path: RelativePath
    body: Annotated[Text, Field(max_length=16000)]
    new_position: Count = 0
    old_position: Count = 0

    @model_validator(mode="after")
    def one_side(self) -> "ForgeReviewComment":
        if bool(self.new_position) == bool(self.old_position):
            raise ValueError("review comments need exactly one old or new file line")
        return self


class ReviewCarryForward(Contract):
    source_review_id: UUID
    evidence_artifact_id: UUID


class ReviewCarryForwardEvidence(Contract):
    kind: Literal["review_carry_forward"]
    version: Literal[1]
    forge_item_id: UUID
    source_review_id: UUID
    source_commit_oid: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]
    head_commit_oid: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]
    target_branch: Text
    base_commit_oid: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]
    scope_paths: list[RelativePath] = Field(min_length=1, max_length=256)
    delta_analysis: Annotated[Text, Field(max_length=8000)]
    dependency_analysis: Annotated[Text, Field(max_length=8000)]
    rationale: Annotated[Text, Field(max_length=4000)]


class ForgeReview(Contract):
    forge_item_id: UUID
    commit_oid: Text
    verdict: Literal["approved", "changes_requested", "commented"]
    summary: Text | None = None
    assessment: "ReviewAssessment | None" = None
    comments: list[ForgeReviewComment] = Field(default_factory=list, max_length=100)
    policy_revision_id: UUID | None = None
    reviewer_descriptor_id: UUID | None = None
    reviewer_descriptor_revision_id: UUID | None = None
    provider_request_id: UUID | None = None
    integration_identity_id: UUID | None = None
    historical: bool = False
    carry_forward: list[ReviewCarryForward] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def historical_feedback(self):
        if self.assessment:
            self.assessment.check_verdict(self.verdict)
            object.__setattr__(self, "summary", self.assessment.render(self.verdict))
            if self.historical and self.assessment.resolutions:
                raise ValueError("Historical feedback cannot resolve current objections")
        elif not self.summary:
            raise ValueError("Provide a structured assessment or a summary")
        if self.historical and (self.verdict != "commented" or not self.provider_request_id or self.comments):
            raise ValueError("Historical reviewer feedback requires an invocation, commented verdict and no inline coordinates")
        if self.carry_forward and (self.historical or self.verdict != "approved" or self.reviewer_descriptor_id
                                   or self.provider_request_id or self.reviewer_descriptor_revision_id):
            raise ValueError("Carry-forward is an explicit current-head maintainer approval, not a specialist verdict")
        if len({item.source_review_id for item in self.carry_forward}) != len(self.carry_forward):
            raise ValueError("Carry-forward source reviews must be distinct")
        return self


class ForgeMerge(Contract):
    forge_item_id: UUID
    review_gate_id: UUID
    expected_head_oid: Text
    integration_identity_id: UUID | None = None


class ForgeCreate(Contract):
    repository_id: UUID
    origin_run_id: UUID
    review_phase: Literal["preprocessing", "formalization", "postprocessing"]
    kind: Literal["pull_request", "issue"]
    title: Text
    body: Text
    head: Text | None = Field(default=None, description="Published head branch name from the verified change receipt, not its commit_oid")
    base: Text | None = Field(default=None, description="Target branch name; omit for the repository default branch")
    stack_reason: Text | None = None

    @model_validator(mode="after")
    def branches_match_kind(self):
        if self.kind == "pull_request" and not self.head:
            raise ValueError("pull requests require a head branch")
        if self.kind == "pull_request" and any(
                value and re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", value)
                for value in (self.head, self.base)):
            raise ValueError("pull requests require branch names, not commit hashes; use the publication's branch")
        if self.kind == "issue" and (self.head is not None or self.base is not None):
            raise ValueError("issues do not have head or base branches")
        return self


class ForgeComment(Contract):
    forge_item_id: UUID
    body: Text


class ForgeEdit(Contract):
    forge_item_id: UUID
    expected_head_oid: Text
    expected_base: Text
    base: Text | None = None
    state: Literal["open", "closed"] | None = None

    @model_validator(mode="after")
    def has_change(self):
        if self.base is None and self.state is None:
            raise ValueError("provide a base or state change")
        return self


GitObjectId = Annotated[Text, Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]


class ForgeFileChange(Contract):
    operation: Literal["create", "update", "delete"]
    path: RelativePath
    content_artifact_id: UUID | None = None
    sha: GitObjectId | None = None

    @model_validator(mode="after")
    def fields_match_operation(self):
        if self.operation in ("create", "update") and self.content_artifact_id is None:
            raise ValueError("creating or updating a file requires its content artifact")
        if self.operation == "delete" and self.content_artifact_id is not None:
            raise ValueError("deleting a file cannot supply replacement content")
        if self.operation in ("update", "delete") and self.sha is None:
            raise ValueError("updating or deleting a file requires its current blob SHA")
        if self.operation == "create" and self.sha is not None:
            raise ValueError("creating a new file cannot supply an existing blob SHA")
        return self


class ForgeChange(Contract):
    repository_id: UUID
    origin_run_id: UUID
    base_commit_oid: GitObjectId
    message: Text
    files: list[ForgeFileChange] = Field(min_length=1, max_length=200)
    forge_item_id: UUID | None = None

    @model_validator(mode="after")
    def unique_paths(self):
        if len({file.path for file in self.files}) != len(self.files):
            raise ValueError("a Forge change can contain only one operation per path")
        return self


class WorkerEvent(Contract):
    execution_id: UUID
    epoch: Positive
    source_event_id: Text
    event_artifact_id: UUID


OPERATION_MODELS: dict[str, type[Contract]] = {
    "provider_input": ProviderInput,
    "goal_update": GoalUpdate,
    "zulip_post": ZulipPost,
    "forge_label": ForgeLabel,
    "forge_review": ForgeReview,
    "forge_merge": ForgeMerge,
    "forge_create": ForgeCreate,
    "forge_comment": ForgeComment,
    "forge_change": ForgeChange,
    "forge_edit": ForgeEdit,
    "publication": Publish,
    "worker_event": WorkerEvent,
}


def validate_operation(kind: str, schema_version: int, payload: object) -> Contract:
    if schema_version != 1 or kind not in OPERATION_MODELS:
        raise ValueError("unknown outbox operation kind or schema version")
    return OPERATION_MODELS[kind].model_validate(payload)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class IntegrationCreate(Contract):
    kind: Literal["forge", "zulip"]
    endpoint: Text
    credential_ref: Text
    enabled: StrictBool = True

    @model_validator(mode="after")
    def secure_endpoint(self) -> "IntegrationCreate":
        from urllib.parse import urlsplit

        parsed = urlsplit(self.endpoint)
        if not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("integration endpoint must be an absolute URL without credentials")
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        ):
            raise ValueError("integration endpoint requires HTTPS except on loopback")
        return self


class RepositoryCreate(Contract):
    project_id: UUID
    slug: Slug
    integration_id: UUID
    remote_id: Text
    remote_path: Text | None = None
    default_branch: Text = "main"
    purpose: Literal["workspace", "knowledge", "reference", "library"]


class SourceCreate(Contract):
    project_id: UUID
    title: Text
    source_repository_id: UUID
    source_path: RelativePath
    source_commit_oid: Text


class DocumentCreate(SourceCreate):
    kind: Literal["roadmap", "specification", "notes", "reference_list"]


class NodeCreate(SourceCreate):
    parent_node_ids: list[UUID] = Field(default_factory=list)


class ReferenceCreate(Contract):
    project_id: UUID
    cite_key: Slug
    kind: Literal["article", "book", "inproceedings", "thesis", "report", "webpage", "dataset", "other"]
    title: Text
    authors: list[Text]
    issued_year: StrictInt | None = None
    venue: Text | None = None
    identifiers: ReferenceIdentifiers = Field(default_factory=ReferenceIdentifiers)
    urls: list[Text] = Field(default_factory=list)
    abstract: Text | None = None
    metadata_source: Text | None = None
    status: Literal["active", "incomplete", "withdrawn"] = "active"

    @model_validator(mode="after")
    def metadata_completeness(self):
        if self.status != "withdrawn":
            object.__setattr__(self, "status", "active" if self.authors and self.issued_year is not None else "incomplete")
        return self


class ReviewPolicyCreate(Contract):
    maintainer_identity_id: UUID | None = None
    project_id: UUID
    slug: Slug
    repository_ids: Annotated[list[UUID], Field(min_length=1)]
    phases: Annotated[list[PhaseKind], Field(min_length=1)]
    reviewer_descriptor_ids: list[UUID] = Field(default_factory=list)
    instructions: Text
    required_checks: list[Slug] = Field(default_factory=list)
    attention_labels: list[Annotated[Text, Field(max_length=200)]] = Field(default_factory=lambda: ["awaiting-review"], max_length=100)
    enabled: StrictBool = True

    @model_validator(mode="after")
    def unique_selectors(self) -> "ReviewPolicyCreate":
        for name in ("repository_ids", "phases", "reviewer_descriptor_ids"):
            values = getattr(self, name)
            if len(values) != len(set(values)):
                raise ValueError(f"{name} must contain unique values")
        return self


class ReviewerDescriptorCreate(Contract):
    project_id: UUID
    slug: Slug
    functions: list[Slug] = Field(default_factory=list)
    instructions: Text
    guidance_document_ids: list[UUID] = Field(default_factory=list)
    harness_id: UUID | None = None
    model_options: ModelOptions = Field(default_factory=ModelOptions)
    invocation: Literal["subrequest", "assignment"] = "subrequest"
    integration_identity_id: UUID | None = None
    enabled: StrictBool = True


class HostCreate(Contract):
    slug: Slug
    display_name: Text
    workspace_root: AbsolutePath
    scratch_root: AbsolutePath
    sandbox: SandboxPolicy
    mode: Literal["enabled", "draining", "disabled"] = "enabled"


class HarnessCreate(Contract):
    slug: Slug
    adapter: Literal["codex_app_server", "codex_exec", "claude_exec"]
    adapter_version: Text
    provider_version: Text
    model_options: ModelOptions
    settings: AdapterSettings
    enabled: StrictBool = True


class HostHarnessCreate(Contract):
    host_id: UUID
    harness_id: UUID
    executable_path: AbsolutePath
    provider_home: AbsolutePath
    credential_ref: Text
    execution_slots: Positive
    max_parallel_subagents: Count = 0
    enabled: StrictBool = True
    resource_limit_ids: list[UUID] = Field(default_factory=list)


class WorkspaceCreate(Contract):
    project_id: UUID
    host_id: UUID
    repository_id: UUID
    path: AbsolutePath
    branch_name: Text
    base_commit_oid: Text
    head_commit_oid: Text | None = None
    status: Literal["preparing", "ready", "unavailable", "retired"] = "preparing"


class RoadmapSnapshotCreate(Contract):
    project_id: UUID
    roadmap_document_id: UUID
    source_commit_oid: Text
    graph_manifest_artifact_id: UUID
    acceptance_artifact_id: UUID | None = None
    status: Literal["provisional", "frozen"] = "provisional"
    frozen_at: Instant | None = None

    @model_validator(mode="after")
    def frozen_evidence(self) -> "RoadmapSnapshotCreate":
        if self.status == "frozen" and (self.frozen_at is None or self.acceptance_artifact_id is None):
            raise ValueError("frozen snapshots require acceptance evidence and timestamp")
        if self.status == "provisional" and self.frozen_at is not None:
            raise ValueError("provisional snapshots cannot carry a freeze timestamp")
        return self


class SubscriptionCreate(Contract):
    assignment_id: UUID
    subject: ObjectRef
    mode: Literal["digest", "prompt", "muted"] = "digest"
    origin: Literal["assignment", "explicit"] = "explicit"
    expires_at: Instant | None = None


class ResourceLimitCreate(Contract):
    kind: Literal["provider_account", "build_pool"]
    slug: Slug
    max_concurrent: Positive


class IntegrationIdentityCreate(Contract):
    integration_id: UUID
    principal_id: UUID
    remote_user_id: Text
    credential_ref: Text
    enabled: StrictBool = True


CREATE_MODELS: dict[str, type[Contract]] = {
    "project": ProjectCreate, "mission": MissionCreate, "run": RunCreate,
    "assignment": AssignmentCreate, "automation": AutomationCreate,
    "obligation": ObligationCreate, "integration": IntegrationCreate,
    "repository": RepositoryCreate, "document": DocumentCreate, "node": NodeCreate,
    "reference": ReferenceCreate, "review_policy": ReviewPolicyCreate,
    "reviewer_descriptor": ReviewerDescriptorCreate, "host": HostCreate,
    "harness": HarnessCreate, "host_harness": HostHarnessCreate,
    "workspace": WorkspaceCreate, "roadmap_snapshot": RoadmapSnapshotCreate,
    "subscription": SubscriptionCreate, "resource_limit": ResourceLimitCreate,
    "integration_identity": IntegrationIdentityCreate,
}

# API collection fields are explicitly projected onto these normalized joins.
RELATIONS: dict[str, dict[str, tuple[str, str, str]]] = {
    "mission": {
        "node_ids": ("mission_node", "mission_id", "node_id"),
        "document_ids": ("mission_document", "mission_id", "document_id"),
    },
    "node": {"parent_node_ids": ("node_dependency", "child_node_id", "parent_node_id")},
    "review_policy": {
        "repository_ids": ("review_policy_repository", "review_policy_id", "repository_id"),
        "reviewer_descriptor_ids": ("review_policy_reviewer", "review_policy_id", "reviewer_descriptor_id"),
    },
    "reviewer_descriptor": {
        "guidance_document_ids": ("reviewer_guidance", "reviewer_descriptor_id", "document_id"),
    },
}
