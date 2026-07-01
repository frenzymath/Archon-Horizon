from __future__ import annotations

import os
import re
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from .models import LeanDecl


# Scanning fewer files than this runs serially — a process pool would cost more
# in start-up and IPC than it saves.
_PARALLEL_MIN_FILES = 8


# Lines at column 0 that indicate we've left the declaration body.
_OUTSIDE_DECL_RE = re.compile(
    r'^(?:end\b|section\b|namespace\b|variable\b|open\b'
    r'|noncomputable\s+section|#check\b|#eval\b|#print\b)'
)

# Opening line of a Lean declaration.
# `example` and anonymous `instance :` are split points even with no name —
# `*` (not `+`) allows an empty capture group. A same-line attribute prefix
# (`@[simp] lemma …`) is consumed so attributed declarations are still found.
# Leading horizontal whitespace is permitted so that indented declarations —
# e.g. the inner `def`s of a `mutual` block — are picked up too.
_DECL_RE = re.compile(
    r'^[^\S\n]*(?:@\[[^\]]*\]\s*)*'
    r'(?:(?:private|protected|noncomputable|irreducible|unsafe|scoped)\s+)*'
    r'(?:theorem|def|lemma|instance|abbrev|structure|class|inductive|example)\s+'
    r'([^\s{(\[:]*)' ,
    re.MULTILINE,
)

# Placeholders that mark a proof as incomplete.
_SORRY_RE = re.compile(r'\b(?:sorry|admit)\b')

# A newline followed by one or more blank (whitespace-only) lines.
_BLANK_LINES_RE = re.compile(r'\n(?:[ \t]*\n)+')

# `namespace` / `section` / `end` scope directives at column 0. A leading
# `noncomputable ` is allowed (e.g. `noncomputable section`). Group 2 is the
# (possibly dotted, possibly empty) scope name.
_SCOPE_RE = re.compile(
    r'^(?:noncomputable\s+)?(namespace|section|end)\b[^\S\n]*(\S*)',
    re.MULTILINE,
)


