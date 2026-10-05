"""Extract Lean declarations and search them with BM25 / name / type matching.

Declaration extraction is cached as JSONL. Text and header ranking use bm25s
(NumPy/SciPy sparse BM25); the inverted index is persisted next to that cache
so the first query does not rebuild it in Python.
"""

from __future__ import annotations

import json
import logging
import os
import re
from bisect import bisect_left
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

logging.getLogger("bm25s").setLevel(logging.WARNING)

CACHE_VERSION = 6

# ── Declaration extraction ────────────────────────────────────────────────────

_DECL_KINDS = (
    "theorem", "lemma", "def", "abbrev", "instance",
    "structure", "class", "inductive", "axiom", "opaque",
)
_MODIFIERS = ("private", "protected", "noncomputable", "partial", "unsafe", "nonrec", "scoped", "local")
_DECL_RE = re.compile(
    r"^(?:@\[[^\]]*\]\s*)*"
    r"(?:(?:" + "|".join(_MODIFIERS) + r")\s+)*"
    r"(" + "|".join(_DECL_KINDS) + r")\b"
    r"\s*(@?[^\s:(){\[\]]*)"
)
_NAMESPACE_RE = re.compile(r"^namespace\s+(\S+)")
_END_RE = re.compile(r"^end(?:\s+(\S+))?\s*$")
# camelCase / snake_case / dotted splitter for tokenisation.
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_']*")
_CAMEL_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z0-9]+|[A-Z]+|[0-9]+")


@dataclass(frozen=True, slots=True)
class Declaration:
    name: str          # fully-qualified, e.g. "Continuous.comp"
    kind: str          # theorem | def | …
    signature: str     # head of the declaration, up to ":=" / "where"
    doc: str           # docstring text, if any
    library: str       # which configured library / project it came from
    file: str          # path, workspace-relative when possible
    line: int          # 1-based line of the declaration keyword
    header: str = ""   # accumulated `/-!` module / section header

    def as_document(self) -> str:
        """The text BM25 indexes: name + signature + docstring + module header."""
        spaced_name = self.name.replace(".", " ")
        return f"{spaced_name}\n{self.signature}\n{self.doc}\n{self.header}"


def _read_comment_block(lines: list[str], i: int, opener: str) -> tuple[str | None, int]:
    """If line ``i`` opens ``opener … -/``, return (text, end_index)."""
    line = lines[i].lstrip()
    if not line.startswith(opener):
        return None, i
    rest = line[len(opener):]
    if "-/" in rest:
        return rest.split("-/", 1)[0].strip(), i
    buf = [rest]
    j = i + 1
    while j < len(lines):
        if "-/" in lines[j]:
            buf.append(lines[j].split("-/", 1)[0])
            return " ".join(s.strip() for s in buf).strip(), j
        buf.append(lines[j])
        j += 1
    return " ".join(s.strip() for s in buf).strip(), len(lines) - 1


def _read_docstring(lines: list[str], i: int) -> tuple[str | None, int]:
    """If line ``i`` opens a ``/-- … -/`` docstring, return (text, end_index)."""
    return _read_comment_block(lines, i, "/--")


