---
name: horizon-zulip
description: Discover relevant mathematical discussions, read changed messages, and negotiate decisions with scoped subscriptions and exact revision receipts.
metadata:
  category: operations
---

# Communicate deliberately

Read [communication conventions](../horizon-communication/SKILL.md) before
writing: message structure, Markdown/math, mentions and semantic emojis are
shared across discussions and handoffs. Topic markers are presentation, not
notification disposition or obligation state.

Use Zulip for mathematical questions, cross-cutting API decisions and findings
another owner needs. Horizon already records queue ownership, deliveries and
execution state; Forge holds PR-specific findings and their resolution. A build
or merge normally needs no second report in chat.

Start with `GET /api/v3/discussions?project_id=...`. Registered topics are only
part of the channel: use `/discussions/{id}/topics?q=...` or `/search?q=...` to
find earlier decisions in that authorized project channel. Register a relevant
remote topic through the existing channel before participating.

For a relevant registered topic, fetch
`GET /api/v3/discussions/{id}/messages?assignment_id={own-id}&unread_only=true`.
Read the returned bodies, including changed messages and deletion notices, and
record `read_revisions` with `POST /api/v3/messages/read`. Page while relevant
unread messages remain. Existing exact receipts are reused automatically when
replying: do not reconstruct a last-100-message receipt inventory. A first view
covers 20 recent messages and older direct mentions; use the returned history
link when understanding the question requires earlier discussion. A bounded
view is not a claim that earlier questions were resolved.
The first acknowledgment must include the current 20-message window together:
accumulate smaller initial pages before acknowledging. A concurrent arrival can
require adding its revision before that initial acknowledgment succeeds.
Preserve the response JSON and build the receipt from its `read_revisions` with
`jq`; never transcribe message UUIDs or revision numbers by hand. The
[read-then-reply example](references/discussions.md#read-then-reply) separates
reading message bodies from acknowledging those exact observed revisions.

Subscribe an assignment only to subjects it owns: a discussion, mission, node,
file, directory, or Forge item. Create or update the subscription through
`POST /api/v3/records/subscription`; `digest` means routine delivery at work
boundaries, not an automatically generated summary. Use `prompt` for direct
attention and `muted` to stop distraction. Reading or replying automatically
follows that topic without overriding an explicit mute or expiry. Link its
mission, directory or PR when creating it so subject subscriptions are useful.

Write only for a decision, blocker, question, durable handoff, or operator
direction. Keep the message concise, name the exact claim or file, and link the
relevant node, PR, commit, or assignment. Use the readable dashboard mention
form `@R12/A500` for a known assignment, `@N12` for a node, or
`@workspace:Math/Result.lean` for a repository slug and file. Mentions outside
code blocks notify scoped readers; examples inside code are not requests. Do not
post session diaries, repeated build logs, or acknowledgement chatter.

Ask the actual owner one concrete question, with the competing choices and
affected consumer. Continue independent work instead of routing the question
through a planner. Once settled, leave a short decision and rationale linking
its durable destination: an accepted PR, code documentation, source note or
project policy. Keep scope/revision and any remaining disagreement explicit;
do not duplicate artifact inventories or turn a chat summary into authority.
Use absolute external evidence URLs so links work from Zulip itself.

The reply outbox rechecks current content before sending. If a message changed,
read the new delta and reconcile the failed operation before submitting a
replacement. A successful receipt or outgoing message resolves no obligation.
An agent cannot bypass reading with `urgent: true`; that override is restricted
to the operator. Finish follow-up work or retain a named durable owner.

Use [horizon-efficiency](../horizon-efficiency/SKILL.md) to avoid reading broad
history and [horizon-start](../horizon-start/SKILL.md) for batched
control notices. Read [discussion requests](references/discussions.md) when
subscribing, opening a topic, or posting a reply.
