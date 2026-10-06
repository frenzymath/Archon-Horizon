"""Compare existing Radar-format measurements; never execute benchmark code."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re


def finite(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("measurement values must be finite numbers")
    try:
        result = float(value)
    except OverflowError as error:
        raise ValueError("measurement value exceeds finite numeric range") from error
    if not math.isfinite(result):
        raise ValueError("measurement values must be finite numbers")
    return result


def read_measurements(path: Path) -> dict[str, tuple[float, str | None]]:
    measurements: dict[str, tuple[float, str | None]] = {}
    with path.open(encoding="utf-8") as source:
        for number, line in enumerate(source, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("each measurement must be an object")
                metric, unit = row.get("metric"), row.get("unit")
                if not isinstance(metric, str) or not metric.strip():
                    raise ValueError("metric must be a nonempty string")
                if unit is not None and not isinstance(unit, str):
                    raise ValueError("unit must be a string or null")
                value = finite(row.get("value"))
                if metric in measurements:
                    previous, previous_unit = measurements[metric]
                    if unit != previous_unit:
                        raise ValueError(f"inconsistent units for {metric!r}")
                    value = finite(previous + value)
                measurements[metric] = (value, unit)
            except ValueError as error:
                raise ValueError(f"{path}:{number}: {error}") from error
    if not measurements:
        raise ValueError(f"{path}: no measurements")
    return measurements


def compare(base: dict, head: dict) -> list[dict]:
    rows = []
    for metric in sorted(base.keys() | head.keys()):
        before, base_unit = base.get(metric, (None, None))
        after, head_unit = head.get(metric, (None, None))
        delta = percent = None
        if before is None:
            status = "head_only"
        elif after is None:
            status = "base_only"
        elif base_unit != head_unit:
            status = "unit_mismatch"
        else:
            status = "compared"
            delta = finite(after - before)
            if before != 0:
                percent = finite((delta / abs(before)) * 100)
        rows.append({"metric": metric, "status": status,
                     "base": before, "head": after,
                     "base_unit": base_unit, "head_unit": head_unit,
                     "delta": delta, "percent_change": percent})
    return rows


def commit_oid(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value):
        raise argparse.ArgumentTypeError("use a full lowercase Git commit OID")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--head", type=Path, required=True)
    parser.add_argument("--base-commit", type=commit_oid, required=True)
    parser.add_argument("--head-commit", type=commit_oid, required=True)
    parser.add_argument("--context", required=True,
                        help="runner, benchmark revision, toolchain, targets and cache conditions")
    args = parser.parse_args()
    if not args.context.strip():
        parser.error("context must describe the measurement conditions")
    try:
        rows = compare(read_measurements(args.base), read_measurements(args.head))
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps({"schema_version": 1, "provenance": "caller_supplied",
                      "base_commit": args.base_commit, "head_commit": args.head_commit,
                      "context": args.context, "metrics": rows,
                      "limitations": "Numeric comparison only; verify run success, provenance, "
                      "comparability and metric direction before drawing conclusions."},
                     indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