def _collect_signature(lines: list[str], i: int) -> str:
    """Join the declaration head from line ``i`` until the TOP-LEVEL ``:=`` /
    ``where`` / blank line.

    A ``:=`` (or ``where``) only terminates the signature at bracket depth 0 — one
    inside ``(…)``/``[…]``/``{…}`` is a named argument like ``(I := …)`` or a
    default value, NOT the definition body, so it must not truncate the signature.
    """
    parts: list[str] = []
    depth = 0
    for k in range(i, min(i + 32, len(lines))):
        raw = lines[k]
        stripped = raw.strip()
        if k > i and (not stripped or _DECL_RE.match(stripped) or _NAMESPACE_RE.match(stripped)):
            break
        cut_at: int | None = None
        j = 0
        while j < len(raw):
            ch = raw[j]
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth = max(0, depth - 1)
            elif depth == 0:
                if raw.startswith(":=", j):
                    cut_at = j
                    break
                if raw.startswith(" where", j) or raw.startswith("\twhere", j):
                    cut_at = j
                    break
            j += 1
        if cut_at is not None:
            parts.append(raw[:cut_at])
            return re.sub(r"\s+", " ", " ".join(parts)).strip()
        parts.append(raw)
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def extract_declarations(text: str, *, library: str, file: str) -> list[Declaration]:
    """Parse one Lean source into declarations. Heuristic, not a real parser."""
    lines = text.splitlines()
    ns: list[str] = []
    pending_doc: str | None = None
    module_header = ""
    out: list[Declaration] = []
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if not stripped:
            i += 1
            continue

        if stripped.startswith("/-!"):
            header, end = _read_comment_block(lines, i, "/-!")
            if header:
                module_header = f"{module_header} {header}".strip() if module_header else header
            pending_doc = None
            i = end + 1
            continue
        if stripped.startswith("/--"):
            doc, end = _read_docstring(lines, i)
            pending_doc = doc
            i = end + 1
            continue
        if stripped.startswith("/-"):  # module/section comment — skip the block
            j = i
            while j < len(lines) and "-/" not in lines[j]:
                j += 1
            i = j + 1
            continue

        if stripped.startswith("@["):  # attribute line — keep any pending docstring
            i += 1
            continue

        m_ns = _NAMESPACE_RE.match(stripped)
        if m_ns:
            ns.append(m_ns.group(1))
            pending_doc = None
            i += 1
            continue
        m_end = _END_RE.match(stripped)
        if m_end:
            if ns and (m_end.group(1) is None or ns[-1].endswith(m_end.group(1))):
                ns.pop()
            i += 1
            continue

        m = _DECL_RE.match(stripped)
        if m:
            kind, raw_name = m.group(1), m.group(2).lstrip("@")
            qualified = ".".join([*ns, raw_name]) if raw_name else ".".join(ns)
            out.append(Declaration(
                name=qualified,
                kind=kind,
                signature=_collect_signature(lines, i),
                doc=pending_doc or "",
                library=library,
                file=file,
                line=i + 1,
                header=module_header,
            ))
            pending_doc = None
            i += 1
            continue

        pending_doc = None
        i += 1
    return out


# ── Tokenisation ──────────────────────────────────────────────────────────────

def _tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for word in _WORD_RE.findall(text):
        lower = word.lower()
        if len(lower) >= 2:
            tokens.append(lower)
        for piece in _CAMEL_RE.findall(word):
            p = piece.lower()
            if len(p) >= 2 and p != lower:
                tokens.append(p)
    return tokens


# ── BM25 (bm25s) ──────────────────────────────────────────────────────────────

def _bm25s_module():
    try:
        import bm25s
    except ImportError as exc:
        raise RuntimeError(
            "Lean text search requires bm25s, numpy, and scipy "
            "(install archon-horizon[search])"
        ) from exc
    return bm25s


def _numpy():
    try:
        import numpy as np
    except ImportError as exc:
        raise RuntimeError(
            "Lean text search requires numpy (install archon-horizon[search])"
        ) from exc
    return np


def _build_retriever(documents: list[str]):
    if not documents:
        return None
    bm25s = _bm25s_module()
    retriever = bm25s.BM25(k1=1.5, b=0.75, method="lucene")
    np = _numpy()
    try:
        with np.errstate(invalid="ignore", divide="ignore"):
            retriever.index([_tokenize(text) for text in documents], show_progress=False, create_empty_token=True)
    except ValueError:
        return None
    return retriever


def _load_retriever(path: Path):
    if not (path / "params.index.json").is_file():
        return None
    return _bm25s_module().BM25.load(path, mmap=True, load_corpus=False, show_progress=False)


def _save_retriever(retriever: Any, path: Path) -> None:
    if retriever is None:
        return
    path.mkdir(parents=True, exist_ok=True)
    retriever.save(path, show_progress=False)


