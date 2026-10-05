from __future__ import annotations

import pytest

from archon_horizon.pipeline.documents import parse_document


def test_frontmatter_preserves_body_and_normalizes_dates():
    metadata, body = parse_document("---\r\ntitle: Target\r\ndate: 2026-09-29\r\n---\r\n\r\n# Proof\r\n")
    assert metadata == {"title": "Target", "date": "2026-09-29"}
    assert body == "# Proof\r\n"
    assert parse_document("# Plain markdown\n") == ({}, "# Plain markdown\n")


@pytest.mark.parametrize("document", [
    "---\ntitle: Unclosed\n",
    "---\n- not a mapping\n---\n",
    "---\n1: non-string key\n---\n",
    "---\nloop: &loop [*loop]\n---\n",
    "---\nobject: !!python/object/apply:os.system ['false']\n---\n",
])
def test_frontmatter_rejects_invalid_or_executable_metadata(document):
    with pytest.raises(ValueError):
        parse_document(document)
