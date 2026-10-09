"""Argument contracts shared by command execution and agent discovery."""

from copy import deepcopy
from uuid import UUID

from pydantic import Field, StrictBool, StrictStr, create_model

from .models import AssignmentCreate, AutomationCreate, Contract, ObligationCreate, Positive, Text, ObjectRef, RunPhase


class Empty(Contract):
    pass


class Note(Contract):
    note: StrictStr


class OptionalNote(Contract):
    note: Text | None = None


class ExtendRunBudget(Contract):
    additional_assignments: Positive
    note: Text


class SetRunBudget(Contract):
    max_assignments: Positive | None
    note: Text


class DeliveryAbsence(Contract):
    note: StrictStr = Field(min_length=1, max_length=16000,
        description="Operator conclusion and references to the exact remote evidence proving this delivery had no effect; does not resend")


class Neighbor(Contract):
    other_id: UUID


class Snapshot(Contract):
    snapshot_id: UUID


class ReviewSettlement(Note):
    generation: Positive


class ReviewRequest(Note):
    run_id: UUID | None = None


class LedgerComment(Contract):
    note: Text = Field(max_length=8000)


class PhaseAcceptance(Note):
    evidence: list[ObjectRef] = Field(min_length=1, max_length=32)
    next_phase: RunPhase | None = None


class Repair(Note):
    assignment: AssignmentCreate


class Subscription(Contract):
    assignment_id: UUID
    subscribed: StrictBool


class Fencing(Note):
    evidence: Text
    machine_fenced: StrictBool


def subset(name, source, fields, **extra):
    # Reuse field metadata so discovery follows the domain model's constraints.
    definitions = {key: (source.model_fields[key].annotation, deepcopy(source.model_fields[key])) for key in fields}
    return create_model(name, __base__=Contract, **definitions, **extra)


EditObligation = subset("EditObligation", ObligationCreate, ("description", "kind"))
UpdateAssignment = subset("UpdateAssignment", AssignmentCreate,
    ("not_before", "expires_at", "start_condition", "instructions", "functions"))
CheckpointAssignment = subset("CheckpointAssignment", AssignmentCreate,
    ("not_before", "start_condition"), note=(Text, ...))
DeferAutomation = subset("DeferAutomation", AutomationCreate,
    ("not_before", "start_condition", "cooldown_seconds"),
    enabled=(StrictBool, True), no_progress=(StrictBool, False))

class MaintenanceRequest(Note):
    evidence: list[ObjectRef] = Field(min_length=1, max_length=32)


COMMAND_ARGS = {
    "complete_mission": Note, "cancel_mission": Note, "reopen_mission": Note,
    "pause_run": OptionalNote, "resume_run": OptionalNote, "cancel_run": OptionalNote,
    "drain_run": OptionalNote, "complete_run": OptionalNote, "reopen_run": OptionalNote,
    "extend_run_budget": ExtendRunBudget, "set_run_budget": SetRunBudget,
    "adopt_roadmap_snapshot": Snapshot, "queue_repair": Repair,
    "cancel_assignment": OptionalNote, "retry_assignment": Empty,
    "move_before": Neighbor, "move_after": Neighbor, "update_assignment": UpdateAssignment,
    "checkpoint_assignment": CheckpointAssignment, "resume_assignment": Note,
    "defer_automation": DeferAutomation, "retry_publication": Empty,
    "set_subscription": Subscription, "retry_delivery": Note, "cancel_delivery": Note,
    "reconcile_delivery_absent": DeliveryAbsence,
    "recover_context": Note, "edit_obligation": EditObligation, "reopen_obligation": Note,
    "confirm_host_stopped": Fencing,
    "request_review": ReviewRequest, "settle_review": ReviewSettlement,
    "comment_obligation": LedgerComment, "resume_session": Note,
    "accept_phase": PhaseAcceptance, "request_maintenance": MaintenanceRequest,
    "reset_circuit": Note, "retire_workspace": Note,
}
