# Repository progress

Node Markdown and frontmatter in the knowledge repository remain authoritative.
Use the same node keys and progress vocabulary for every workspace or library.
`implementations` maps registered repository UUIDs to these fields:

| Field | Meaning |
| --- | --- |
| `status` | `open`, `in_progress`, `informal_stated`, `proof_sketch`, `informal_proved`, `formally_stated`, `formally_proved`, `conditionally_proved`, `kernel_checked` |
| `review` | `pending`, `statement_accepted`, `changes_requested`, `accepted` |
| `node_sha256` | Digest of the node content and this target's dependency list |
| `commit_oid` | Full immutable implementation repository commit; required for proof claims |
| `evidence_node_commit_oid` | Alternatively, an immutable knowledge-repository revision of this node containing the original proof evidence and its pins; preserves historical claims without inventing a new implementation pin |
| `declarations` | Lean declaration names at that implementation commit |
| `children` | Optional target-specific prerequisite keys; omission uses the node's shared `children` |

Missing target records mean open/pending, never inherited workspace completion.
These are evidence-backed source claims, not automatic mathematical certification.
An evidence-node reference preserves an earlier claim; it does not certify a
new implementation. Inspect its original proof pins before reusing that evidence.
An accepted statement with `sorry` is formally_stated/statement_accepted, not
formally_proved. Track its admissions and an actual proof owner.

For example (replace the example UUID with the registered target):

```yaml
implementations:
  12345678-1234-1234-1234-123456789abc:
    status: open
    review: pending
    children: [metric, connection]
```

After editing the content or target children, calculate the digest:

```sh
python -m archon_horizon.pipeline.graph_progress nodes/example.md --repository-id REPOSITORY_UUID
```

Set `node_sha256` to that output when recording assessed progress. All non-open
progress and non-pending review require it. The read API also returns
`node_sha256` for the selected target's currently indexed node. Do not reuse that
value after editing the node content. Never hand-write `_horizon_*` fields; those
are derived index metadata. A changed digest makes the old record historical:
the UI displays Needs Reassessment and pending review until evidence is renewed.
Changing status/evidence alone does not change the digest.

Replace outdated text instead of accumulating old/new proofs in one document.
Keep implementation pins and Git history. A statement replacement must be
reviewed against the new content; the previous proof may prove only the old
statement. Preserve the original frozen main-run snapshot. Targets may use
different dependency lists; each must be complete and acyclic within this graph.
Remove unused nodes in Git after repairing incoming edges and recording successors
where useful. Removed files leave the current graph, while historical catalog IDs
remain available for old missions and evidence.

Workers propose knowledge-repository edits through its existing PR policy.
Maintainers reconcile status with actual library commits, resolved reviews and
consumer checks. A merged supporting PR does not automatically complete a milestone.
Use the graph to feed ready family ports; do not turn bookkeeping into another
whole-project prerequisite or reopen already completed code work merely because
its graph mapping is missing.
