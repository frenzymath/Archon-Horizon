"""Versioned review questions and assessments, independent of reviewer personas."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StringConstraints, model_validator


ReviewText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8000)]
Dimension = Literal["mathematical_contract", "public_design", "proof_trust",
                    "computational_cost", "distributability", "scholarly_traceability"]


class ReviewContract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReviewQuestion(ReviewContract):
    dimension: Dimension
    question: ReviewText


class ReviewPlan(ReviewContract):
    """Shadow classification: it cannot reduce an existing policy's coverage."""

    version: Literal[1] = 1
    mode: Literal["shadow"] = "shadow"
    base_commit_oid: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]
    scope_paths: list[ReviewText] = Field(min_length=1, max_length=256)
    questions: list[ReviewQuestion] = Field(min_length=1, max_length=6)
    risk_triggers: list[ReviewText] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def distinct_questions(self):
        if len({question.dimension for question in self.questions}) != len(self.questions):
            raise ValueError("Each dimension has one bounded question per invocation")
        return self


class ReviewFinding(ReviewContract):
    key: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")]
    dimension: Dimension
    severity: Literal["blocking", "improvement"]
    scope: ReviewText
    finding: ReviewText
    evidence: list[ReviewText] = Field(min_length=1, max_length=16)
    requested_change: ReviewText


class ReviewResolution(ReviewContract):
    review_id: UUID
    disposition: Literal["repaired", "withdrawn"]
    explanation: ReviewText
    evidence: list[ReviewText] = Field(min_length=1, max_length=16)


class ReviewAssessment(ReviewContract):
    version: Literal[1] = 1
    scope: ReviewText
    dimensions: list[Dimension] = Field(min_length=1, max_length=6)
    complete: StrictBool
    classification_confirmed: StrictBool = False
    evidence: list[ReviewText] = Field(min_length=1, max_length=32)
    limitations: list[ReviewText] = Field(default_factory=list, max_length=16)
    findings: list[ReviewFinding] = Field(default_factory=list, max_length=100)
    resolutions: list[ReviewResolution] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def distinct_entries(self):
        for values in (self.dimensions, [finding.key for finding in self.findings],
                       [resolution.review_id for resolution in self.resolutions]):
            if len(values) != len(set(values)):
                raise ValueError("Dimensions, finding keys and resolution targets must be distinct")
        if any(finding.dimension not in self.dimensions for finding in self.findings):
            raise ValueError("Findings must identify an assessed dimension")
        return self

    def check_verdict(self, verdict: str) -> None:
        blockers = any(finding.severity == "blocking" for finding in self.findings)
        if verdict == "approved" and (not self.complete or blockers):
            raise ValueError("Approval requires complete assessment without unresolved blocking findings")
        if verdict == "changes_requested" and not blockers:
            raise ValueError("A change request must identify an actionable blocking finding")
        if self.resolutions and verdict != "approved":
            raise ValueError("Only a completed independent approval confirms resolution")

    def render(self, verdict: str) -> str:
        decision = {"approved": "Approved", "changes_requested": "Changes requested", "commented": "Incomplete assessment"}[verdict]
        parts = [f"**{decision}.**"]
        for finding in self.findings:
            parts.append(f"- **{finding.key} ({finding.severity})**: {finding.finding} "
                         f"Scope: {finding.scope}. Requested change: {finding.requested_change} "
                         f"Evidence: {'; '.join(finding.evidence)}")
        for resolution in self.resolutions:
            parts.append(f"- Review `{resolution.review_id}` {resolution.disposition}: {resolution.explanation} "
                         f"Evidence: {'; '.join(resolution.evidence)}")
        parts.extend([f"Scope: {self.scope}", f"Evidence: {'; '.join(self.evidence)}"])
        if self.limitations:
            parts.append(f"Limits: {'; '.join(self.limitations)}")
        return "\n\n".join(parts)