class LeanScanner:
    """
    Scan a Lean project tree and extract all named declarations.

    Usage::

        scanner = LeanScanner()
        decls = scanner.scan(Path("."))   # dict[name, LeanDecl]

    After :meth:`scan`, ``scanner.collisions`` lists genuine name conflicts as
    ``(fqn, path_a, path_b)`` tuples — the *same* fully-qualified name produced
    by two files with *different* source. Identical re-definitions (e.g. the
    same file reachable twice) are not reported.
    """

    def __init__(self) -> None:
        self.collisions: list[tuple[str, str, str]] = []

    def scan(self, root: Path) -> dict[str, LeanDecl]:
        """Return ``{name: LeanDecl}`` for every named declaration under *root*.

        Files are parsed in parallel across processes when there are enough of
        them; results are merged in sorted-path order so the mapping (and the
        recorded :attr:`collisions`) is deterministic regardless of scheduling.
        Each physical file is scanned once even if symlinks expose it under
        several paths.
        """
        self.collisions = []

        files: list[Path] = []
        seen_real: set[Path] = set()
        for f in sorted(root.glob('**/*.lean')):
            try:
                rel_parts = f.relative_to(root).parts
            except ValueError:
                rel_parts = f.parts
            # Skip anything under a hidden directory (.lake build cache, .git,
            # tooling dirs like .archon snapshot copies, …) — these hold stale
            # duplicates that would otherwise masquerade as conflicts.
            if any(part.startswith('.') for part in rel_parts):
                continue
            try:
                real = f.resolve()
            except OSError:
                real = f
            if real in seen_real:
                continue          # same physical file via another path
            seen_real.add(real)
            files.append(f)

        if len(files) >= _PARALLEL_MIN_FILES and (os.cpu_count() or 1) > 1:
            workers = min(len(files), os.cpu_count() or 1)
            chunk   = max(1, len(files) // (workers * 4))
            try:
                with ProcessPoolExecutor(max_workers=workers) as pool:
                    per_file = list(pool.map(_scan_file, map(str, files), chunksize=chunk))
            except OSError:
                # Process pool unavailable (sandboxed env etc.) — fall back.
                per_file = [_scan_file(str(f)) for f in files]
        else:
            per_file = [_scan_file(str(f)) for f in files]

        result: dict[str, LeanDecl] = {}
        winner: dict[str, Path] = {}
        for path, decls in zip(files, per_file):
            try:
                rel = path.relative_to(root).as_posix()
            except ValueError:
                rel = path.name
            for name, decl in decls.items():
                decl.file = rel
                if name in result and result[name].source != decl.source:
                    # Same name, different body — a real conflict worth flagging.
                    self.collisions.append((name, str(winner[name]), str(path)))
                result[name] = decl
                winner[name]  = path
        return result

    def _extract_from_source(self, raw: str) -> dict[str, LeanDecl]:
        """Extract declarations from one source string (kept for direct use)."""
        return self._extract_decls(raw)

    @staticmethod
    def _extract_decls(raw: str) -> dict[str, LeanDecl]:
        # Strip comments up front so that no later regex can mistake a keyword
        # inside a `--` line comment or a `/- … -/` block comment (including
        # `/-- … -/` docstrings) for a real declaration or scope directive.
        raw = LeanScanner._strip_comments(raw)

        result: dict[str, LeanDecl] = {}
        matches = list(_DECL_RE.finditer(raw))

        prefixes = LeanScanner._namespace_prefixes(raw, matches)

        for i, m in enumerate(matches):
            name = m.group(1).rstrip('.,;')
            if not name:
                # Anonymous example / unnamed instance — acts as a range
                # boundary but produces no LeanDecl entry.
                continue

            prefix = prefixes[i]
            if name.startswith('_root_.'):
                # `_root_.Foo` escapes the enclosing namespace — the name is
                # absolute, so the active prefix must NOT be prepended.
                fqn = name[len('_root_.'):]
            else:
                fqn = f'{prefix}.{name}' if prefix else name

            start  = m.start()
            end    = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
            source = LeanScanner._trim_to_decl(raw[start:end].strip())
            # Comment stripping leaves blank lines behind; collapse any run of
            # them to a single blank line so the displayed source stays compact.
            source = _BLANK_LINES_RE.sub('\n\n', source)

            has_sorry = bool(_SORRY_RE.search(source))

            result[fqn] = LeanDecl(
                name       = fqn,
                source     = source,
                proof_size = None if has_sorry else len(source),
                has_sorry  = has_sorry,
            )

        return result

    @staticmethod
    def _namespace_prefixes(raw: str, decls: list[re.Match]) -> list[str]:
        """
        Compute the fully-qualified namespace prefix active at each declaration.

        Sweeps ``namespace``/``section``/``end`` directives and declarations in
        source order, maintaining a scope stack.  Only ``namespace`` frames
        contribute to a declaration's dotted prefix, so a ``def toAbSheaf``
        inside ``namespace A.B`` resolves to ``A.B.toAbSheaf`` — matching the
        fully-qualified name a blueprint ``\\lean{}`` reference uses.
        """
        # (position, order, kind, payload) — order breaks ties so a scope
        # directive is applied before a declaration at the same offset.
        events: list[tuple[int, int, str, object]] = []
        for sm in _SCOPE_RE.finditer(raw):
            events.append((sm.start(), 0, 'scope', (sm.group(1), sm.group(2))))
        for idx, dm in enumerate(decls):
            events.append((dm.start(), 1, 'decl', idx))
        events.sort(key=lambda e: (e[0], e[1]))

        stack: list[tuple[str, str]] = []   # (kind, name); kind ∈ {namespace, section}
        prefixes: list[str] = [''] * len(decls)

        for _pos, _order, kind, payload in events:
            if kind == 'scope':
                keyword, sname = payload  # type: ignore[misc]
                if keyword in ('namespace', 'section'):
                    stack.append((keyword, sname))
                else:  # end — close the matching (or innermost) scope
                    if sname:
                        while stack:
                            if stack.pop()[1] == sname:
                                break
                    elif stack:
                        stack.pop()
            else:
                prefixes[payload] = '.'.join(  # type: ignore[index]
                    n for k, n in stack if k == 'namespace' and n
                )

        return prefixes

    @staticmethod
    def _trim_to_decl(source: str) -> str:
        """
        Strip trailing non-declaration content from a raw source slice.

        After splitting at declaration keywords the slice may include trailing
        infrastructure: ``section``/``end``/``variable``/``open``/``#check``
        lines and column-0 comment blocks.
        """
        lines  = source.splitlines()
        cutoff = len(lines)

        for i in range(1, len(lines)):
            line     = lines[i]
            stripped = line.strip()
            if not stripped:
                continue

            at_col0 = bool(line) and not line[0].isspace()
            if at_col0 and _OUTSIDE_DECL_RE.match(stripped):
                j = i
                while j > 0 and not lines[j - 1].strip():
                    j -= 1
                cutoff = j
                break

        return '\n'.join(lines[:cutoff]).rstrip()

    @staticmethod
    def _strip_comments(text: str) -> str:
        """
        Remove Lean ``--`` line comments and nested ``/- -/`` block comments
        (``/-- -/`` docstrings included).

        Newlines are preserved so that line-anchored matches stay aligned, and
        each block comment leaves a single space behind so it cannot fuse the
        tokens on either side of it.
        """
        out, i, n = [], 0, len(text)
        while i < n:
            if text[i] == '-' and i + 1 < n and text[i + 1] == '-':
                while i < n and text[i] != '\n':
                    i += 1
            elif text[i] == '/' and i + 1 < n and text[i + 1] == '-':
                i += 2
                depth = 1
                while i < n and depth > 0:
                    if text[i] == '/' and i + 1 < n and text[i + 1] == '-':
                        depth += 1
                        i += 2
                    elif text[i] == '-' and i + 1 < n and text[i + 1] == '/':
                        depth -= 1
                        i += 2
                    else:
                        if text[i] == '\n':
                            out.append('\n')
                        i += 1
                out.append(' ')
            else:
                out.append(text[i])
                i += 1
        return ''.join(out)


def _scan_file(path_str: str) -> dict[str, LeanDecl]:
    """Read and extract one Lean file. Top-level so it is picklable for the pool."""
    try:
        raw = Path(path_str).read_text(encoding='utf-8', errors='replace')
    except OSError:
        return {}
    return LeanScanner._extract_decls(raw)
