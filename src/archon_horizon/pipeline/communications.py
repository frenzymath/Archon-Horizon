"""Selective subscriptions and exact message-revision receipts."""

from datetime import datetime, timezone
import hashlib
import re
from pathlib import PurePosixPath
from uuid import UUID

from pydantic import Field, model_validator
from sqlalchemy import BigInteger, and_, cast, delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert

from .auth import require_admin, require_project
from .errors import DomainError
from .models import Contract, ObjectRef, SubscriptionCreate, object_ref_adapter
from .records import change, create, get, object_ref, project_of, same_project, save_blob
from .schema import tables

SUBJECT_KINDS = {"mission", "node", "file", "directory", "discussion", "forge_item"}


class DiscussionRegistration(Contract):
    project_id: UUID
    topic: str = Field(min_length=1, max_length=60)
    source_discussion_id: UUID | None = None
    integration_id: UUID | None = None
    channel_remote_id: str | None = Field(default=None, pattern=r"^[1-9][0-9]{0,18}$")
    subjects: list[ObjectRef] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def one_binding(self):
        if self.topic != self.topic.strip() or any(c in self.topic for c in "\n\r\x00"):
            raise ValueError("topic must be a trimmed single line")
        if self.source_discussion_id:
            if self.integration_id or self.channel_remote_id:
                raise ValueError("fork an existing discussion or bind an integration/channel")
        elif not self.integration_id or not self.channel_remote_id:
            raise ValueError("new channel binding requires integration_id and channel_remote_id")
        return self


class DiscussionSubjectLink(Contract):
    discussion_id: UUID
    subject: ObjectRef


def register_discussion(conn, actor, data: DiscussionRegistration):
    require_project(conn, actor, data.project_id, "worker")
    if data.source_discussion_id:
        source = same_project(conn, "discussion", data.source_discussion_id, data.project_id)
        integration_id, channel = source["integration_id"], source["channel_remote_id"]
    else:
        require_admin(conn, actor)
        integration_id, channel = data.integration_id, data.channel_remote_id
    integration = get(conn, "integration", integration_id)
    if integration["kind"] != "zulip" or not integration["enabled"]:
        raise DomainError("discussion_integration_unavailable", "Discussion needs an enabled Zulip integration", 422)
    table = tables["discussion"]
    bound = list(conn.execute(select(table).where(table.c.integration_id == integration_id,
                                                table.c.channel_remote_id == channel)).mappings())
    if any(row["project_id"] != data.project_id for row in bound):
        raise DomainError("channel_scope_conflict", "This channel is already bound to another project", 422)
    existing = next((row for row in bound if row["topic"] == data.topic), None)
    result = dict(existing) if existing else create(conn, "discussion", project_id=data.project_id,
        integration_id=integration_id, channel_remote_id=channel, topic=data.topic,
        sync_status="reconciling", observed_at=func.now())
    for subject in data.subjects:
        link_subject(conn, actor, DiscussionSubjectLink(discussion_id=result["id"], subject=subject))
    return result


def scoped_subject(conn, subject: dict, project_id: UUID) -> UUID:
    value = object_ref_adapter.validate_python(subject).model_dump(mode="json")
    if value["kind"] not in SUBJECT_KINDS:
        raise DomainError("invalid_subject", "This object is not a discussion subject", 422)
    path = value.get("path")
    same_project(conn, "repository" if path else value["kind"],
                 value.get("repository_id", value.get("id")), project_id)
    return object_ref(conn, value["kind"], value.get("repository_id", value.get("id")), path)


def link_subject(conn, actor, data: DiscussionSubjectLink):
    discussion = get(conn, "discussion", data.discussion_id)
    require_project(conn, actor, discussion["project_id"], "worker")
    reference = scoped_subject(conn, data.subject.model_dump(mode="json"), discussion["project_id"])
    conn.execute(insert(tables["discussion_subject"]).values(discussion_id=discussion["id"],
        subject_id=reference).on_conflict_do_nothing())
    return {"discussion_id": discussion["id"], "subject_id": reference}