# ── Name / type matching (Loogle-style) ──────────────────────────────────────

# One type hole: an identifier, or a single paren/bracket/brace group. No nested
# unbounded `.*?`, so patterns like `(?a -> ?b) -> List ?a -> List ?b` cannot
# explode on a long signature.
_TYPE_HOLE = (
    r"(?:[^\s()\[\]{}]{1,80}"
    r"|\([^()]{0,120}\)"
    r"|\[[^\[\]]{0,120}\]"
    r"|\{[^{}]{0,80}\})"
)
_TYPE_WILDCARD_RE = re.compile(
    r"(\?[A-Za-z0-9_']+|(?<![A-Za-z0-9_'])_(?![A-Za-z0-9_']))"
)


def _name_score(query: str, name: str) -> float:
    """Exact / prefix / substring. No subsequence — that ranked unrelated names."""
    q, n = query.lower(), name.lower()
    if not q:
        return 0.0
    segment = n.rsplit(".", 1)[-1]
    if q == n or q == segment:
        return 100.0
    if segment.startswith(q):
        return 80.0 - (len(segment) - len(q)) * 0.1
    if n.startswith(q):
        return 70.0 - (len(n) - len(q)) * 0.1
    start = 0
    while True:
        dot = n.find(".", start)
        if dot < 0:
            break
        suffix = n[dot + 1:]
        if suffix == q:
            return 100.0
        if suffix.startswith(q):
            return 80.0 - (len(suffix.rsplit(".", 1)[0]) - len(q)) * 0.1
        start = dot + 1
    if q in n:
        return 60.0 - (len(n) - len(q)) * 0.05
    return 0.0


def _type_pattern_to_regex(pattern: str) -> re.Pattern[str] | None:
    """Compile a signature pattern. Wildcards are one bounded type atom.

    Returns ``None`` when the pattern is empty or only wildcards (those would
    match every signature).
    """
    norm = pattern.replace("→", "->").replace("⟶", "->").strip()
    if not norm:
        return None
    parts: list[str] = []
    has_literal = False
    for tok in _TYPE_WILDCARD_RE.split(norm):
        if not tok:
            continue
        if tok.startswith("?") or tok == "_":
            parts.append(_TYPE_HOLE)
            continue
        literal = re.escape(tok.strip()).replace(r"\ ", r"\s*")
        if not literal:
            continue
        has_literal = True
        parts.append(literal)
    if not has_literal or not parts:
        return None
    body = r"\s*".join(parts)
    try:
        return re.compile(body)
    except re.error:
        return None


def _type_literal_needles(pattern: str) -> list[str]:
    """Compact literal chunks used to skip signatures before running the regex."""
    norm = pattern.replace("→", "->").replace("⟶", "->")
    needles: list[str] = []
    for tok in _TYPE_WILDCARD_RE.split(norm):
        if not tok or tok.startswith("?") or tok == "_":
            continue
        compact = re.sub(r"\s+", "", tok)
        if compact:
            needles.append(compact)
    return needles


def _prefix_indices(sorted_pairs: list[tuple[str, int]], prefix: str) -> list[int]:
    if not prefix or not sorted_pairs:
        return []
    i = bisect_left(sorted_pairs, (prefix, -1))
    out: list[int] = []
    while i < len(sorted_pairs):
        key, idx = sorted_pairs[i]
        if not key.startswith(prefix):
            break
        out.append(idx)
        i += 1
    return out


def _normalize_sig(signature: str) -> str:
    return re.sub(r"\s+", " ", signature.replace("→", "->").replace("⟶", "->"))


@dataclass(frozen=True, slots=True)
class SearchHit:
    declaration: Declaration
    score: float


# ── The index ─────────────────────────────────────────────────────────────────

