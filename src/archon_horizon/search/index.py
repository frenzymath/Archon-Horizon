"""Extract Lean declarations and search them with BM25 / name / type matching.

Everything here is pure Python and offline. The expensive step — walking and
parsing thousands of ``.lean`` files — is cached as JSONL under the workspace
state dir and only redone when the sources change, so a query is fast.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

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

    def as_document(self) -> str:
        """The text BM25 indexes: name + signature + docstring."""
        spaced_name = self.name.replace(".", " ")
        return f"{spaced_name}\n{self.signature}\n{self.doc}"


def _read_docstring(lines: list[str], i: int) -> tuple[str | None, int]:
    """If line ``i`` opens a ``/-- … -/`` docstring, return (text, end_index)."""
    line = lines[i].lstrip()
    if not line.startswith("/--"):
        return None, i
    buf = [line[3:]]
    if "-/" in line[3:]:
        return buf[0].split("-/", 1)[0].strip(), i
    j = i + 1
    while j < len(lines):
        if "-/" in lines[j]:
            buf.append(lines[j].split("-/", 1)[0])
            return " ".join(s.strip() for s in buf).strip(), j
        buf.append(lines[j])
        j += 1
    return " ".join(s.strip() for s in buf).strip(), len(lines) - 1


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
    out: list[Declaration] = []
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if not stripped:
            i += 1
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


# ── BM25 ──────────────────────────────────────────────────────────────────────

class _BM25:
    """Okapi BM25 over an in-memory inverted index (pure Python)."""

    def __init__(self, documents: list[str], *, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self.postings: dict[str, list[tuple[int, int]]] = {}
        self.doc_len: list[int] = []
        df: dict[str, int] = {}
        for doc_id, text in enumerate(documents):
            tf: dict[str, int] = {}
            for tok in _tokenize(text):
                tf[tok] = tf.get(tok, 0) + 1
            self.doc_len.append(sum(tf.values()))
            for tok, count in tf.items():
                self.postings.setdefault(tok, []).append((doc_id, count))
                df[tok] = df.get(tok, 0) + 1
        self.n = len(documents)
        self.avgdl = (sum(self.doc_len) / self.n) if self.n else 0.0
        self.idf = {
            tok: math.log(1 + (self.n - n_q + 0.5) / (n_q + 0.5)) for tok, n_q in df.items()
        }

    def search(self, query: str) -> dict[int, float]:
        scores: dict[int, float] = {}
        for tok in set(_tokenize(query)):
            postings = self.postings.get(tok)
            if not postings:
                continue
            idf = self.idf[tok]
            for doc_id, freq in postings:
                dl = self.doc_len[doc_id] or 1
                denom = freq + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1))
                scores[doc_id] = scores.get(doc_id, 0.0) + idf * (freq * (self.k1 + 1)) / denom
        return scores


# ── Name / type matching (Loogle-style) ──────────────────────────────────────

def _name_score(query: str, name: str) -> float:
    q, n = query.lower(), name.lower()
    if not q:
        return 0.0
    segment = n.rsplit(".", 1)[-1]
    if q == n or q == segment:
        return 100.0
    if segment.startswith(q):
        return 80.0 - (len(segment) - len(q)) * 0.1
    if q in n:
        return 60.0 - (len(n) - len(q)) * 0.05
    # subsequence fallback (fuzzy)
    it = iter(n)
    if all(ch in it for ch in q):
        return 20.0 - len(n) * 0.01
    return 0.0


def _type_pattern_to_regex(pattern: str) -> re.Pattern[str]:
    norm = pattern.replace("→", "->").replace("⟶", "->")
    # Split into wildcard runs (?ident / _) vs literal chunks.
    pieces: list[str] = []
    for tok in re.split(r"(\?[A-Za-z0-9_']+|(?<![A-Za-z0-9_'])_(?![A-Za-z0-9_']))", norm):
        if not tok:
            continue
        if tok.startswith("?") or tok == "_":
            pieces.append(".*?")
        else:
            pieces.append(re.escape(tok.strip()).replace(r"\ ", r"\s*"))
    return re.compile(".*?".join(p for p in pieces if p), re.DOTALL)


def _normalize_sig(signature: str) -> str:
    return re.sub(r"\s+", " ", signature.replace("→", "->").replace("⟶", "->"))


@dataclass(frozen=True, slots=True)
class SearchHit:
    declaration: Declaration
    score: float


# ── The index ─────────────────────────────────────────────────────────────────

class LeanSearchIndex:
    """Build (and cache) a declaration index, then query it three ways."""

    def __init__(self, declarations: list[Declaration]) -> None:
        self.declarations = declarations
        self._bm25: _BM25 | None = None

    # -- construction --

    @classmethod
    def build(cls, source_roots: dict[str, Path], *, workspace_root: Path | None = None) -> "LeanSearchIndex":
        """Extract declarations from ``{library_name: root_dir}``."""
        decls: list[Declaration] = []
        for library, root in source_roots.items():
            for path in _iter_lean_files(root):
                try:
                    text = path.read_text("utf-8", errors="ignore")
                except OSError:
                    continue
                file = _display_path(path, workspace_root)
                decls.extend(extract_declarations(text, library=library, file=file))
        return cls(decls)

    @property
    def bm25(self) -> _BM25:
        if self._bm25 is None:
            self._bm25 = _BM25([d.as_document() for d in self.declarations])
        return self._bm25

    # -- queries --

    def search_text(self, query: str, *, limit: int = 10, library: str | None = None) -> list[SearchHit]:
        scores = self.bm25.search(query)
        hits = [
            SearchHit(self.declarations[i], s)
            for i, s in scores.items()
            if library is None or self.declarations[i].library == library
        ]
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:limit]

    def search_name(self, query: str, *, limit: int = 10, library: str | None = None) -> list[SearchHit]:
        hits: list[SearchHit] = []
        for d in self.declarations:
            if library is not None and d.library != library:
                continue
            score = _name_score(query, d.name)
            if score > 0:
                hits.append(SearchHit(d, score))
        hits.sort(key=lambda h: (h.score, -len(h.declaration.name)), reverse=True)
        return hits[:limit]

    def search_type(self, pattern: str, *, limit: int = 10, library: str | None = None) -> list[SearchHit]:
        regex = _type_pattern_to_regex(pattern)
        hits: list[SearchHit] = []
        for d in self.declarations:
            if library is not None and d.library != library:
                continue
            sig = _normalize_sig(d.signature)
            if regex.search(sig):
                # Prefer tighter matches: shorter signatures rank higher.
                hits.append(SearchHit(d, 1000.0 / (len(sig) + 1)))
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:limit]

    # -- caching --

    def to_jsonl(self) -> str:
        return "\n".join(json.dumps(asdict(d)) for d in self.declarations)

    @classmethod
    def from_jsonl(cls, text: str) -> "LeanSearchIndex":
        decls = [Declaration(**json.loads(line)) for line in text.splitlines() if line.strip()]
        return cls(decls)


# ── Source discovery + file walking ───────────────────────────────────────────

def _iter_lean_files(root: Path) -> Iterable[Path]:
    if not root.exists():
        return
    for path in root.rglob("*.lean"):
        # Skip anything under a nested ``.lake`` (build artifacts and a package's
        # own vendored deps). External-library roots point *at* the package dir,
        # so its sources sit above any nested ``.lake`` and are kept.
        if ".lake" in path.relative_to(root).parts:
            continue
        yield path


def _display_path(path: Path, workspace_root: Path | None) -> str:
    if workspace_root is not None:
        try:
            return str(path.relative_to(workspace_root))
        except ValueError:
            pass
    return str(path)
