"""Dashboard textbook chapters follow the same entry as hgraph.

``project_chapters`` must expand the configured blueprint entry (content.tex),
include only chapters reached from that entry, and strip ``\\newcommand`` /
preamble definitions so they never render as prose.
"""

from __future__ import annotations

from pathlib import Path

from archon_horizon.blueprint.chapters import project_chapters
from archon_horizon.core.workspace import Project, Workspace


def _ws(tmp_path: Path, *, layout: str = "src") -> Workspace:
    """Minimal workspace with one project and a blueprint entry.

    ``layout="src"`` → ``blueprint/src/content.tex`` (leanblueprint convention,
    same as config ``blueprint.path: blueprint/src``).
    ``layout="flat"`` → ``blueprint/content.tex``.
    """
    proj = tmp_path / "P"
    if layout == "src":
        bp = proj / "blueprint" / "src"
        bp_cfg = Path("P/blueprint/src")
    else:
        bp = proj / "blueprint"
        bp_cfg = Path("P/blueprint")
    bp.mkdir(parents=True)
    chapters = bp / "chapters"
    chapters.mkdir()
    (bp / "macros.tex").write_text(
        r"\newcommand{\R}{\mathbb{R}}" "\n"
        r"\newtheorem{theorem}{Theorem}" "\n",
        "utf-8",
    )
    (chapters / "01_main.tex").write_text(
        r"\section{Main}" "\n"
        r"\newcommand{\mufast}{\mu_{\mathrm{fast}}}" "\n"
        r"Fix $p>0$ on $\R$. Use $\mufast$." "\n"
        r"\begin{theorem}\label{thm:t}T.\end{theorem}" "\n",
        "utf-8",
    )
    (chapters / "02_more.tex").write_text(
        r"\section{More}" "\n"
        r"More text." "\n",
        "utf-8",
    )
    # Sidecar macro file — NOT input from content.tex; must not become a chapter.
    (chapters / "00_katex_macros.tex").write_text(
        r"\newcommand{\GS}{\mathsf{GS}}" "\n"
        r"\newcommand{\C}{\mathbb{C}}" "\n",
        "utf-8",
    )
    (bp / "content.tex").write_text(
        r"\input{macros}" "\n"
        r"\chapter*{About}" "\n"
        r"About this blueprint." "\n"
        r"\chapter{Problem setting}" "\n"
        r"\input{chapters/01_main}" "\n"
        r"\chapter{Complete proof}" "\n"
        r"\input{chapters/02_more}" "\n",
        "utf-8",
    )
    return Workspace(
        name="ws",
        root=tmp_path,
        projects={
            "P": Project(name="P", path=Path("P"), blueprint_path=bp_cfg),
        },
    )


def test_chapters_follow_content_entry_not_loose_files(tmp_path: Path) -> None:
    data = project_chapters(_ws(tmp_path), "P")
    assert data["hasBlueprint"] is True
    assert data["error"] is None
    titles = [c["title"] for c in data["chapters"]]
    # content.tex order; 00_katex_macros is never a chapter.
    assert titles == ["About", "Problem setting", "Complete proof"]
    slugs = [c["slug"] for c in data["chapters"]]
    assert "00-katex-macros" not in slugs
    assert "00_katex_macros" not in slugs


def test_newcommand_stripped_from_chapter_body(tmp_path: Path) -> None:
    data = project_chapters(_ws(tmp_path), "P")
    main = next(c for c in data["chapters"] if c["title"] == "Problem setting")
    # Definitions must not leak as prose ({GS}, [1]{…}, etc.).
    assert r"\newcommand" not in main["tex"]
    assert r"\mufast" not in main["tex"] or "mu" in data["macros"].get(r"\mufast", "")
    assert "{GS}" not in main["tex"]
    assert "Fix $p>0$" in main["tex"]
    # KaTeX still receives the macros harvested from the tree / entry.
    assert data["macros"].get(r"\R") == r"\mathbb{R}"
    assert data["macros"].get(r"\mufast") == r"\mu_{\mathrm{fast}}"
    assert data["macros"].get(r"\GS") == r"\mathsf{GS}"


def test_macros_input_not_a_phantom_introduction(tmp_path: Path) -> None:
    data = project_chapters(_ws(tmp_path), "P")
    titles = [c["title"] for c in data["chapters"]]
    # Preamble-only material before the first \chapter must not invent a chapter.
    assert "Introduction" not in titles


def test_missing_entry_reports_error(tmp_path: Path) -> None:
    proj = tmp_path / "Q"
    bp = proj / "blueprint" / "src"
    bp.mkdir(parents=True)
    (bp / "chapters").mkdir()
    (bp / "chapters" / "only.tex").write_text(r"\section{Orphan}X.", "utf-8")
    # No content.tex / web.tex / print.tex
    ws = Workspace(
        name="ws",
        root=tmp_path,
        projects={
            "Q": Project(
                name="Q",
                path=Path("Q"),
                blueprint_path=Path("Q/blueprint/src"),
            ),
        },
    )
    data = project_chapters(ws, "Q")
    assert data["hasBlueprint"] is False
    assert data["chapters"] == []
    assert data["error"] and "entry" in data["error"].lower()