class LeanSearchIndex:
    """Build (and cache) a declaration index, then query it three ways."""

    def __init__(self, declarations: list[Declaration], *, cache_dir: Path | None = None) -> None:
        self.declarations = declarations
        self._cache_dir = Path(cache_dir) if cache_dir else None
        self._text_retriever: Any | None = None
        self._header_retriever: Any | None = None
        self._library_counts: dict[str, int] | None = None
        self._libraries: Any | None = None
        self._has_header: Any | None = None
        self._name_by_full: dict[str, list[int]] | None = None
        self._name_by_segment: dict[str, list[int]] | None = None
        self._name_suffixes: list[tuple[str, int]] | None = None

    # -- construction --

    @classmethod
    def build(cls, source_roots: dict[str, Path], *, workspace_root: Path | None = None,
              check_cancelled: Callable[[], None] | None = None) -> "LeanSearchIndex":
        """Extract declarations from ``{library_name: root_dir}``."""
        jobs = []
        for library, path in _iter_source_files(source_roots):
            if check_cancelled:
                check_cancelled()
            jobs.append((library, path, _display_path(path, workspace_root)))
        if not jobs:
            return cls([])
        workers = min(8, os.cpu_count() or 4, len(jobs))
        def extract(job):
            if check_cancelled:
                check_cancelled()
            return _extract_source_file(job)
        if workers <= 1:
            parts = [extract(job) for job in jobs]
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                parts = list(pool.map(extract, jobs))
        if check_cancelled:
            check_cancelled()
        decls = [decl for part in parts for decl in part]
        return cls(decls)

    @property
    def library_counts(self) -> dict[str, int]:
        if self._library_counts is None:
            counts: dict[str, int] = {}
            for declaration in self.declarations:
                counts[declaration.library] = counts.get(declaration.library, 0) + 1
            self._library_counts = dict(sorted(counts.items()))
        return self._library_counts

    def validate_library(self, library: str | None) -> None:
        if library is not None and library not in self.library_counts:
            available = ", ".join(self.library_counts) or "(none)"
            raise ValueError(f"Unknown Lean library {library!r}. Indexed libraries: {available}.")

    def _text(self):
        if self._text_retriever is None:
            cached = _load_retriever(self._cache_dir / "text") if self._cache_dir else None
            self._text_retriever = cached if cached is not None else _build_retriever(
                [d.as_document() for d in self.declarations])
            if cached is None and self._cache_dir is not None:
                _save_retriever(self._text_retriever, self._cache_dir / "text")
        return self._text_retriever

    def _header(self):
        if self._header_retriever is None:
            cached = _load_retriever(self._cache_dir / "header") if self._cache_dir else None
            self._header_retriever = cached if cached is not None else _build_retriever(
                [d.header for d in self.declarations])
            if cached is None and self._cache_dir is not None:
                _save_retriever(self._header_retriever, self._cache_dir / "header")
        return self._header_retriever

    def query(self, text: str, *, mode: str = "text", limit: int = 10,
              library: str | None = None) -> list[SearchHit]:
        if mode == "name":
            return self.search_name(text, limit=limit, library=library)
        if mode == "type":
            return self.search_type(text, limit=limit, library=library)
        if mode == "header":
            return self.search_header(text, limit=limit, library=library)
        if mode not in {"text", "informal"}:
            raise ValueError(f"Unknown search mode {mode!r}. Use text, name, type, or header.")
        return self.search_text(text, limit=limit, library=library)

    # -- queries --

    def search_text(self, query: str, *, limit: int = 10, library: str | None = None) -> list[SearchHit]:
        self.validate_library(library)
        return self._search_bm25(self._text(), query, limit=limit, library=library)

    def search_name(self, query: str, *, limit: int = 10, library: str | None = None) -> list[SearchHit]:
        self.validate_library(library)
        q = query.strip().lower()
        if not q or not self.declarations:
            return []
        self._ensure_name_index()
        by_full = self._name_by_full or {}
        by_segment = self._name_by_segment or {}
        suffixes = self._name_suffixes or []
        best: dict[int, float] = {}

        def consider(idx: int, score: float) -> None:
            if score <= 0:
                return
            if library is not None and self.declarations[idx].library != library:
                return
            if score > best.get(idx, 0.0):
                best[idx] = score

        for idx in by_full.get(q, ()):
            consider(idx, 100.0)
        for idx in by_segment.get(q, ()):
            consider(idx, 100.0)
        for idx in _prefix_indices(suffixes, q):
            consider(idx, _name_score(q, self.declarations[idx].name))

        have_exact = any(score >= 100.0 for score in best.values())
        have_enough_prefix = sum(1 for score in best.values() if score >= 70.0) >= max(1, limit)
        if not have_exact and not have_enough_prefix:
            needle = q
            for idx, declaration in enumerate(self.declarations):
                if idx in best:
                    continue
                if library is not None and declaration.library != library:
                    continue
                if needle in declaration.name.lower():
                    consider(idx, _name_score(q, declaration.name))

        hits = [SearchHit(self.declarations[idx], score) for idx, score in best.items()]
        hits.sort(key=lambda h: (h.score, -len(h.declaration.name)), reverse=True)
        return hits[:limit]

    def search_type(self, pattern: str, *, limit: int = 10, library: str | None = None) -> list[SearchHit]:
        self.validate_library(library)
        regex = _type_pattern_to_regex(pattern)
        if regex is None:
            return []
        needles = _type_literal_needles(pattern)
        hits: list[SearchHit] = []
        for d in self.declarations:
            if library is not None and d.library != library:
                continue
            sig = _normalize_sig(d.signature)
            compact = sig.replace(" ", "")
            if any(needle not in compact for needle in needles):
                continue
            if regex.search(sig):
                # Prefer tighter matches: shorter signatures rank higher.
                hits.append(SearchHit(d, 1000.0 / (len(sig) + 1)))
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:limit]

    def _ensure_name_index(self) -> None:
        if self._name_suffixes is not None:
            return
        by_full: dict[str, list[int]] = {}
        by_segment: dict[str, list[int]] = {}
        suffixes: list[tuple[str, int]] = []
        for idx, declaration in enumerate(self.declarations):
            full = declaration.name.lower()
            segment = full.rsplit(".", 1)[-1]
            by_full.setdefault(full, []).append(idx)
            by_segment.setdefault(segment, []).append(idx)
            suffixes.append((full, idx))
            start = 0
            while True:
                dot = full.find(".", start)
                if dot < 0:
                    break
                suffixes.append((full[dot + 1:], idx))
                start = dot + 1
        suffixes.sort()
        self._name_by_full = by_full
        self._name_by_segment = by_segment
        self._name_suffixes = suffixes

    def search_header(self, query: str, *, limit: int = 10, library: str | None = None) -> list[SearchHit]:
        self.validate_library(library)
        return self._search_bm25(self._header(), query, limit=limit, library=library, require_header=True)

    def _library_array(self):
        np = _numpy()
        if self._libraries is None:
            self._libraries = np.array([d.library for d in self.declarations], dtype=object)
        return self._libraries

    def _header_mask(self):
        np = _numpy()
        if self._has_header is None:
            self._has_header = np.array([bool(d.header) for d in self.declarations], dtype=bool)
        return self._has_header

    def _search_bm25(self, retriever: Any, query: str, *, limit: int, library: str | None,
                     require_header: bool = False) -> list[SearchHit]:
        tokens = _tokenize(query)
        if retriever is None or not tokens or not self.declarations:
            return []
        np = _numpy()
        scores = np.asarray(retriever.get_scores(tokens), dtype=np.float32)
        if scores.shape[0] != len(self.declarations):
            return []
        if library is not None:
            scores = np.where(self._library_array() == library, scores, np.float32("-inf"))
        if require_header:
            scores = np.where(self._header_mask(), scores, np.float32("-inf"))
        positive = scores > 0
        count = int(np.count_nonzero(positive))
        if count == 0:
            return []
        k = min(max(1, limit), count)
        scores = np.where(positive, scores, np.float32("-inf"))
        top = np.argpartition(scores, -k)[-k:]
        top = top[np.lexsort((top, -scores[top]))]
        return [SearchHit(self.declarations[int(i)], float(scores[i])) for i in top]

    # -- caching --

    def to_jsonl(self) -> str:
        return "\n".join(json.dumps(asdict(d)) for d in self.declarations)

    @classmethod
    def from_jsonl(cls, text: str, *, cache_dir: Path | None = None) -> "LeanSearchIndex":
        decls = []
        for line in text.splitlines():
            if not line.strip():
                continue
            payload = json.loads(line)
            payload.setdefault("header", "")
            decls.append(Declaration(**payload))
        return cls(decls, cache_dir=cache_dir)

    def save_cache(self, dest: Path, fingerprint: str, *, check_cancelled: Callable[[], None] | None = None) -> None:
        if check_cancelled:
            check_cancelled()
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "declarations.jsonl").write_text(self.to_jsonl(), encoding="utf-8")
        (dest / "meta.json").write_text(json.dumps({
            "fingerprint": fingerprint, "version": CACHE_VERSION,
            "declaration_count": len(self.declarations),
        }), encoding="utf-8")
        if self._text_retriever is None:
            self._text_retriever = _build_retriever([d.as_document() for d in self.declarations])
        if check_cancelled:
            check_cancelled()
        if self._header_retriever is None:
            self._header_retriever = _build_retriever([d.header for d in self.declarations])
        if check_cancelled:
            check_cancelled()
        _save_retriever(self._text_retriever, dest / "text")
        _save_retriever(self._header_retriever, dest / "header")
        self._cache_dir = dest

    @classmethod
    def load_cache(cls, dest: Path, fingerprint: str) -> "LeanSearchIndex | None":
        try:
            meta = json.loads((dest / "meta.json").read_text(encoding="utf-8"))
            if meta.get("fingerprint") != fingerprint or meta.get("version") != CACHE_VERSION:
                return None
            index = cls.from_jsonl((dest / "declarations.jsonl").read_text(encoding="utf-8"), cache_dir=dest)
            if (text := _load_retriever(dest / "text")) is not None:
                index._text_retriever = text
            if (header := _load_retriever(dest / "header")) is not None:
                index._header_retriever = header
        except Exception:
            # Cache files are disposable and may be left incomplete by an old
            # process or library version. The caller will rebuild atomically.
            return None
        return index