def discussion_channel(conn, actor, discussion_id: UUID):
    discussion = get(conn, "discussion", discussion_id)
    require_project(conn, actor, discussion["project_id"])
    table = tables["discussion"]
    if conn.execute(select(table.c.id).where(table.c.integration_id == discussion["integration_id"],
            table.c.channel_remote_id == discussion["channel_remote_id"],
            table.c.project_id != discussion["project_id"]).limit(1)).first():
        raise DomainError("channel_scope_conflict", "Channel discovery requires one unambiguous project binding", 422)
    integration = get(conn, "integration", discussion["integration_id"])
    if integration["kind"] != "zulip" or not integration["enabled"]:
        raise DomainError("discussion_integration_unavailable", "Discussion needs an enabled Zulip integration", 422)
    return discussion, integration


def seed_assignment_subscriptions(conn, assignment: dict) -> None:
    mission = get(conn, "mission", assignment["mission_id"])
    scopes = [("mission", mission["id"])]
    scopes.extend(("node", identifier) for identifier in conn.execute(select(tables["mission_node"].c.node_id).where(
        tables["mission_node"].c.mission_id == mission["id"])).scalars())
    for kind, identifier in scopes:
        reference = scoped_subject(conn, {"kind": kind, "id": str(identifier)}, mission["project_id"])
        conn.execute(insert(tables["subscription"]).values(assignment_id=assignment["id"], subject_id=reference,
            mode="digest", origin="assignment").on_conflict_do_nothing())


def subscribe(conn, actor, data: SubscriptionCreate, service):
    service.require_ledger_owner(conn, actor, data.assignment_id)
    subject = data.subject.model_dump(mode="json")
    if subject["kind"] not in SUBJECT_KINDS:
        raise DomainError("invalid_subscription", "This subject cannot be subscribed to", 422)
    project_id = project_of(conn, "assignment", data.assignment_id)
    path = subject.get("path")
    same_project(conn, "repository" if path else subject["kind"],
                 subject.get("repository_id", subject.get("id")), project_id)
    ref = object_ref(conn, subject["kind"], subject.get("repository_id", subject.get("id")), path)
    table = tables["subscription"]
    existing = conn.execute(select(table).where(table.c.assignment_id == data.assignment_id,
        table.c.subject_id == ref)).mappings().first()
    if existing:
        if existing["origin"] == "explicit" and data.origin == "assignment":
            return dict(existing)
        return change(conn, "subscription", existing["id"], mode=data.mode, origin=data.origin, expires_at=data.expires_at)
    return create(conn, "subscription", assignment_id=data.assignment_id, subject_id=ref,
                  mode=data.mode, origin=data.origin, expires_at=data.expires_at)


def read_messages(conn, actor, assignment_id: UUID, receipts: list[dict], service):
    service.require_ledger_owner(conn, actor, assignment_id)
    if len(receipts) > 200:
        raise DomainError("too_many_receipts", "Read at most 200 message revisions per request", 422)
    project_id = project_of(conn, "assignment", assignment_id)
    table = tables["message_read"]
    from .models import MessageRevision
    validated = []
    by_discussion = {}
    for raw in receipts:
        receipt = MessageRevision.model_validate(raw)
        message = same_project(conn, "message", receipt.id, project_id)
        if message["revision"] != receipt.revision:
            raise DomainError("message_changed", "A message changed; read its current revision before replying")
        validated.append((receipt, message))
        by_discussion.setdefault(message["discussion_id"], set()).add((receipt.id, receipt.revision))
    message_table = tables["message"]
    for discussion_id, supplied in by_discussion.items():
        previous = conn.execute(select(table.c.message_id).join(message_table,
            table.c.message_id == message_table.c.id).where(table.c.assignment_id == assignment_id,
                message_table.c.discussion_id == discussion_id).limit(1)).first()
        if previous:
            continue
        recent = set(conn.execute(select(message_table.c.id, message_table.c.revision).where(
            message_table.c.discussion_id == discussion_id).order_by(
                cast(message_table.c.remote_id, BigInteger).desc()).limit(20)).all())
        if not recent.issubset(supplied):
            raise DomainError("incomplete_initial_discussion_read",
                "First participation requires acknowledging the current 20-message initial window together; accumulate initial pages and retry after new arrivals",
                delta_url=f"/api/v3/discussions/{discussion_id}/messages?assignment_id={assignment_id}&unread_only=true")
    for receipt, message in validated:
        conn.execute(insert(table).values(assignment_id=assignment_id, message_id=receipt.id,
            message_revision=receipt.revision, read_at=func.now()).on_conflict_do_nothing())
        follow_discussion(conn, assignment_id, message["discussion_id"])
    return {"recorded": len(receipts)}


