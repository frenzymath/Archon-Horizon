---
name: horizon-start
description: Recover missing or stale assignment context, unpublished work, or an uncertain API operation after interruption.
metadata:
  category: operations
---

# Recover Useful Context

Use this procedure after interruption or when the supplied task brief is
incomplete. A normal continuation keeps its context and resumes the next action;
it does not repeat startup, catalog discovery or completed checks.

Run `horizon-pipeline agent context` when current state is needed. Read the
mission, assignment instructions, open obligations and relevant notices. These
define the task. A provider-native goal is an optional mirror, not another
authority or a prerequisite for work. Follow detail links only for relevant
truncated fields or missing evidence.

Recover the last useful artifact and the next unfinished action. Inspect the
assigned files and relevant PR discussion, not the entire project. Verify the
publication receipt before relying on a commit produced on another host. An
existing owner or published result is a reason to resume or collect that work,
not to create another broad assignment.

For an uncertain API response, run `horizon-pipeline agent pending` and reconcile
the existing operation before retrying. Reuse its idempotency key. `agent replay`
replays journaled intents after transport recovers; it does not create a new
intent. Read only the schema for the operation you actually need.

Act on relevant control notices and then record their disposition with the
current revision and a factual note. Routine discussion is not an instruction
to expand your scope. Use [workspace recovery](../horizon-workspace/SKILL.md)
when publication or checkout ownership is the blocker.

`recover_context` replaces an unusable provider context only after its assignment
is pending or failed, execution has stopped and requests have settled. A
completed objective session stays settled; queue a different bounded mission for
new work. A suspended objective session uses `resume_session` after a recorded
repair and keeps its retry history. Unusable native context can use `recover_context`
only after physical stop is confirmed. Explicit legacy `resume_assignment` is an
operator action and can fail when its workspace has been reassigned. For
planned work after a real external event, keep the current owner through an
event-conditioned [checkpoint](../horizon-delegation/references/queue.md).
