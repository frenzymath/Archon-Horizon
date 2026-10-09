#!/usr/bin/env python3
"""Generate the public dashboard's small, deterministic, entirely synthetic data.

This deliberately has no API client, database connection, or snapshot/import
option. Public documentation must never acquire operator records or credentials.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = ROOT / "src/archon_horizon/frontend/src/showcase/fixture.json"
STAMP = "2026-10-01T10:00:00Z"


def instruction_catalog() -> tuple[dict, list[dict]]:
    """Select public bundled instructions without reading an operator catalog."""
    skills = [
        {"name": name, "description": description, "category": category,
         "path": f"skills/{category}/{name}/SKILL.md", "resources": []}
        for category, name, description in [
            ("operations", "horizon-pipeline", "Coordinate formalization assignments and phase delivery."),
            ("operations", "horizon-delegation", "Delegate independently scoped work and collect its evidence."),
            ("operations", "horizon-graph", "Inspect and update source-bound dependency graphs."),
            ("lean", "horizon-formalization", "Develop and verify Lean formalizations."),
            ("lean", "lean-check", "Check Lean sources in their pinned project toolchain."),
            ("review", "horizon-review", "Review proposals and reconcile findings and repairs."),
        ]
    ]
    subagents = [
        {"slug": name, "description": description, "category": category,
         "source_path": f"subagents/{category}/{name}.md", "skills": selected_skills}
        for category, name, description, selected_skills in [
            ("implementation", "lean-worker", "Implement a scoped Lean proof and report verification.", ["horizon-formalization", "lean-check"]),
            ("validation", "build-checker", "Check the exact source revision and report build evidence.", ["lean-check"]),
            ("reviewers", "mathematical-fidelity", "Review mathematical meaning and hypotheses.", ["horizon-review"]),
        ]
    ]
    prompts = [{"name": "Planning mission", "description": "The bundled objective-planning mission template.",
                "category": "prompts", "path": "prompts/objective/planning-mission.md"}]
    files = []
    for item in [*skills, *subagents, *prompts]:
        path = item.get("path") or item["source_path"]
        source = path.replace("prompts/", "instructions/templates/", 1)
        content = (ROOT / "src/archon_horizon/pipeline" / source).read_text("utf-8")
        files.append({"path": path, "content": content,
                      "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()})
    return {"revision": "synthetic-catalog-1", "skills": skills, "subagents": subagents,
            "prompts": prompts, "files": [item["path"] for item in files]}, files


def fixture() -> dict:
    """Build a reproducible project showing objective, graph, work and references."""
    project = {"id": "demo-project", "number": 1, "revision": 1,
               "slug": "finite-sums", "title": "Finite sums",
               "description": "A synthetic project: prove and package the sum of the first n natural numbers."}
    objective = {"id": "demo-objective", "title": "Formalize finite sums", "revision": 1,
                 "created_at": STAMP, "updated_at": STAMP,
                 "markdown": "# Direction\n\n- [x] State the finite-sum theorem.\n- [x] Prove the induction helper.\n- [ ] Complete the main theorem.\n- [ ] Package a small reusable library.\n\nThis is invented demonstration data, not a verified formalization.\n\nThe graph records local prerequisites; this objective records the overall direction."}
    nodes = [
        {"id": "sum-formula", "label": "M1", "title": "Sum of the first n natural numbers",
         "kind": "claim", "status": "open", "labels": ["milestone", "formally_stated"],
         "children": ["sum-step", "sum-zero"],
         "markdown": "For every natural number n, twice the sum of integers below n is n(n−1).\n\nThe synthetic main milestone depends on the base case and induction helper."},
        {"id": "sum-step", "label": "sum-step", "title": "Induction step",
         "kind": "claim", "status": "open", "labels": ["formally_proved"], "children": ["sum-zero"],
         "markdown": "Separate the final summand and simplify the polynomial expression.\n\nIn a real project, proof labels must be backed by current source evidence."},
        {"id": "sum-zero", "label": "sum-zero", "title": "Empty sum",
         "kind": "claim", "status": "open", "labels": ["formally_proved"], "children": [],
         "markdown": "The sum of an empty finite set is zero."},
    ]
    for node in nodes:
        node.update(created_at=STAMP, updated_at=STAMP)
    usage = {"tokens_in": 12000, "tokens_out": 2400, "cost_usd": None}
    sessions = [
        {"id": "demo-worker", "label": "2", "session_number": 2,
         "title": "Complete the finite-sum theorem", "role": "worker", "status": "running",
         "profile_id": "worker", "elapsed_seconds": 180, "agent_seconds": 180, "attempt_count": 1,
         "native": False, "can_resume": False, "context": {}, "usage": usage,
         "models": ["example-model"], "efforts": ["medium"], "skills": [], "subagents": []},
        {"id": "demo-maintainer", "label": "3", "session_number": 3,
         "title": "Review the reusable library interface", "role": "maintainer", "status": "queued",
         "profile_id": "maintainer", "queue_position": 1, "native": False, "can_resume": False,
         "context": {"admission": {"state": "waiting", "summary": "Wait for the library proposal",
                                   "reason": "The author has not requested review yet.", "checked_at": STAMP}},
         "usage": {"tokens_in": None, "tokens_out": None, "cost_usd": None}, "subagents": []},
    ]
    for session in sessions:
        session.update(revision=1, run_id="demo-run", created_at=STAMP, started_at=STAMP,
                       host_id="example-host", updated_at=STAMP)
    sessions.append({**sessions[0], "id": "demo-completed", "label": "1", "session_number": 1,
                     "title": "Prove the empty-sum case", "status": "succeeded", "finished_at": STAMP})
    run = {"id": "demo-run", "number": 1, "project_id": project["id"], "project_title": project["title"],
           "mission_id": "demo-mission", "title": "Formalize finite sums", "phase": {"kind": "formalization"},
           "status": "running", "revision": 1, "created_at": STAMP, "started_at": STAMP,
           "session_count": 3, "total_session_count": 3, "subagent_count": 0,
           "main_session_count": 3, "delegated_session_count": 0, "native_subagent_count": 0,
           "usage": usage, "elapsed_seconds": 180, "agent_seconds": 180,
           "models": ["example-model"], "context": {}, "slot_capacity": 3}
    events = [{"id": "demo-event", "kind": "agent_message", "title": "The induction helper is ready.",
               "created_at": STAMP, "links": [], "data": {"text": "The induction helper is ready. Next: connect it to the main theorem."}}]
    report = {"id": "demo-report", "revision": 1, "kind": "context", "created_at": STAMP,
              "markdown": "The helper is complete; the main theorem remains in progress.",
              "ledger": {"mission": "Complete the **finite-sum theorem**.",
                         "acceptance_criteria": ["Check the theorem in the project toolchain."],
                         "items": [{"id": "demo-open", "number": 1, "description": "Connect the **induction step**.",
                                    "status": "open", "comments": [{"markdown": "Keep the public statement small.", "created_at": STAMP}]},
                                   {"id": "demo-done", "number": 2, "description": "Prove the empty-sum case.", "status": "completed"}]}}
    reference = {"id": "demo-reference", "project_id": project["id"], "cite_key": "synthetic_finite_sums_2026",
                 "kind": "book", "title": "A synthetic introduction to finite sums", "authors": ["Example Author"],
                 "issued_year": 2026, "venue": "Demonstration Library", "identifiers": {}, "urls": [],
                 "abstract": "Invented bibliography data demonstrating the reference catalog; this is not a published work.",
                 "metadata_source": "manual", "status": "active", "revision": 1, "created_at": STAMP, "updated_at": STAMP}
    catalog, instruction_files = instruction_catalog()
    hosts = [
        {"id": "example-host", "revision": 1, "slug": "proof-worker", "display_name": "Proof worker",
         "mode": "enabled", "workspace_root": "/example/workspaces", "scratch_root": "/example/scratch"},
        {"id": "example-review-host", "revision": 1, "slug": "review-worker", "display_name": "Review worker",
         "mode": "draining", "workspace_root": "/example/reviews", "scratch_root": "/example/review-scratch"},
    ]
    harnesses = [
        {"id": "demo-codex", "revision": 1, "slug": "codex-example", "adapter": "codex_exec",
         "provider_version": "synthetic", "enabled": True,
         "model_options": {"model": "example-model", "reasoning_effort": "medium"}},
        {"id": "demo-claude", "revision": 1, "slug": "claude-example", "adapter": "claude_exec",
         "provider_version": "synthetic", "enabled": True, "model_options": {"model": "example-model"}},
    ]
    reviewers = [{"id": "demo-reviewer", "revision": 1, "project_id": project["id"],
                  "slug": "mathematical-fidelity", "enabled": True, "invocation": "subrequest",
                  "functions": ["reviewer"], "harness_id": "demo-codex", "model_options": {},
                  "instructions": "Review the finite-sum statement and preserve its hypotheses. This is a synthetic reviewer configuration."}]
    resources = {"hosts": [
        {"id": hosts[0]["id"], "name": hosts[0]["display_name"], "mode": "enabled", "status": "enabled",
         "slots": 4, "occupied_slots": 1, "heartbeat_at": STAMP, "detail": "One synthetic proof session; three free slots."},
        {"id": hosts[1]["id"], "name": hosts[1]["display_name"], "mode": "draining", "status": "draining",
         "slots": 2, "occupied_slots": 1, "heartbeat_at": STAMP, "detail": "Finishing a synthetic review; no new sessions admitted."},
    ], "storage": [{"category": "workspaces", "bytes": 2147483648, "protected_bytes": 1610612736,
                    "reclaimable_bytes": 536870912}], "free_bytes": 68719476736, "observed_at": STAMP,
        "oldest_pending_delivery": None, "backup_status": "synthetic snapshot", "provider_status": "demo only"}
    integrations = [{"id": f"demo-{kind}", "kind": kind, "enabled": True,
                     "public_url": f"./integrations/{kind}.html", "browser_session": False}
                    for kind in ["forge", "zulip"]]
    return {"schema_version": 2, "generated_at": STAMP, "project": project, "objective": objective,
            "account": {"id": "demo-administrator", "username": "demo-admin", "role": "admin",
                        "permissions": {"admin": True, "write": False}},
            "hosts": hosts, "harnesses": harnesses, "reviewers": reviewers, "resources": resources,
            "integrations": integrations, "instruction_catalog": catalog, "instruction_files": instruction_files,
            "nodes": nodes, "run": run, "sessions": sessions, "events": events, "report": report,
            "references": [reference], "bibtex": "@book{synthetic_finite_sums_2026,\n  title = {A synthetic introduction to finite sums},\n  author = {Example Author},\n  year = {2026},\n  note = {Invented demonstration data}\n}\n"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true", help="fail if the checked-in fixture differs")
    args = parser.parse_args()
    content = json.dumps(fixture(), ensure_ascii=False, indent=2) + "\n"
    if args.check:
        if not args.output.is_file() or args.output.read_text() != content:
            parser.exit(1, "Dashboard fixture is stale; run scripts/generate_dashboard_fixture.py.\n")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
