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
