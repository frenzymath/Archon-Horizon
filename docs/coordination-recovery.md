# Legacy Maintenance And Recovery

This reference describes `orchestration: legacy`. New objective runs use the
[objective lifecycle and migration contract](design/objective-orchestration.md).

Explicit legacy runs use one `root-maintainer` automation. It creates a root-scoped
maintainer task for the next decision: choose work, review a result, arrange a
repair or close the phase. Workers and child maintainers own narrower outcomes.
There is no new task store, scheduling profile or agent hierarchy.

## Admission And Changed Evidence

The scheduler keeps at most one unfinished occurrence of the root automation.
Its initial condition admits planning or actionable Forge work under the normal
capacity, budget, lease and workspace checks. After its first pass, spare
capacity alone cannot launch repeated maintenance sessions.

The existing `run_coordination` record stores the substantive evidence fingerprint
seen at maintenance admission. Published commit contents, current PR heads and
statuses, review decisions and nonrecurring worker outcomes can change it.
Repeated root-session completion, queue churn and observation timestamps do not.
Changed evidence is a reason to reconsider the next action, not mathematical
acceptance.

An integration owner can checkpoint on a specific external event, including
failure/cancellation, and release its execution slot. Its retained context resumes
when that condition is satisfied. A deliberate event wait remains authoritative;
an idle slot does not bypass it. Native children still need their parent to collect
them within the same live execution.

The default root path does not also enter idle-planner refill or legacy
coordination-recovery admission. Those mechanisms remain for previously configured
separate planner/supervisor runs. Fresh orchestrated launches are rejected. Saved `orchestrated: true` runs retain that
compatibility path; it is not the new default.

## Failure And Missing Ownership

Ordinary execution recovery first preserves the same assignment, provider context,
journal and source work under the configured retry policy. A failed worker or
changed review result gives the root maintainer new evidence to act on.

When the root owner terminates unsuccessfully, or finishes an open mission with
neither a follow-up owner nor an explicit event wait, the existing automation can
own a recovery decision. The decision identifies the observed failure and asks
for a concrete repair or justified wait. The agent may invoke a diagnostic native
helper if the cause is unclear; an audit is not a mandatory extra stage.

Recovery is deduplicated against the substantive evidence. Repeating the same
failure does not recursively launch more coordinators. An unchanged failed
recovery remains visible as blocked, with its preserved decision and work.
New substantive evidence or explicit operator recovery can make another useful
attempt possible. This avoids an unbounded retry storm; it does not claim that
every infrastructure failure can be repaired autonomously.

Journal reconciliation remains separate from mathematical progress. A lost API
reply must be reconciled under its original idempotency key. Known no-effect
validation failures are diagnostics. Unknown transport outcomes and state conflicts
remain unresolved until inspected. Clearing a wrapper obligation alone cannot
settle an uncertain local request.

## On-Demand Diagnosis

`horizon-pipeline agent context --view operations` is available to live workers
and maintainers. It exposes bounded owner/activity data, queue reasons, review
coverage and sanitized host health, with scoped drilldowns. The
`orchestration-auditor` native helper investigates an identified anomaly and
returns evidence, cause, proposed action, responsible owner and confirmation
condition. Its parent owns mutations and incident communication.

The helper cannot initiate itself when no parent is running. Scheduler recovery
therefore retains responsibility for making the existing maintenance entrypoint
available. Agents retain responsibility for mathematical decomposition, review
and integration decisions. A diagnostic message alone is not a repaired run.

## Completion And Deployment

Worker delivery, specialist assessment, maintainer acceptance and phase completion
are different facts. The current API still requires explicit obligation and
mission settlement; it does not infer them from final prose. The root maintainer
records semantic completion, drains the run and returns. Infrastructure waits
for physical execution, publication and external delivery settlement.

No schema migration is added for this workflow; it reuses existing assignments,
automations, obligations and coordination state. Normal installations still need
the repository's current migrations. Source updates do not rewrite existing
automations, pinned instruction bundles or project reviewer descriptors.

Regression tests cover startup, unchanged-evidence suppression, failure recovery,
review repair, explicit waits and root closure. Prompt/skill checks cover scoped
reviewer startup and phase acceptance boundaries. These establish specific runtime
invariants; they do not prove model judgment or a preprocessing duration target.
A fresh representative Luna run is still needed to measure the revised workflow.