def follow_discussion(conn, assignment_id: UUID, discussion_id: UUID) -> None:
    """Participation follows a topic without overriding explicit mute/expiry."""
    reference = object_ref(conn, "discussion", discussion_id)
    conn.execute(insert(tables["subscription"]).values(assignment_id=assignment_id,
        subject_id=reference, mode="digest", origin="assignment").on_conflict_do_nothing())


def discussion_fingerprint(conn, discussion_id: UUID) -> str:
    message = tables["message"]
    digest = hashlib.sha256()
    discussion = get(conn, "discussion", discussion_id)
    digest.update(f"{discussion['channel_remote_id']}:{discussion['topic']}\n".encode("utf-8"))
    for row in conn.execute(select(message.c.id, message.c.revision, message.c.deleted_at).where(
            message.c.discussion_id == discussion_id).order_by(message.c.id)):
        digest.update(f"{row.id}:{row.revision}:{bool(row.deleted_at)}\n".encode("ascii"))
    return digest.hexdigest()


def discussion_read_scope(conn, discussion_id: UUID, assignment_id: UUID):
    """A bounded initial window, then every revision since actual participation.

    Message IDs are Zulip's monotonic identities, not arrival timestamps. This
    catches late reconciliation and edits/deletions without an unsafe timestamp
    high-water mark. Directly addressed older messages remain in scope too.
    """
    message, receipt = tables["message"], tables["message_read"]
    previous = conn.execute(select(func.min(cast(message.c.remote_id, BigInteger)))
        .select_from(message.join(receipt, receipt.c.message_id == message.c.id)).where(
            message.c.discussion_id == discussion_id, receipt.c.assignment_id == assignment_id)).scalar_one()
    if previous is None:
        recent = select(cast(message.c.remote_id, BigInteger).label("remote_id")).where(
            message.c.discussion_id == discussion_id).order_by(cast(message.c.remote_id, BigInteger).desc()).limit(20).subquery()
        previous = conn.execute(select(func.min(recent.c.remote_id))).scalar_one()
    reference, mentions = tables["object_reference"], tables["message_reference"]
    addressed = select(mentions.c.message_id).join(reference, reference.c.id == mentions.c.subject_id).where(
        mentions.c.message_id == message.c.id, reference.c.assignment_id == assignment_id).exists()
    scope = and_(message.c.discussion_id == discussion_id,
                 or_(cast(message.c.remote_id, BigInteger) >= (previous or 0), addressed))
    known = select(receipt.c.message_id).where(receipt.c.assignment_id == assignment_id,
        receipt.c.message_id == message.c.id, receipt.c.message_revision == message.c.revision).exists()
    return scope, known, previous


def queue_reply(conn, actor, service, *, discussion_id: UUID, assignment_id: UUID | None,
                body: str, read_revisions: list[dict], urgent: bool, key: str):
    discussion = get(conn, "discussion", discussion_id)
    require_project(conn, actor, discussion["project_id"], "worker")
    if urgent:
        require_admin(conn, actor)
    if not isinstance(body, str) or not body.strip() or len(body) > 32000:
        raise DomainError("invalid_message", "A reply must contain at most 32000 characters", 422)
    if assignment_id:
        service.require_ledger_owner(conn, actor, assignment_id)
        same_project(conn, "assignment", assignment_id, discussion["project_id"])
    if actor.kind == "agent" and not assignment_id:
        raise DomainError("missing_assignment", "Agent replies require their assignment identity", 422)
    read_start = None
    if assignment_id and not urgent:
        message = tables["message"]
        for asserted in read_revisions:
            current = get(conn, "message", asserted["id"])
            if current["discussion_id"] != discussion_id or current["revision"] != asserted["revision"]:
                raise DomainError("message_changed", "A supplied message revision changed; read its current revision before replying")
        scope, known, read_start = discussion_read_scope(conn, discussion_id, assignment_id)
        if conn.execute(select(message.c.id).where(scope, ~known).limit(1)).first():
            raise DomainError("unread_messages", "Read the relevant discussion revisions before replying",
                delta_url=f"/api/v3/discussions/{discussion_id}/messages?assignment_id={assignment_id}&unread_only=true")
        # Freeze the acknowledged revisions, including tombstones, for delivery.
        # The caller need not resend previously recorded receipts on every reply.
        read_revisions = [{"id": str(row.id), "revision": row.revision} for row in conn.execute(
            select(message.c.id, message.c.revision).where(scope).order_by(cast(message.c.remote_id, BigInteger)))]
    if assignment_id:
        follow_discussion(conn, assignment_id, discussion_id)
    blob = save_blob(conn, service.store, discussion["project_id"], {"body": body})
    row = create(conn, "outbox_operation", project_id=discussion["project_id"], actor_principal_id=actor.id,
                 kind="zulip_post", schema_version=1, idempotency_key=key,
                 payload={"discussion_id": str(discussion_id),
                          "source_assignment_id": str(assignment_id) if assignment_id else None,
                          "body_artifact_id": str(blob["id"]), "read_messages": read_revisions,
                          "read_start_remote_id": read_start,
                          "require_read_receipts": bool(assignment_id and not urgent)})
    return row


