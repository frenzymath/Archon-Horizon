"""Development inventory must expose nested scope and preserve missing evidence."""

import importlib.util
from pathlib import Path


def load_script():
    path = Path(__file__).resolve().parent.parent / "scripts/review_inventory.py"
    spec = importlib.util.spec_from_file_location("review_inventory", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_nested_functions_are_separate_and_evidence_is_explicit(tmp_path):
    module = load_script()
    source = tmp_path / "src"
    source.mkdir()
    (source / "sample.py").write_text('''"""A sample."""
class Reader:
    async def read(self):
        """Read one item."""
        def helper():
            if True:
                return 1
        # This choice belongs to the outer function.
        if True:
            return helper()
''')
    report = module.scan(tmp_path, source, {"files": {"src/sample.py": {"executed_lines": [3, 9, 10], "missing_lines": [5, 6, 7]}}},
                         {"src/sample.py": {"reviewed_functions": ["Reader.read"]}})
    outer, inner = report["files"][0]["functions"]
    assert outer["name"] == "Reader.read" and inner["name"] == "Reader.read.helper"
    assert outer["async"] and outer["docstring"] and outer["reviewed"]
    assert not inner["docstring"] and not inner["reviewed"]
    assert outer["branch_constructs"] == 1 and inner["branch_constructs"] == 1
    assert outer["comment_lines"] == 1
    assert outer["coverage"]["percent"] == 50.0
    assert inner["coverage"]["percent"] == 0.0
    assert "Reader.read.helper" in module.markdown_report(report)
    assert module.scan(tmp_path, source)["files"][0]["functions"][0]["coverage"] is None


def test_syntax_errors_and_generated_trees_are_not_silently_hidden(tmp_path):
    module = load_script()
    source = tmp_path / "src"
    source.mkdir()
    (source / "broken.py").write_text("def invalid(:\n")
    generated = source / "node_modules"
    generated.mkdir()
    (generated / "omit.py").write_text("def hidden(): pass\n")
    report = module.scan(tmp_path, source)
    assert report["summary"]["files"] == 1
    assert report["summary"]["parse_errors"] == 1
    assert report["files"][0]["parse_error"]
