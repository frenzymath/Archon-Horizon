from __future__ import annotations

import bisect
import re
from pathlib import Path
from typing import Callable

from .models import BlueprintDecl


NODE_ENVS: frozenset[str] = frozenset({
    "definition", "lemma", "proposition", "theorem",
    "corollary", "exercise", "remark", "conjecture", "notation",
})

_STRIP_CMDS = re.compile(
    r'\\(?:label|lean|uses|source|leanok|notready|mathlibok|discussion)'
    r'(?:\*)?'
    r'(?:\s*\{[^}]*\})?'
)

_LABEL_RE   = re.compile(r'\\label\{([^}]+)\}')
_LEAN_RE    = re.compile(r'\\lean\{([^}]+)\}')
_USES_RE    = re.compile(r'\\uses\{([^}]+)\}')
_SOURCE_RE  = re.compile(r'\\source\{([^}]+)\}')
_LEANOK_RE  = re.compile(r'\\leanok\b')
_MATHLIBOK_RE = re.compile(r'\\mathlibok\b')
_CHAPTER_RE = re.compile(r'\\chapter\*?\{([^}]+)\}')
_INPUT_RE   = re.compile(r'\\input\{([^}]+)\}')
_PROOF_RE   = re.compile(r'\\begin\{proof\}(.*?)\\end\{proof\}', re.DOTALL)