def _mention_text(body: str) -> str:
    lines = []
    fence = None
    for line in body.splitlines():
        match = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if match:
            marker = match.group(1)
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
            continue
        if fence is None:
            # Inline examples are not directed requests either.
            lines.append(re.sub(r"(`+).*?\1", "", line))
    return "\n".join(lines)


def resolve_mentions(conn, project_id: UUID, body: str) -> tuple[set[UUID], set[UUID]]:
    """Resolve project-scoped display references; unknown references stay plain text."""
    text = _mention_text(body[:32000])
    subjects: set[UUID] = set()
    direct: set[UUID] = set()
    pattern = re.compile(r"(?<![\w@])@(?:R([1-9]\d{0,17})/A([1-9]\d{0,17})\b|run/([1-9]\d{0,17})/assignment/([1-9]\d{0,17})\b|"
                         r"(N|M)([1-9]\d{0,17})\b|(node|mission)/([1-9]\d{0,17})\b|"
                         r"file/([a-z0-9][a-z0-9_-]*)/([^\s<>`]+)|([a-z0-9][a-z0-9_-]*):([^\s<>`]+))")
    for match in list(pattern.finditer(text))[:100]:
        run_number, assignment_number = match[1] or match[3], match[2] or match[4]
        if run_number:
            assignment, run, mission = (tables[name] for name in ("assignment", "run", "mission"))
            value = conn.execute(select(assignment).join(run, assignment.c.run_id == run.c.id).join(
                mission, run.c.mission_id == mission.c.id).where(run.c.number == int(run_number),
                assignment.c.number == int(assignment_number), mission.c.project_id == project_id)).mappings().first()
            if value:
                subjects.add(object_ref(conn, "assignment", value["id"]))
                if value["status"] in ("pending", "running"):
                    direct.add(value["id"])
            continue
        kind, number = ("node" if match[5] == "N" else "mission" if match[5] == "M" else match[7]), match[6] or match[8]
        if number:
            table = tables[kind]
            identifier = conn.execute(select(table.c.id).where(table.c.project_id == project_id,
                                      table.c.number == int(number))).scalar_one_or_none()
            if identifier:
                subjects.add(object_ref(conn, kind, identifier))
            continue
        slug, path = match[9] or match[11], (match[10] or match[12]).rstrip(".,;:!?)]}")
        directory = path.endswith("/")
        path = path.rstrip("/")
        try:
            repository = tables["repository"]
            identifier = conn.execute(select(repository.c.id).where(repository.c.project_id == project_id,
                repository.c.slug == slug, repository.c.archived_at.is_(None))).scalar_one_or_none()
            if identifier:
                subjects.add(scoped_subject(conn, {"kind": "directory" if directory else "file",
                    "repository_id": str(identifier), "path": path}, project_id))
        except ValueError:
            continue
    return subjects, direct


