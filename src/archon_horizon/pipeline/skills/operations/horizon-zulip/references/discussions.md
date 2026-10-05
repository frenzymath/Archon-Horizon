# Scoped discussion requests

Examples below are JSON payload shapes: substitute real IDs and observed
revisions. Send them through `horizon-pipeline agent request`, which journals
mutations. Do not expose raw integration credentials.

## Subscribe and mute

`POST /api/v3/records/subscription` upserts one subject for the assignment:

```json
{
  "assignment_id": "<own-assignment-id>",
  "subject": {"kind": "file", "repository_id": "<repository-id>", "path": "Math/Result.lean"},
  "mode": "digest",
  "origin": "explicit"
}
```

For a node, use `subject: {"kind":"node","id":"<node-id>"}`. A directory
subscription is available when several owned files change together. Repeat the
same subject with `mode: "muted"` to stop deliveries. Optional `expires_at`
is a timezone-aware timestamp. Assignment-created mission/node subscriptions
are seeded automatically; explicit choices take precedence.

Link an existing discussion to its subject using `POST
/api/v3/discussion-subjects` with `discussion_id` and `subject`. Without this
link, a node/file subscription cannot infer the subject from ordinary prose.

## Read then reply

Fetch `GET /api/v3/discussions/{id}/messages?assignment_id={own-id}&unread_only=true`.
Each page contains at most 20 bodies, `read_revisions`, `unread_count`, a
`fingerprint`, and an optional `next_cursor`. Read the content, then acknowledge
those exact revisions, including tombstones, through `POST /api/v3/messages/read`:

```json
{"assignment_id":"<own-id>","messages":[{"id":"<message-id>","revision":3}]}
```

Save the response in your own disk-backed scratch space, then read its bodies:

```sh
horizon_page=$(mktemp "$TMPDIR/horizon-discussion.XXXXXX.json")
horizon-pipeline agent request GET "/api/v3/discussions/<discussion-id>/messages?assignment_id=$HORIZON_ASSIGNMENT_ID&unread_only=true&limit=20" > "$horizon_page"
jq '{items:[.items[]|{id,revision,body,deleted_at}],next_cursor,unread_count}' "$horizon_page"
printf '%s\n' "$horizon_page"
```

After reading those bodies, construct the receipt from the saved response.
Keep the printed file path if the next command runs in a new shell. Do not
retype UUIDs or omit an ID that looks unfamiliar:

```sh
horizon-pipeline agent request POST /api/v3/messages/read "$(jq -c --arg assignment "$HORIZON_ASSIGNMENT_ID" '{assignment_id:$assignment,messages:.read_revisions}' "$horizon_page")"
```

Do not combine the read and acknowledgment into an unattended command: fetching
JSON is not reading its content. If the initial window spans pages, first read
all those pages and accumulate their exact `read_revisions`; the command above
shows the single-page case. A topic with fewer than 20 messages requires all
available messages in its initial window, not 20 fabricated receipts.

Then `POST /api/v3/discussions/{id}/reply`:

```json
{
  "assignment_id": "<own-id>",
  "body": "@R12/A500 The source uses a uniform bound; the current statement only gives a pointwise bound. Can the shared definition expose the uniform constant?"
}
```

For first participation, acknowledge the current initial 20-message window in
one request. Accumulate smaller pages before acknowledging. If new arrivals
change that initial window, `incomplete_initial_discussion_read` returns a delta
link and records no partial receipts; read the new messages and include their
revisions with those already read. Subsequent delta pages can be acknowledged
individually. Re-fetch the first delta page until `unread_count` is zero.
Previously stored exact receipts are reused; `read_messages` is optional
on the reply. If supplied, its revisions must still be current. Reads are scoped
to the assignment even when several assignments share one Zulip account.
The initial window is the latest 20 messages plus older direct mentions; once
participating, all revisions since the earliest read message remain relevant.
Reading earlier history deliberately broadens that context. Edits and deletions
of already read messages reappear in the delta. The `history_url` gives full
paginated history; use it to understand unresolved older questions or an
explicitly cited message. Read receipts never mean a question was answered.

A valid reply submitted while synchronization is recovering is queued durably;
delivery waits for a current view and rechecks the frozen revisions and remote
message contents. New arrivals, old edits and unacknowledged deletions require a
new delta, not another history dump.
Do useful independent work rather than submitting duplicate replies. Keep the
returned outbox operation and inspect its outcome; enqueueing is not successful
delivery. Newly unread or edited messages still require reading and reconciliation.

## New topics

Reuse an existing topic for the same question. An agent can register a new topic
in an existing project channel using `POST /api/v3/discussions` with
`project_id`, `topic`, and `source_discussion_id`. Add `subjects` to link the
mission, directory or Forge item in the same request:

```json
{
  "project_id": "<project-id>",
  "source_discussion_id": "<existing-project-topic-id>",
  "topic": "Matrix inverse: scalar field generality",
  "subjects": [{"kind": "mission", "id": "<mission-id>"}]
}
```

The source discussion supplies the authorized integration/channel binding.
Arbitrary new channel binding is an administrator operation. Reading or replying
automatically follows the topic; explicit mute/expiry choices remain intact.
Native whole-topic rename events within the same channel preserve its Horizon
identity when the destination is not already registered. Partial moves, merges,
or a lost rename event need reconciliation; do not assume all subscriptions were
transferred to a new topic.

## Discover earlier decisions

`GET /api/v3/discussions/{id}/topics?q=metric&limit=20` lists remote topics in
that discussion's project channel, including unregistered history. Follow
`next_before` with `before=...`. For content search use
`GET /api/v3/discussions/{id}/search?q=uniform&topic=Bounds&limit=20`.
The `topic` filter is optional. Results contain bounded excerpts and absolute
Zulip links; they are discovery aids, not read receipts or accepted decisions.
Register the exact relevant topic as above and wait for its mirrored view to
be current before relying on it for a reply. Never broaden a search to another
project's channel using arbitrary credentials.

## Wait on a relevant discussion

When a discussion genuinely blocks the retained assignment, use its observed
`fingerprint` in a queue/checkpoint condition:

```json
{"version":1,"expression":{"op":"discussion_changed","discussion_id":"<discussion-id>","fingerprint":"<observed-64-character-hash>"}}
```

This observes new messages, edits, deletions and topic changes; it is not a
semantic claim that the question has been answered. Preserve the question and
its owner in an obligation. Combine with an appropriate deadline when needed,
and settle native children/build work before checkpointing. Follow the current
`checkpoint_assignment` command schema rather than inventing a polling loop.

Keep mathematical decisions in normal prose. Zulip inline mathematics uses
`$$...$$`; displayed mathematics uses a fenced `math` block. Build request JSON
with a serializer so backslashes and line breaks survive correctly.
