"""hgraph — a plain-files semantic graph for autoformalization.

Storage is deliberately boring and human-first:

    <project>/hgraph/
      nodes/<id>.md        one Markdown file per node: YAML header + content body
      nodes/<id>/          sibling dir, created only if the node has attachments
      edges/<src>__<tgt>.md   one file per edge: YAML header (type/hard/note/…)
      edges/<src>__<tgt>/     sibling dir, created only if the edge has attachments

* A **node** is a `.md` file. Its YAML header holds metadata (``type: tex|lean|…``,
  ``title``, ``origin``, ``content_type``, …); its body is the actual content
  ("Let X_1, X_2, … be iid …", a Lean declaration, a proof).
* An **edge** is its own small file named ``<source>__<target>.md`` — one per
  ordered pair. Its YAML header carries ``type`` and whether it is *hard* or
  *soft*:
    - **hard** = a dependency (``uses``) — the DAG you schedule on;
    - **soft** = a semantic link (``formalizes``, ``related_to``) — relates
      different representations/notes of the same object, not a dependency.
* **Comments** (reviews, "tried simp, failed because …") are *attachments*: small
  files under the owner node's sibling directory (``nodes/<id>/comment-N.md``) —
  failure memory that lives with the node, not extra nodes in the graph.

No logs, no databases, no git-machinery. The files *are* the graph; edit them by
hand or through the CLI, and let git version them. Every node gets a stable
opaque hash id — synced nodes hash their ``label`` / Lean ``decl``, hand-added
nodes hash a ``key`` (default: the title). Address any of them without knowing
the hash via ``label:``/``decl:``/``key:`` (see :meth:`Graph.resolve`).

Requires PyYAML.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import threading
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator, Optional

import yaml

# the C loader parses the same YAML ~10x faster than the pure-Python one, and
# header parsing dominates every whole-graph read; fall back transparently
# where libyaml isn't compiled in
try:
    from yaml import CSafeLoader as _YamlLoader
except ImportError:                                   # pragma: no cover
    from yaml import SafeLoader as _YamlLoader

# the one edge type that means "A depends on B" (everything else is a soft/
# semantic link). Both a statement's and a proof's `\uses` become this.
HARD_TYPES = {"uses"}
# the one soft link that asserts two nodes are the SAME conceptual object in
# different forms (1 informal ↔ many formal) — the only kind sync generates.
# ONLY this merges nodes in the union view; other soft links use `related_to`.
IDENTITY_TYPES = {"formalizes"}
# per-node notes ("tried simp, failed …", a review) are ATTACHMENTS — files under
# the node's sibling dir, not edges. Each kind is stored as `<kind>-N.md`.
ATTACHMENT_KINDS = ("comment", "review")


class HGraphError(Exception):
    pass


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def slugify(text: str) -> str:
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    t = re.sub(r"[^a-zA-Z0-9]+", "-", t).strip("-").lower()
    return t or "node"


def node_id(kind: str, key: str) -> str:
    """Stable opaque id for a node: ``sha1("<kind>:<key>")[:12]``. ``kind`` is
    ``bp`` (blueprint, keyed on its LaTeX label), ``lean`` (keyed on its
    fully-qualified name), or ``manual`` (hand-added, keyed on a human ``key``).
    The prefix keeps the populations from colliding and encodes which one it is."""
    return hashlib.sha1(f"{kind}:{key}".encode("utf-8")).hexdigest()[:12]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# timestamps don't count as a "change" — else every sync would rewrite every file
_VOLATILE = frozenset({"created", "updated"})


def _read_doc(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            meta = yaml.load(text[4:end], Loader=_YamlLoader) or {}
            return meta, text[end + 5:]
    return {}, text


def _write_doc(path: Path, meta: dict, body: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = yaml.safe_dump(meta, sort_keys=True, allow_unicode=True,
                        default_flow_style=False).rstrip("\n")
    path.write_text("---\n" + fm + "\n---\n" + body, encoding="utf-8")


# --------------------------------------------------------------------------- #
# value objects
# --------------------------------------------------------------------------- #
@dataclass
class Node:
    id: str
    type: Optional[str] = None
    title: Optional[str] = None
    content: str = ""
    meta: dict = field(default_factory=dict)   # all other frontmatter fields


@dataclass
class Edge:
    id: str
    source: str
    target: str
    type: str
    hard: bool = False
    attrs: dict = field(default_factory=dict)

    @property
    def soft(self) -> bool:
        return not self.hard


# --------------------------------------------------------------------------- #
# the graph
# --------------------------------------------------------------------------- #
class Graph:
    def __init__(self, base: Path):
        self.base = Path(base)
        self.nodes_dir = self.base / "nodes"
        self.edges_dir = self.base / "edges"

    @classmethod
    def open(cls, project: str | Path = ".") -> "Graph":
        """Open (or lazily create) the graph under ``<project>/hgraph``."""
        return cls(Path(project) / "hgraph")

    # ---- paths ------------------------------------------------------------ #
    def _node_path(self, nid: str) -> Path:
        return self.nodes_dir / f"{nid}.md"

    def _node_dir(self, nid: str) -> Path:
        """Sibling directory holding a node's attachments (comments, …)."""
        return self.nodes_dir / nid

    def _edge_path(self, eid: str) -> Path:
        return self.edges_dir / f"{eid}.md"

    def _edge_dir(self, eid: str) -> Path:
        return self.edges_dir / eid

    def has_node(self, nid: str) -> bool:
        return self._node_path(nid).exists()

    def resolve(self, ref: str) -> str:
        """Turn a human-friendly reference into a node id. Accepts a raw id, or
        ``label:<latex-label>`` / ``decl:<lean-fqname>`` / ``key:<manual-key>`` to
        look a node up by its (opaque, hashed) id without knowing the hash. Raises
        if nothing matches, or if the reference is *ambiguous* (several nodes carry
        the same label/decl/key) — rather than silently picking one."""
        if self.has_node(ref):
            return ref
        if ":" in ref:
            field, val = ref.split(":", 1)
            if field in ("label", "decl", "key"):
                hits = [n.id for n in self.nodes() if str(n.meta.get(field)) == val]
                if len(hits) == 1:
                    return hits[0]
                if len(hits) > 1:
                    raise HGraphError(
                        f"'{ref}' is ambiguous — {len(hits)} nodes match: "
                        f"{', '.join(hits)} (address one by its raw id)")
        raise HGraphError(f"no node matching '{ref}'")

    # ---- nodes ------------------------------------------------------------ #
    def add_node(self, title: str, *, type: Optional[str] = None,
                 id: Optional[str] = None, key: Optional[str] = None,
                 content: str = "", author: Optional[str] = None, **meta) -> str:
        """Add a node. Synced callers pass an explicit ``id`` (the precomputed
        hash). Hand-added nodes leave ``id`` unset and get a ``manual`` hash of
        ``key`` (default: ``title``), which is stored so the node stays
        addressable by ``key:<key>``. ``created``/``updated`` are stamped now."""
        if id is not None:
            nid = id
            if key is not None:               # keep a raw-id node addressable by key:
                meta.setdefault("key", key)
        else:
            k = key if key is not None else title
            nid = node_id("manual", k)
            meta.setdefault("key", k)
        if self.has_node(nid):
            raise HGraphError(f"node '{nid}' already exists (use modify)")
        m = {"title": title}
        if type is not None:
            m["type"] = type
        if author is not None:
            m["author"] = author
        m.update({mk: mv for mk, mv in meta.items() if mv is not None})
        now = _now()
        m["created"], m["updated"] = now, now
        _write_doc(self._node_path(nid), m, content)
        return nid

    def get_node(self, nid: str) -> Node:
        p = self._node_path(nid)
        if not p.exists():
            raise HGraphError(f"no such node '{nid}'")
        meta, body = _read_doc(p)
        meta = dict(meta)
        return Node(id=nid, type=meta.pop("type", None),
                    title=meta.pop("title", None), content=body, meta=meta)

    def modify_node(self, nid: str, *, title: Optional[str] = None,
                    type: Optional[str] = None, content: Optional[str] = None,
                    set_meta: Optional[dict] = None,
                    unset: Iterable[str] = (), author: Optional[str] = None) -> None:
        n = self.get_node(nid)

        def snapshot() -> tuple:
            meta = {k: v for k, v in n.meta.items() if k not in _VOLATILE}
            return (n.title, n.type, n.content, meta)

        before = snapshot()
        created = n.meta.get("created")
        if title is not None:
            n.title = title
        if type is not None:
            n.type = type
        if content is not None:
            n.content = content
        if set_meta:
            n.meta.update(set_meta)
        for k in unset:
            n.meta.pop(k, None)
        if author is not None:
            n.meta["author"] = author
        if snapshot() == before:
            return          # nothing meaningful changed → no write, no churn, keep timestamps
        now = _now()
        n.meta["created"], n.meta["updated"] = created or now, now
        m = {"title": n.title} if n.title is not None else {}
        if n.type is not None:
            m["type"] = n.type
        m.update(n.meta)
        _write_doc(self._node_path(nid), m, n.content)

    def delete_node(self, nid: str, *, cascade: bool = True) -> None:
        if not self.has_node(nid):
            return
        if cascade:
            for e in self.edges(source=nid) + self.edges(target=nid):
                self.delete_edge(e.id)
        self._node_path(nid).unlink()
        d = self._node_dir(nid)
        if d.exists():
            shutil.rmtree(d)

    def nodes(self, *, type: Optional[str] = None) -> Iterator[Node]:
        if not self.nodes_dir.exists():
            return
        for p in sorted(self.nodes_dir.glob("*.md")):
            n = self.get_node(p.stem)
            if type is None or n.type == type:
                yield n

    # ---- edges ------------------------------------------------------------ #
    def has_edge(self, source: str, target: str) -> bool:
        return self._edge_path(f"{source}__{target}").exists()

    def add_edge(self, source: str, target: str, type: str, *,
                 hard: Optional[bool] = None, replace: bool = False, **attrs) -> str:
        for end in (source, target):
            if not self.has_node(end):
                raise HGraphError(f"edge endpoint '{end}' is not a node")
        eid = f"{source}__{target}"       # one edge per ordered pair; type is in meta
        # The pair is the identity, so writing a second edge would *silently*
        # overwrite the first (a hard dep could vanish under a soft link). Refuse
        # unless the caller explicitly opts in with ``replace``.
        if not replace and self._edge_path(eid).exists():
            cur = self.get_edge(eid)
            raise HGraphError(
                f"an edge {source}→{target} already exists (type '{cur.type}'); "
                f"only one edge is kept per ordered pair. Pass replace=True "
                f"(CLI: --replace) to overwrite it, or delete it first.")
        if hard is None:
            hard = type in HARD_TYPES
        m = {"source": source, "target": target, "type": type, "hard": bool(hard)}
        m.update({k: v for k, v in attrs.items() if v is not None})
        _write_doc(self._edge_path(eid), m, "")
        return eid

    def get_edge(self, eid: str) -> Edge:
        p = self._edge_path(eid)
        if not p.exists():
            raise HGraphError(f"no such edge '{eid}'")
        m, _ = _read_doc(p)
        m = dict(m)
        try:
            source, target, type_ = m.pop("source"), m.pop("target"), m.pop("type")
        except KeyError as e:
            # a hand-edited file missing a required header key shouldn't take
            # down every whole-graph operation with a bare KeyError
            raise HGraphError(f"edge file '{eid}.md' is missing {e} in its header")
        return Edge(id=eid, source=source, target=target,
                    type=type_, hard=bool(m.pop("hard", False)), attrs=m)

    def delete_edge(self, eid: str) -> None:
        p = self._edge_path(eid)
        if p.exists():
            p.unlink()
        d = self._edge_dir(eid)
        if d.exists():
            shutil.rmtree(d)

    def edges(self, *, source: Optional[str] = None, target: Optional[str] = None,
              type: Optional[str] = None, hard: Optional[bool] = None) -> list[Edge]:
        if not self.edges_dir.exists():
            return []
        out = []
        for p in sorted(self.edges_dir.glob("*.md")):
            e = self.get_edge(p.stem)
            if source is not None and e.source != source:
                continue
            if target is not None and e.target != target:
                continue
            if type is not None and e.type != type:
                continue
            if hard is not None and e.hard != hard:
                continue
            out.append(e)
        return out

    # ---- attachments: comments, reviews (files under the node's data dir) -- #
    @staticmethod
    def _attach_num(p: Path) -> int:
        m = re.search(r"-(\d+)$", p.stem)
        return int(m.group(1)) if m else 0

    def _attach_files(self, nid: str, kind: str) -> list[Path]:
        d = self._node_dir(nid)
        if not d.exists():
            return []
        return sorted(d.glob(f"{kind}-*.md"), key=self._attach_num)

    # Dashboard/API workers may write attachments concurrently; the
    # read-max-then-write numbering below must be atomic or two writes to the
    # same node pick the same N and one silently vanishes.
    _attach_lock = threading.Lock()

    def add_attachment(self, target: str, kind: str, content: str, *,
                       author: Optional[str] = None, **meta) -> str:
        """Attach a note of ``kind`` (``comment`` / ``review``) to a node: a file
        ``nodes/<target>/<kind>-N.md``. Not a node, not an edge. Extra metadata
        (``title``, ``confidence``, …) is stored in the header."""
        if not self.has_node(target):
            raise HGraphError(f"no such node '{target}'")
        m = {}
        if author is not None:
            m["author"] = author
        m.update({k: v for k, v in meta.items() if v is not None})
        now = _now()
        m["created"] = m["updated"] = m["date"] = now   # date kept for back-compat
        with self._attach_lock:
            existing = self._attach_files(target, kind)
            n = (self._attach_num(existing[-1]) + 1) if existing else 1
            _write_doc(self._node_dir(target) / f"{kind}-{n}.md", m, content)
        return f"{target}/{kind}-{n}"

    def attachments(self, nid: str, kind: str) -> list[Node]:
        out = []
        for p in self._attach_files(nid, kind):
            meta, body = _read_doc(p)
            out.append(Node(id=p.stem, type=kind, content=body, meta=dict(meta)))
        return out

    def delete_attachment(self, nid: str, kind: str, n: int) -> None:
        """Remove one attachment file ``nodes/<nid>/<kind>-<n>.md``. Numbers of the
        surviving attachments are left as-is (stable ids; no renumber cascade)."""
        p = self._node_dir(nid) / f"{kind}-{n}.md"
        if not p.exists():
            raise HGraphError(f"no {kind} #{n} on node '{nid}'")
        p.unlink()

    # convenience wrappers
    def add_comment(self, target: str, content: str, *,
                    author: Optional[str] = None) -> str:
        return self.add_attachment(target, "comment", content, author=author)

    def comments(self, nid: str) -> list[Node]:
        return self.attachments(nid, "comment")

    # ---- neighbourhood / traversal ---------------------------------------- #
    def successors(self, nid: str, *, type: Optional[str] = None,
                   hard: Optional[bool] = None) -> list[Edge]:
        return self.edges(source=nid, type=type, hard=hard)

    def predecessors(self, nid: str, *, type: Optional[str] = None,
                     hard: Optional[bool] = None) -> list[Edge]:
        return self.edges(target=nid, type=type, hard=hard)

    def _reach(self, start: str, *, forward: bool, hard: Optional[bool]) -> list[str]:
        # one edges() pass up front: calling self.edges() per visited node
        # re-reads and YAML-parses every edge file each time — O(V·E) disk
        # reads, seconds on a thousand-statement blueprint
        adj: dict[str, list[str]] = {}
        for e in self.edges(hard=hard):
            if forward:
                adj.setdefault(e.source, []).append(e.target)
            else:
                adj.setdefault(e.target, []).append(e.source)
        seen, order, stack = {start}, [], [start]
        while stack:
            cur = stack.pop()
            for nxt in adj.get(cur, ()):
                if nxt not in seen:
                    seen.add(nxt)
                    order.append(nxt)
                    stack.append(nxt)
        return order

    def ancestors(self, nid: str) -> list[str]:
        """Everything ``nid`` transitively depends on (follows hard edges out).
        Convention: ``a --uses--> b`` ⇒ b is an ancestor of a."""
        return self._reach(nid, forward=True, hard=True)

    def descendants(self, nid: str) -> list[str]:
        """Everything that transitively depends on ``nid`` (hard edges, inward)."""
        return self._reach(nid, forward=False, hard=True)

    # ---- the union / soft-cluster view ------------------------------------ #
    def soft_components(self) -> list[set[str]]:
        """Connected components under *identity* links (``formalizes``) — each
        is one conceptual object that may bundle a tex statement with several
        Lean formalizations. Dependencies, ``related_to`` links, and comments
        do NOT merge nodes (they relate distinct objects)."""
        parent: dict[str, str] = {n.id: n.id for n in self.nodes()}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: str, b: str) -> None:
            parent[find(a)] = find(b)

        for e in self.edges():
            if e.type in IDENTITY_TYPES:
                if e.source in parent and e.target in parent:
                    union(e.source, e.target)
        comps: dict[str, set[str]] = {}
        for nid in parent:
            comps.setdefault(find(nid), set()).add(nid)
        return list(comps.values())

    def union_view(self) -> tuple[list[set[str]], list[tuple[int, int, str]]]:
        """Return (supernodes, super_edges). Supernodes are soft-clusters;
        super_edges lift each hard dependency to (cluster_i, cluster_j, type)."""
        comps = self.soft_components()
        idx = {nid: i for i, comp in enumerate(comps) for nid in comp}
        seen, super_edges = set(), []
        for e in self.edges(hard=True):
            i, j = idx.get(e.source), idx.get(e.target)
            if i is None or j is None or i == j:
                continue
            key = (i, j, e.type)
            if key not in seen:
                seen.add(key)
                super_edges.append(key)
        return comps, super_edges
