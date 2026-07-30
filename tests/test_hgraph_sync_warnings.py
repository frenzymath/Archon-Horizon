"""Regression coverage for the current vendored hgraph sync diagnostics."""

from __future__ import annotations

from pathlib import Path

from archon_horizon.hgraph.cli import main
from archon_horizon.hgraph.dashboard import number_document
from archon_horizon.hgraph.sync import parse_document


BLUEPRINT = r"""
\begin{document}
\chapter{Warnings}
\begin{lemma}[Attached]
\label{attached}
\lean{Project.attached}\leanok
This declaration is attached.
\end{lemma}

\begin{lemma}[Missing dependency]
\label{bad-dependency}
\uses{missing-label}
This dependency does not exist.
\end{lemma}

\begin{lemma}[Bad status]
\label{bad-status}
\leanok
This status has no declaration.
\end{lemma}

\begin{lemma}[Unlabeled]
\lean{Project.unlabeled}
This statement cannot become a graph node.
\end{lemma}

\begin{proof}
\proves{ghost}
This proof points to no statement.
\end{proof}
\end{document}
"""


LEAN = """namespace Project
theorem attached : True := by trivial
theorem orphan : True := by trivial
private theorem privateHelper : True := by trivial
end Project
"""


def _warning_project(root: Path) -> None:
    (root / "blueprint").mkdir(parents=True)
    (root / "Lean").mkdir()
    (root / "hgraph").mkdir()
    (root / "blueprint" / "blueprint.tex").write_text(BLUEPRINT, encoding="utf-8")
    (root / "Lean" / "Project.lean").write_text(LEAN, encoding="utf-8")
    (root / "hgraph" / "config.yaml").write_text(
        "blueprint: blueprint/blueprint.tex\nlean: [Lean]\n", encoding="utf-8"
    )


def test_sync_groups_new_warning_classes_and_ignores_private_helpers(
    tmp_path: Path, capsys
) -> None:
    _warning_project(tmp_path)

    assert main(["--root", str(tmp_path), "sync", "--color", "never"]) == 0
    report = capsys.readouterr().out

    assert "Lean declarations not attached to blueprint nodes" in report
    assert "Project.orphan" in report
    assert "Project.privateHelper" not in report
    assert "blueprint dependencies not found" in report
    assert "missing-label" in report
    assert "blueprint structure issues" in report
    assert "unlabeled blueprint statement" in report
    assert r"\proves{ghost}" in report
    assert "blueprint formalization status inconsistencies" in report
    assert r"\leanok present but no \lean{...}" in report


def test_document_numbering_handles_starred_chapters_appendices_and_equations() -> None:
    chapters = parse_document(
        r"""
\chapter{Main}
\label{chap:main}
\begin{equation}\label{eq:main}x=y\end{equation}
\chapter*{Preface}
\begin{remark}\label{rem:preface}A note.\end{remark}
\appendix
\chapter{Details}
\begin{equation}\label{eq:appendix}a=b\end{equation}
"""
    )
    references = number_document(chapters)

    assert [chapter["num"] for chapter in chapters] == ["1", None, "A"]
    assert references["chap:main"]["num"] == "1"
    assert references["eq:main"]["num"] == "1.1"
    assert references["eq:appendix"]["num"] == "A.1"
    preface_statement = next(
        block for block in chapters[1]["blocks"] if block["t"] == "stmt"
    )
    assert preface_statement["num"] == "1"