def route_subject_event(conn, event: dict) -> None:
    """Route an exact Forge item event without promoting routine updates to control."""
    if not event.get("subject_id") or not event.get("project_id"):
        return
    reference = get(conn, "object_reference", event["subject_id"])
    if reference["kind"] == "forge_review":
        item_id = get(conn, "forge_review", reference["forge_review_id"])["forge_item_id"]
        subject_id = object_ref(conn, "forge_item", item_id)
    elif reference["kind"] == "forge_item":
        item_id, subject_id = reference["forge_item_id"], event["subject_id"]
    else:
        return
    same_project(conn, "forge_item", item_id, event["project_id"])
    subscription, assignment, notification = (tables[name] for name in ("subscription", "assignment", "notification"))
    candidates = conn.execute(select(subscription.c.assignment_id, subscription.c.mode)
        .join(assignment, assignment.c.id == subscription.c.assignment_id).where(
            subscription.c.subject_id == subject_id, subscription.c.mode != "muted",
            assignment.c.status.in_(("pending", "running")),
            ((subscription.c.expires_at.is_(None)) | (subscription.c.expires_at > func.now())))).mappings()
    for row in candidates:
        if project_of(conn, "assignment", row["assignment_id"]) != event["project_id"]:
            continue
        conn.execute(insert(notification).values(assignment_id=row["assignment_id"], event_id=event["id"],
            urgency="direct" if row["mode"] == "prompt" else "routine").on_conflict_do_nothing())


def route_message(conn, event: dict, discussion_id: UUID, referenced_subject_ids: list[UUID] = ()):
    discussion = same_project(conn, "discussion", discussion_id, event["project_id"])
    subject, subscription, assignment = (tables[name] for name in ("discussion_subject", "subscription", "assignment"))
    discussion_ref = object_ref(conn, "discussion", discussion_id)
    selectors = {discussion_ref}
    selectors.update(conn.execute(select(subject.c.subject_id).where(subject.c.discussion_id == discussion_id)).scalars())
    for reference_id in referenced_subject_ids:
        reference = get(conn, "object_reference", reference_id)
        kind = reference["kind"]
        key = "repository_id" if kind in {"file", "directory"} else kind + "_id"
        if project_of(conn, "repository" if kind in {"file", "directory"} else kind, reference[key]) == event["project_id"]:
            selectors.add(reference_id)
    direct: set[UUID] = set()
    source_assignment_id = None
    reference = get(conn, "object_reference", event["subject_id"]) if event.get("subject_id") else None
    if reference and reference["kind"] == "message":
        message = get(conn, "message", reference["message_id"])
        source_assignment_id = message["source_assignment_id"]
        if message["discussion_id"] != discussion["id"]:
            raise DomainError("scope_mismatch", "Message event belongs to another discussion", 422)
        if message["deleted_at"] is None:
            mentions, direct = resolve_mentions(conn, event["project_id"], message["body"])
            selectors.update(mentions)
            relation = tables["message_reference"]
            conn.execute(delete(relation).where(relation.c.message_id == message["id"], relation.c.purpose == "mention"))
            for target in mentions:
                conn.execute(insert(relation).values(message_id=message["id"], subject_id=target,
                                                   purpose="mention").on_conflict_do_nothing())
    # A file mention reaches explicit ancestor-directory subscribers, never a
    # basename match in another repository. Creating references grants no access.
    for selector in list(selectors):
        value = get(conn, "object_reference", selector)
        if value["kind"] in {"file", "directory"}:
            for parent in PurePosixPath(value["path"]).parents:
                if str(parent) != ".":
                    selectors.add(object_ref(conn, "directory", value["repository_id"], str(parent)))
    candidates = list(conn.execute(select(subscription).join(assignment).where(subscription.c.subject_id.in_(selectors),
        assignment.c.status.in_(("pending", "running")),
        ((subscription.c.expires_at.is_(None)) | (subscription.c.expires_at > func.now())))).mappings())
    topic_muted = {row["assignment_id"] for row in candidates if row["subject_id"] == discussion_ref and row["mode"] == "muted"}
    recipients = {identifier: "direct" for identifier in direct}
    for subscribed in candidates:
        identifier = subscribed["assignment_id"]
        if subscribed["mode"] == "muted" or identifier in topic_muted or project_of(conn, "assignment", identifier) != event["project_id"]:
            continue
        urgency = "direct" if subscribed["mode"] == "prompt" else "routine"
        if recipients.get(identifier) != "direct":
            recipients[identifier] = urgency
    table = tables["notification"]
    recipients.pop(source_assignment_id, None)
    for identifier, urgency in recipients.items():
        proposed = insert(table).values(assignment_id=identifier, event_id=event["id"], urgency=urgency)
        conn.execute(proposed.on_conflict_do_update(index_elements=[table.c.assignment_id, table.c.event_id],
            set_={"urgency": "direct"}, where=and_(table.c.urgency == "routine", proposed.excluded.urgency == "direct")))
