from __future__ import annotations

import copy
from types import SimpleNamespace

from archon_horizon.server.service import WorkspaceService


def _session_tree() -> dict:
    return {
        "session": "0001-horizon-T",
        "status": "running",
        "last_at": "",
        "meta": {"role": "horizon", "round": 0},
        "usage": {},
        "children": [{
            "session": "0001-audit",
            "status": "running",
            "last_at": "",
            "meta": {"role": "subagent"},
            "usage": {},
            "children": [],
        }],
    }


def test_live_parent_keeps_native_child_running_but_stale_parent_interrupts_it() -> None:
    service = object.__new__(WorkspaceService)
    run = SimpleNamespace(id="0001", sessions=lambda: [object()])
    tree = _session_tree()
    service._session_state = lambda *_args: copy.deepcopy(tree)
    service._baseline_session_state = lambda *_args: None

    live = service._run_state(run, {}, [], process_live=True)
    assert live["sessions"][0]["status"] == "running"
    assert live["sessions"][0]["children"][0]["status"] == "running"

    stopped = service._run_state(
        run,
        {},
        [{"type": "run.stopped", "data": {"run_id": "0001"}}],
        process_live=False,
    )
    assert stopped["sessions"][0]["status"] == "interrupted"
    assert stopped["sessions"][0]["children"][0]["status"] == "interrupted"


def test_session_cache_normalizes_keys_and_drops_missing_terminal(tmp_path) -> None:
    """Abs/rel key duplicates collapse; gone terminal sessions are pruned."""
    from pathlib import Path
    import json
    from archon_horizon.server.service import WorkspaceService, _SESSION_CACHE_VERSION

    ws = tmp_path / "ws"
    (ws / ".archon-horizon" / "cache").mkdir(parents=True)
    (ws / "config.yaml").write_text(
        "workspace:\n  name: w\n  horizon_agent: {harness: hor}\n"
        "harnesses:\n  hor: {kind: \"null\"}\nprojects: {}\n",
        "utf-8",
    )
    live_session = ws / ".archon-horizon" / "runs" / "0001" / "sessions" / "s"
    live_session.mkdir(parents=True)
    (live_session / "meta.json").write_text(json.dumps({"status": "completed"}), "utf-8")
    (live_session / "transcript.jsonl").write_text("", "utf-8")

    rel = ".archon-horizon/runs/0001/sessions/s"
    abs_key = str(live_session)
    gone_rel = ".archon-horizon/runs/0099/sessions/gone"
    cache = {
        "version": 3,
        "sessions": {
            abs_key: {"sig": [1, 1, 1, 1], "node": {"status": "running", "session": "s"}},
            rel: {"sig": [2, 2, 2, 2], "node": {"status": "completed", "session": "s"}},
            gone_rel: {"sig": [3, 3, 3, 3], "node": {"status": "completed", "session": "gone"}},
        },
    }
    (ws / ".archon-horizon" / "cache" / "session-states.json").write_text(
        json.dumps(cache), "utf-8",
    )

    service = WorkspaceService(ws)
    keys = list(service._session_cache)
    assert keys == [rel]
    assert service._session_cache[rel]["node"]["status"] == "completed"
    # Dirty so the next save rewrites at the new version.
    assert service._session_cache_dirty is True
    service._save_session_cache(min_interval_s=0.0)
    saved = json.loads(
        (ws / ".archon-horizon" / "cache" / "session-states.json").read_text("utf-8")
    )
    assert saved["version"] == _SESSION_CACHE_VERSION
    assert list(saved["sessions"]) == [rel]
