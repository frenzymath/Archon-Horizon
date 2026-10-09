"""Exercise the optional commit hook against temporary message files only."""

import os
from pathlib import Path
import subprocess

import pytest


HOOK = Path(__file__).parents[1] / ".githooks" / "commit-msg"


def invoke(message, *, helper=None, helper_body="exit 1\n"):
    environment = dict(os.environ)
    if helper:
        binary = message.parent / "helpers"
        binary.mkdir()
        executable = binary / helper
        executable.write_text("#!/bin/sh\n" + helper_body)
        executable.chmod(0o700)
        environment["PATH"] = str(binary) + os.pathsep + os.defpath
    return subprocess.run(["sh", str(HOOK), str(message)], env=environment,
                          capture_output=True, text=True, timeout=10)


def test_hook_filters_documented_attribution_and_keeps_interior_blank_lines(tmp_path):
    message = tmp_path / "commit message"
    message.write_text("Title\n\nUseful explanation.\n\n"
        "Co-authored-by: Human <human@example.invalid>\n"
        "  cO-AuThOrEd-By: Claude <claude@example.invalid>\n"
        "Co-authored-by: Teammate at Anthropic <person@example.invalid>\n"
        "Generated with [Claude Code](https://example.invalid)\n"
        "gEnErAtEd WiTh Claude Code\n"
        "  🤖 Generated with another assistant\n\n\n")
    result = invoke(message)
    assert result.returncode == 0
    assert message.read_text() == ("Title\n\nUseful explanation.\n\n"
                                   "Co-authored-by: Human <human@example.invalid>\n")
    assert not list(tmp_path.glob("*.horizon.*"))


@pytest.mark.parametrize("helper, body", [
    ("awk", "printf 'partial transformed output\\n'\nexit 1\n"),
    ("mv", "exit 1\n"),
    ("mktemp", "exit 1\n"),
])
def test_hook_preserves_original_and_cleans_scratch_when_a_helper_fails(tmp_path, helper, body):
    message = tmp_path / "COMMIT_EDITMSG"
    original = b"Keep this message\n\nGenerated with Claude Code\n\n"
    message.write_bytes(original)
    result = invoke(message, helper=helper, helper_body=body)
    assert result.returncode == 0
    assert message.read_bytes() == original
    assert not list(tmp_path.glob("*.horizon.*"))


def test_hook_can_remove_all_attribution_without_treating_empty_output_as_failure(tmp_path):
    message = tmp_path / "COMMIT_EDITMSG"
    message.write_text("Generated with Claude Code\n\n")
    assert invoke(message).returncode == 0
    assert message.read_bytes() == b""
    assert not list(tmp_path.glob("*.horizon.*"))


def test_hook_ignores_missing_message_paths(tmp_path):
    message = tmp_path / "missing"
    assert invoke(message).returncode == 0
    assert not message.exists()
