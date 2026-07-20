"""Dependency-closure analysis: what's proven, what's ready, what's blocked.

``lean_status`` on a node is *local* — its own Lean code. The question an agent
walking the graph actually asks is *transitive*: is everything this node depends
on also done (⇒ **closed**), or are its prerequisites done so it is the next
thing worth working on (⇒ **ready**)? This module derives those states.

It builds the hard-dependency graph **once, in memory** — repeating the on-disk
edge scan per node (as ``ancestors`` does) is O(N·E) and far too slow on a
thousands-of-nodes blueprint. Closure is an iterative (cycle-safe) DFS, so it
never risks a recursion overflow on a deep chain.
"""

from __future__ import annotations

from collections import Counter, deque
from typing import Optional

from .graph import Graph

# a node is *locally* done when its own Lean side carries no sorry
DONE = {"lean_ok", "mathlib_ok"}

# the five states, mirroring a leanblueprint summary
#   closed          — this node and its whole prerequisite closure are done
#   formalized_open — locally done, but some prerequisite is still open
#   ready           — not done, yet every prerequisite is closed → workable now
#   blocked         — not done, and a prerequisite is still open
#   (informal is a lens over the not-done nodes: lean_status == "empty")
STATES = ("closed", "ready", "formalized_open", "blocked")


class Analysis:
    def __init__(self, g: Graph):
        self.nodes = {n.id: n for n in g.nodes()}
        self.deps: dict[str, list[str]] = {i: [] for i in self.nodes}   # i → what it needs
        self.rdeps: dict[str, list[str]] = {i: [] for i in self.nodes}  # i → who needs it
        for e in g.edges(hard=True):
            if e.source in self.nodes and e.target in self.nodes and e.source != e.target:
                self.deps[e.source].append(e.target)
                self.rdeps[e.target].append(e.source)
        self.local = {i: (n.meta.get("lean_status") in DONE)
                      for i, n in self.nodes.items()}
        self.closed = self._closure()
        self.states = {i: self._state(i) for i in self.nodes}

    # ---- transitive closure (iterative white/grey/black DFS) -------------- #
    def _closure(self) -> dict[str, bool]:
        WHITE, GREY, BLACK = 0, 1, 2
        color = {i: WHITE for i in self.nodes}
        closed: dict[str, bool] = {}
        for start in self.nodes:
            if color[start] != WHITE:
                continue
            stack = [start]
            while stack:
                u = stack[-1]
                if color[u] == WHITE:
                    color[u] = GREY
                    for v in self.deps[u]:
                        if color[v] == WHITE:
                            stack.append(v)
                else:
                    stack.pop()
                    if color[u] == BLACK:
                        continue
                    if not self.local[u]:
                        closed[u] = False
                    else:
                        # closed iff every dep resolved & closed. A GREY dep is
                        # a cycle back-edge: count it as its own *local* state,
                        # matching layout._closures and the frontend's closure —
                        # otherwise a fully-formalized cyclic cluster reads
                        # "done" in the web graph but "open" in the CLI.
                        closed[u] = all((self.local[v] if color[v] == GREY
                                         else closed.get(v, False))
                                        for v in self.deps[u])
                    color[u] = BLACK
        return closed

    def _state(self, i: str) -> str:
        if self.closed[i]:
            return "closed"
        if self.local[i]:
            return "formalized_open"
        if all(self.closed[d] for d in self.deps[i]):
            return "ready"
        return "blocked"

    # ---- impact: how many still-open nodes a proof would unblock ---------- #
    def unlocks(self, i: str) -> int:
        seen: set[str] = set()
        dq = deque(self.rdeps[i])
        while dq:
            x = dq.popleft()
            if x in seen:
                continue
            seen.add(x)
            dq.extend(self.rdeps[x])
        return sum(1 for x in seen if not self.closed[x])

    # ---- summaries -------------------------------------------------------- #
    def state_counts(self) -> dict:
        c = Counter(self.states.values())
        informal = sum(1 for n in self.nodes.values()
                       if (n.meta.get("lean_status") or "empty") == "empty")
        return {**{s: c.get(s, 0) for s in STATES}, "informal": informal}

    def row(self, i: str) -> dict:
        n = self.nodes[i]
        return {"id": i, "title": n.title, "type": n.type,
                "kind": n.meta.get("content_type"),
                "lean_status": n.meta.get("lean_status") or "empty",
                "state": self.states[i],
                "direct_uses": len(self.deps[i]),
                "unlocks": self.unlocks(i)}

    def frontier(self) -> list[dict]:
        """The actionable list: every ``ready`` node, ranked by how much finishing
        it would unblock downstream (then by how much it itself leans on)."""
        rows = [self.row(i) for i, s in self.states.items() if s == "ready"]
        rows.sort(key=lambda r: (-r["unlocks"], -r["direct_uses"], r["title"] or ""))
        return rows
