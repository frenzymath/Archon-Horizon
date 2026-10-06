"""The bundled comparison tool must not turn missing evidence into improvements."""

import base64
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

from archon_horizon.pipeline.bundles import skill_files


SCRIPT = Path(__file__).parents[1] / "src/archon_horizon/pipeline/skills/lean/lean-performance/scripts/compare_measurements.py"
spec = importlib.util.spec_from_file_location("review_measurements", SCRIPT)
measurement_tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(measurement_tool)


def measurements(tmp_path, name, rows):
    path = tmp_path / name
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    return path


def test_sums_within_run_and_preserves_missing_zero_and_mismatched_metrics(tmp_path):
    base = measurements(tmp_path, "base.jsonl", [
        {"metric": "build", "value": 2, "unit": "s"},
        {"metric": "build", "value": 3, "unit": "s"},
        {"metric": "zero", "value": 0},
        {"metric": "removed", "value": 8},
        {"metric": "units", "value": 10, "unit": "B"},
    ])
    head = measurements(tmp_path, "head.jsonl", [
        {"metric": "build", "value": 6, "unit": "s"},
        {"metric": "zero", "value": 2},
        {"metric": "added", "value": 4},
        {"metric": "units", "value": 20, "unit": "s"},
    ])
    rows = {row["metric"]: row for row in measurement_tool.compare(
        measurement_tool.read_measurements(base), measurement_tool.read_measurements(head))}
    assert rows["build"]["base"] == 5
    assert rows["build"]["delta"] == 1
    assert rows["build"]["percent_change"] == 20
    assert rows["zero"]["delta"] == 2
    assert rows["zero"]["percent_change"] is None
    for metric, status in (("removed", "base_only"), ("added", "head_only"), ("units", "unit_mismatch")):
        assert rows[metric]["status"] == status
        assert rows[metric]["delta"] is None
        assert rows[metric]["percent_change"] is None


@pytest.mark.parametrize("rows", [
    [], [{"metric": "x", "value": True}], [{"metric": "x", "value": "2"}],
    [{"metric": "x", "value": float("nan")}], [{"metric": "x", "value": float("inf")}],
    [{"metric": "x", "value": 2, "unit": 1}], [{"metric": "", "value": 2}], [1],
    [{"metric": "x", "value": 1, "unit": "s"}, {"metric": "x", "value": 1, "unit": "B"}],
    [{"metric": "x", "value": 1e308}, {"metric": "x", "value": 1e308}],
])
def test_rejects_invalid_or_ambiguous_measurements(tmp_path, rows):
    path = measurements(tmp_path, "invalid.jsonl", rows)
    with pytest.raises(ValueError, match="invalid.jsonl"):
        measurement_tool.read_measurements(path)


def test_bundled_script_runs_standalone_and_does_not_modify_inputs(tmp_path):
    bundled = skill_files()["files"]["lean/lean-performance/scripts/compare_measurements.py"]
    script = tmp_path / "compare_measurements.py"
    script.write_bytes(base64.b64decode(bundled["content_base64"]))
    path = measurements(tmp_path, "measurements.jsonl", [{"metric": "build", "value": 4, "unit": "s"}])
    before = path.read_bytes()
    command = [sys.executable, "-I", "-B", str(script), "--base", str(path), "--head", str(path),
               "--base-commit", "a" * 40, "--head-commit", "b" * 40, "--context", "fixture runner"]
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    report = json.loads(result.stdout)
    assert report["provenance"] == "caller_supplied"
    assert report["base_commit"] == "a" * 40
    assert report["metrics"][0]["delta"] == 0
    assert path.read_bytes() == before
    path.write_text("invalid json", encoding="utf-8")
    failed = subprocess.run(command, capture_output=True, text=True)
    assert failed.returncode == 2
    assert failed.stdout == ""
    assert "measurements.jsonl:1" in failed.stderr