class BlueprintParser:
    """
    Parse a leanblueprint LaTeX project rooted at an entry ``.tex`` file.

    Usage::

        parser = BlueprintParser(Path("blueprint/src/web.tex"))
        decls, proofs = parser.parse()
        parser.macros   # {"\\macro": "expansion"} collected from the preamble

    After :meth:`parse`, ``parser.macros`` holds the LaTeX macro definitions
    found in the project (``\\newcommand``/``\\def``/``\\DeclareMathOperator``),
    suitable for handing to KaTeX so blueprint-defined notation renders.
    """

    def __init__(self, entry: Path) -> None:
        self._entry = entry
        self.macros: dict[str, str] = {}

    def parse(self) -> tuple[list[BlueprintDecl], dict[str, str]]:
        """
        Return ``(declarations, proof_bodies)``.

        ``proof_bodies`` maps a declaration id to the raw LaTeX text
        inside its nearest ``proof`` environment.
        """
        pieces      = self._load_pieces(self._entry, visited=set())
        text        = ''.join(chunk for _, chunk in pieces)
        self.macros = self._extract_macros(text)
        file_at     = self._build_file_index(pieces)
        decls  = self._extract_declarations(text, file_at)
        proofs = self._extract_proofs(text, decls)
        for decl in decls:
            decl.proof_tex = proofs.get(decl.id, '')
        return decls, proofs

    def _load_pieces(self, path: Path, visited: set[Path]) -> list[tuple[Path, str]]:
        """Recursively load a .tex file, expanding ``\\input{}`` directives.

        Returns the source as a list of ``(file, chunk)`` pieces in reading
        order. Concatenating the chunks yields exactly the flattened text (each
        ``\\input{}`` token replaced by its target's content, or by nothing when
        the target is missing); keeping the pieces lets us map any position in
        that text back to the file it came from.
        """
        path = path.resolve()
        if path in visited:
            return []
        visited.add(path)
        text = self._strip_comments(
            path.read_text(encoding='utf-8', errors='replace')
        )

        pieces: list[tuple[Path, str]] = []
        last = 0
        for m in _INPUT_RE.finditer(text):
            pieces.append((path, text[last:m.start()]))
            fname = m.group(1).strip()
            if not fname.endswith('.tex'):
                fname += '.tex'
            child = (path.parent / fname).resolve()
            if child.exists():
                pieces.extend(self._load_pieces(child, visited))
            last = m.end()
        pieces.append((path, text[last:]))
        return pieces

    def _rel_file(self, path: Path) -> str:
        """A short, stable label for a source file (relative to the entry dir)."""
        base = self._entry.resolve().parent
        try:
            return path.resolve().relative_to(base).as_posix()
        except ValueError:
            return path.name

    def _build_file_index(self, pieces: list[tuple[Path, str]]) -> Callable[[int], str]:
        """Return ``file_at(pos)`` mapping a flattened-text offset to its file."""
        starts: list[int] = []
        files:  list[str] = []
        off = 0
        for path, chunk in pieces:
            starts.append(off)
            files.append(self._rel_file(path))
            off += len(chunk)

        def file_at(pos: int) -> str:
            i = bisect.bisect_right(starts, pos) - 1
            return files[i] if i >= 0 else ""

        return file_at

    def _extract_declarations(
        self, text: str, file_at: Callable[[int], str]
    ) -> list[BlueprintDecl]:
        chapter_positions = [
            (m.start(), m.group(1).strip()) for m in _CHAPTER_RE.finditer(text)
        ]

        def chapter_at(pos: int) -> str:
            name = ""
            for cpos, cname in chapter_positions:
                if cpos <= pos:
                    name = cname
            return name

        decls: list[BlueprintDecl] = []
        for env_type in NODE_ENVS:
            begin_pat = re.compile(
                rf'\\begin\{{{re.escape(env_type)}\}}'
                rf'(?:\s*\[([^\]]*)\])?'
            )
            end_pat = re.compile(rf'\\end\{{{re.escape(env_type)}\}}')

            for bm in begin_pat.finditer(text):
                em = end_pat.search(text, bm.end())
                if em is None:
                    continue

                body  = text[bm.end():em.start()]
                title = (bm.group(1) or '').strip()

                label_m = _LABEL_RE.search(body)
                if label_m is None:
                    continue

                uses_m = _USES_RE.search(body)

                label     = label_m.group(1).strip()
                # \lean{} may list several comma-separated names, and may even
                # appear more than once in a single environment — collect them all.
                lean_names = [
                    name.strip()
                    for lm in _LEAN_RE.finditer(body)
                    for name in lm.group(1).split(',')
                    if name.strip()
                ]
                is_proved  = bool(_LEANOK_RE.search(body))
                is_mathlib = bool(_MATHLIBOK_RE.search(body))
                uses_raw  = uses_m.group(1) if uses_m else ''
                uses      = [u.strip() for u in uses_raw.split(',') if u.strip()]
                sources   = [
                    src.strip()
                    for sm in _SOURCE_RE.finditer(body)
                    for src in sm.group(1).split(',')
                    if src.strip()
                ]

                stmt = _STRIP_CMDS.sub('', body)
                stmt = re.sub(r'\n{3,}', '\n\n', stmt).strip()

                decls.append(BlueprintDecl(
                    id        = label,
                    type      = env_type,
                    title     = title,
                    chapter   = chapter_at(bm.start()),
                    statement = stmt,
                    uses       = uses,
                    sources    = sources,
                    proof_tex  = '',
                    lean_names = lean_names,
                    is_proved  = is_proved,
                    mathlib_ok = is_mathlib,
                    tex_file   = file_at(bm.start()),
                ))

        label_pos = {m.group(1).strip(): m.start() for m in _LABEL_RE.finditer(text)}
        decls.sort(key=lambda d: label_pos.get(d.id, 0))
        return decls

    def _extract_proofs(
        self,
        text: str,
        decls: list[BlueprintDecl],
    ) -> dict[str, str]:
        label_pos     = {m.group(1).strip(): m.start() for m in _LABEL_RE.finditer(text)}
        sorted_labels = sorted(label_pos.items(), key=lambda x: x[1])

        proofs: dict[str, str] = {}
        for pm in _PROOF_RE.finditer(text):
            preceding = [(lid, pos) for lid, pos in sorted_labels if pos < pm.start()]
            if preceding:
                node_id        = max(preceding, key=lambda x: x[1])[0]
                proofs[node_id] = pm.group(1)
        return proofs

    @staticmethod
    def _strip_comments(text: str) -> str:
        """Remove LaTeX % comments, respecting \\%."""
        out, i, n = [], 0, len(text)
        while i < n:
            if text[i] == '\\' and i + 1 < n:
                out.append(text[i:i+2])
                i += 2
            elif text[i] == '%':
                while i < n and text[i] != '\n':
                    i += 1
            else:
                out.append(text[i])
                i += 1
        return ''.join(out)

    # ── Macro extraction ────────────────────────────────────────────────────

    @staticmethod
    def _read_braced(text: str, i: int) -> tuple[str, int]:
        """If ``text[i]`` is ``{``, return ``(body, index_after_close)`` with
        nested braces balanced; otherwise ``('', i)``."""
        if i >= len(text) or text[i] != '{':
            return '', i
        depth, j = 0, i
        n = len(text)
        while j < n:
            c = text[j]
            if c == '\\':            # skip escaped char (e.g. \{ \})
                j += 2
                continue
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    return text[i + 1:j], j + 1
            j += 1
        return text[i + 1:], n       # unbalanced — take the rest

    def _extract_macros(self, text: str) -> dict[str, str]:
        r"""
        Collect ``\newcommand``/``\renewcommand``/``\providecommand``,
        ``\def`` and ``\DeclareMathOperator`` definitions into a KaTeX macro
        map ``{"\\name": "expansion"}``. Argument placeholders (``#1`` …) are
        kept verbatim, which is what KaTeX expects.
        """
        macros: dict[str, str] = {}

        # \newcommand{\name}[args][default]{body}  /  \newcommand\name{body}
        cmd_re = re.compile(r'\\(?:newcommand|renewcommand|providecommand)\s*\*?\s*')
        for m in cmd_re.finditer(text):
            i = m.end()
            # name: either {\foo} or \foo
            if i < len(text) and text[i] == '{':
                name_block, i = self._read_braced(text, i)
                name = name_block.strip()
            else:
                nm = re.match(r'\\([A-Za-z@]+)', text[i:])
                if not nm:
                    continue
                name = '\\' + nm.group(1)
                i += nm.end()
            if not name.startswith('\\'):
                continue
            # optional [n] and [default] specifiers
            while i < len(text) and text[i:].lstrip(' \t').startswith('['):
                i = text.index('[', i)
                close = text.find(']', i)
                if close == -1:
                    break
                i = close + 1
            i = self._skip_ws(text, i)
            body, _ = self._read_braced(text, i)
            if name:
                macros[name] = body.strip()

        # \DeclareMathOperator{\name}{op}  (star → \operatorname*)
        op_re = re.compile(r'\\DeclareMathOperator\s*(\*?)\s*')
        for m in op_re.finditer(text):
            star = m.group(1)
            i = self._skip_ws(text, m.end())
            name_block, i = self._read_braced(text, i)
            i = self._skip_ws(text, i)
            op_block, _ = self._read_braced(text, i)
            name = name_block.strip()
            if name.startswith('\\'):
                opname = '\\operatorname' + ('*' if star else '')
                macros[name] = f'{opname}{{{op_block.strip()}}}'

        # \def\name{body}
        for m in re.finditer(r'\\def\s*\\([A-Za-z@]+)\s*', text):
            name = '\\' + m.group(1)
            body, _ = self._read_braced(text, self._skip_ws(text, m.end()))
            macros.setdefault(name, body.strip())

        return macros

    @staticmethod
    def _skip_ws(text: str, i: int) -> int:
        while i < len(text) and text[i] in ' \t\r\n':
            i += 1
        return i