# ── Source discovery + file walking ───────────────────────────────────────────

def _extract_source_file(job: tuple[str, Path, str]) -> list[Declaration]:
    library, path, file = job
    try:
        text = path.read_text("utf-8", errors="ignore")
    except OSError:
        return []
    return extract_declarations(text, library=library, file=file)


def _iter_source_files(source_roots: Mapping[str, Path]) -> Iterable[tuple[str, Path]]:
    roots = {name: path.resolve() for name, path in source_roots.items()}
    boundaries = set(roots.values())
    seen: set[Path] = set()
    for library, root in sorted(roots.items()):
        for path in _iter_lean_files(root, excluded_roots=boundaries - {root}):
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                yield library, path


def _iter_lean_files(root: Path, *, excluded_roots: set[Path] | None = None) -> Iterable[Path]:
    if not root.exists():
        return
    for directory, folders, files in os.walk(root):
        folders[:] = sorted(name for name in folders if name not in {
            ".lake", ".git", ".horizon", ".archon-horizon", "agent-library", "node_modules", "_lake"}
            and (not excluded_roots or (Path(directory) / name).resolve() not in excluded_roots))
        for name in sorted(files):
            if name.endswith(".lean"):
                yield Path(directory) / name


def _display_path(path: Path, workspace_root: Path | None) -> str:
    if workspace_root is not None:
        try:
            return str(path.relative_to(workspace_root))
        except ValueError:
            pass
    return str(path)
