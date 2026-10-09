#!/usr/bin/env python3
"""Inventory Python review targets without importing or executing application code.

Counts are descriptive heuristics, not a correctness score. Optional coverage.py
JSON and explicit review ledgers report evidence separately; absent evidence is
unknown. Non-Python semantics require the language's own parser and review tools.
"""

from __future__ import annotations

import argparse
import ast
import io
import json
from pathlib import Path
import tokenize


ROOT = Path(__file__).resolve().parent.parent
EXCLUDED = {"node_modules", "__pycache__", ".venv", ".git", "build", "dist"}
BRANCHES = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.IfExp, ast.Match)


def metrics(node: ast.AST) -> tuple[int, int]:
    """Count decision constructs/nesting, excluding nested callable definitions.

This is not cyclomatic complexity: boolean operators and exception handlers have
different meanings, and calls may hide substantial complexity in other modules.
"""
    branches = maximum = 0

    def visit(current: ast.AST, depth: int) -> None:
        nonlocal branches, maximum
        if current is not node and isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            return
        if isinstance(current, BRANCHES):
            branches += 1
            depth += 1
            maximum = max(maximum, depth)
        for child in ast.iter_child_nodes(current):
            visit(child, depth)

    visit(node, 0)
    return branches, maximum


def functions(tree: ast.AST):
    """Yield methods and nested functions with stable lexical scope names."""
    def descend(node: ast.AST, scope: tuple[str, ...]):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = (*scope, child.name)
                yield ".".join(name), child
                yield from descend(child, name)
            elif isinstance(child, ast.ClassDef):
                yield from descend(child, (*scope, child.name))
            else:
                yield from descend(child, scope)

    yield from descend(tree, ())


def scan(root: Path, source: Path, coverage: dict | None = None, ledger: dict | None = None) -> dict:
    """Return complete file/function rows, retaining parse failures as review gaps."""
    files = []
    coverage_files = (coverage or {}).get("files", {})
    for path in sorted(source.rglob("*.py")):
        if EXCLUDED.intersection(path.relative_to(source).parts) or path.is_symlink():
            continue
        relative = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8")
        row = {"path": relative, "lines": len(text.splitlines()), "functions": [], "parse_error": None}
        files.append(row)
        try:
            tree = ast.parse(text, filename=relative)
            comments = {token.start[0] for token in tokenize.generate_tokens(io.StringIO(text).readline)
                        if token.type == tokenize.COMMENT}
        except (SyntaxError, tokenize.TokenError) as error:
            row["parse_error"] = str(error)
            continue
        row["module_docstring"] = bool(ast.get_docstring(tree))
        row["comment_lines"] = len(comments)
        review = (ledger or {}).get(relative, {})
        reviewed = set(review.get("reviewed_functions", []))
        observed = coverage_files.get(relative, coverage_files.get(str(path), None))
        executed = set(observed.get("executed_lines", [])) if observed is not None else set()
        missing = set(observed.get("missing_lines", [])) if observed is not None else set()
        for name, node in functions(tree):
            start, end = node.lineno, node.end_lineno or node.lineno
            # Parent span includes nested definitions: coverage is intentionally
            # labelled span coverage rather than attribution to one callable.
            covered = {line for line in executed if start <= line <= end}
            uncovered = {line for line in missing if start <= line <= end}
            branches, nesting = metrics(node)
            row["functions"].append({"name": name, "line": start, "end_line": end,
                                     "lines": end - start + 1, "async": isinstance(node, ast.AsyncFunctionDef),
                                     "docstring": bool(ast.get_docstring(node)),
                                     "comment_lines": len({line for line in comments if start <= line <= end}),
                                     "branch_constructs": branches, "max_branch_nesting": nesting,
                                     "reviewed": name in reviewed or f"{name}:{start}" in reviewed,
                                     "coverage": None if observed is None else {
                                         "executed_span_lines": len(covered), "missing_span_lines": len(uncovered),
                                         "percent": round(100 * len(covered) / (len(covered) + len(uncovered)), 1)
                                         if covered or uncovered else None}})
        row["review_notes"] = review.get("notes")
    all_functions = [item for row in files for item in row["functions"]]
    return {"schema_version": 1, "source": source.relative_to(root).as_posix(),
            "limits": "Python AST metrics and optional line evidence; not a correctness or performance assessment.",
            "summary": {"files": len(files), "parse_errors": sum(row["parse_error"] is not None for row in files),
                        "functions": len(all_functions), "with_docstrings": sum(item["docstring"] for item in all_functions),
                        "explicitly_reviewed": sum(item["reviewed"] for item in all_functions)},
            "files": files}


def markdown_report(report: dict) -> str:
    """Render every row without hiding low-complexity or undocumented functions."""
    summary = report["summary"]
    lines = ["# Python review inventory", "", report["limits"], "",
             f"Files: {summary['files']}; functions: {summary['functions']}; docstrings: {summary['with_docstrings']}; "
             f"explicitly reviewed: {summary['explicitly_reviewed']}; parse errors: {summary['parse_errors']}.", ""]
    for row in report["files"]:
        lines.extend([f"## `{row['path']}`", "", f"{row['lines']} lines.", ""])
        if row["parse_error"]:
            lines.extend([f"Parse error: {row['parse_error']}", ""])
            continue
        if not row["functions"]:
            lines.extend(["No function definitions.", ""])
            continue
        lines.extend(["| Function | Line | Lines | Docstring | Comments | Branches | Nesting | Reviewed | Span coverage |",
                      "| --- | ---: | ---: | --- | ---: | ---: | ---: | --- | --- |"])
        for item in row["functions"]:
            coverage = item["coverage"]
            percent = f"{coverage['percent']}%" if coverage and coverage["percent"] is not None else "unknown"
            lines.append(f"| `{item['name']}` | {item['line']} | {item['lines']} | {'yes' if item['docstring'] else 'no'} "
                         f"| {item['comment_lines']} | {item['branch_constructs']} | {item['max_branch_nesting']} "
                         f"| {'yes' if item['reviewed'] else 'no'} | {percent} |")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--source", type=Path, default=Path("src/archon_horizon"))
    parser.add_argument("--coverage", type=Path, help="coverage.py JSON report (optional)")
    parser.add_argument("--review-ledger", type=Path, help="explicit per-path reviewed_functions JSON (optional)")
    parser.add_argument("--format", choices=["json", "markdown"], default="json")
    parser.add_argument("--output", type=Path, help="write output here instead of stdout")
    args = parser.parse_args()
    root = args.root.resolve()
    source = (root / args.source).resolve()
    if not source.is_dir() or not source.is_relative_to(root):
        parser.error("--source must be a directory within --root")
    try:
        coverage = json.loads(args.coverage.read_text()) if args.coverage else None
        ledger = json.loads(args.review_ledger.read_text()) if args.review_ledger else None
        if coverage is not None and not isinstance(coverage.get("files"), dict):
            parser.error("Coverage JSON must contain a files object.")
        if ledger is not None and not isinstance(ledger, dict):
            parser.error("Review ledger must be a path-keyed object.")
        report = scan(root, source, coverage, ledger)
    except (ValueError, OSError, UnicodeError, AttributeError, TypeError) as error:
        parser.error(str(error))
    content = markdown_report(report) if args.format == "markdown" else json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(content)
    else:
        print(content, end="" if content.endswith("\n") else "\n")
    return int(report["summary"]["parse_errors"] > 0)


if __name__ == "__main__":
    raise SystemExit(main())
