"""Implementation behind ``horizon graph``.

    horizon graph add node   --title "théorème central limite" --type tex
    horizon graph add edge   theorem-clt clt-lean --type formalizes
    horizon graph add comment clt-lean --content "tried simp, failed because ..."
    horizon graph get        clt-lean
    horizon graph frontier   --type tex

The Horizon wrapper selects the project and supplies its root.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from .analysis import Analysis
from .graph import Graph, HGraphError
from .render import (edge_json, get_json, node_json, render_get, stats_json,
                     view_dot, view_text)


def _kv(pairs, reserved=()) -> dict:
    """Parse repeated ``key=value`` options; values are YAML scalars. Keys in
    ``reserved`` have a dedicated flag and would collide with it, so reject them
    with a clear message instead of crashing on a duplicate keyword argument."""
    out = {}
    for p in pairs or ():
        if "=" not in p:
            raise SystemExit(f"--set expects key=value, got '{p}'")
        k, v = p.split("=", 1)
        if k in reserved:
            raise SystemExit(f"--set {k}=… collides with the dedicated --{k} flag; "
                             f"use --{k} instead")
        out[k] = yaml.safe_load(v)
    return out


def _scalar(v):
    """Parse a CLI value as a YAML scalar (so ``0.8`` → float, ``high`` → str)."""
    return yaml.safe_load(v) if v is not None else None


def _content(args) -> str | None:
    if getattr(args, "content_file", None):
        if args.content_file == "-":
            return sys.stdin.read()
        return Path(args.content_file).read_text(encoding="utf-8")
    return getattr(args, "content", None)


def _emit_json(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _limited(items: list, args) -> tuple[list, int]:
    """Apply ``--limit N`` to a result list; return (shown, total)."""
    total = len(items)
    n = getattr(args, "limit", None)
    return (items[:n] if n is not None else items), total


def _filter_nodes(g: Graph, args) -> list:
    """The queryable node list behind ``horizon graph list`` — every filter is ANDed,
    then sorted by ``--sort``."""
    def tags_of(n):
        t = n.meta.get("tags") or []
        return [t] if isinstance(t, str) else list(t)

    out = []
    for n in g.nodes(type=args.type):
        if args.status and n.meta.get("status") != args.status:
            continue
        if args.lean_status and n.meta.get("lean_status") != args.lean_status:
            continue
        if args.tag and args.tag not in tags_of(n):
            continue
        if args.stale and not n.meta.get("stale"):
            continue
        if args.generated:
            gen = n.meta.get("generated")
            if (args.generated == "manual" and gen) or \
               (args.generated != "manual" and gen != args.generated):
                continue
        if args.match and args.match.lower() not in \
                ((n.title or "") + "\n" + n.content).lower():
            continue
        out.append(n)
    keyf = {
        "id": lambda n: n.id,
        "title": lambda n: (n.title or "").lower(),
        "type": lambda n: (n.type or "", n.id),
        "chapter": lambda n: (str(n.meta.get("chapter") or "~"), n.meta.get("order")
                              if isinstance(n.meta.get("order"), int) else 0),
        "order": lambda n: n.meta.get("order") if isinstance(n.meta.get("order"), int) else 1 << 30,
    }[args.sort]
    out.sort(key=keyf)
    return out


def build_parser(*, prog: str = "horizon graph") -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=prog, description="Horizon's plain-files semantic graph")
    # Internal transport from the Horizon wrapper; project selection is exposed
    # as `horizon graph --project`, not as a second root option.
    p.add_argument("--root", default=".", help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="cmd", required=True)

    # ---- add {node,edge,comment} ----------------------------------------- #
    add = sub.add_parser("add", help="add a node, edge, or comment").add_subparsers(
        dest="what", required=True)

    an = add.add_parser("node", help="add a node")
    an.add_argument("--title", required=True)
    an.add_argument("--type", help="tex, lean, informal, ...")
    an.add_argument("--key", help="human key hashed into the id (default: title); "
                                  "address the node later by key:<key>")
    an.add_argument("--id", help="force a raw id (escape hatch; skips hashing)")
    an.add_argument("--content")
    an.add_argument("--content-file", help="read content from a file (or - for stdin)")
    an.add_argument("--origin")
    an.add_argument("--author", help="who created it (a name, a tool, …)")
    an.add_argument("--content-type", dest="content_type")
    an.add_argument("--set", action="append", metavar="k=v", help="extra metadata")

    ae = add.add_parser("edge", help="add an edge")
    ae.add_argument("source")
    ae.add_argument("target")
    ae.add_argument("--type", required=True, help="uses (hard), formalizes (identity), related_to (associative)")
    g = ae.add_mutually_exclusive_group()
    g.add_argument("--hard", action="store_true", help="force dependency edge")
    g.add_argument("--soft", action="store_true", help="force semantic (non-dep) edge")
    ae.add_argument("--replace", action="store_true",
                    help="overwrite an edge already present on this ordered pair "
                         "(only one edge is kept per pair)")
    ae.add_argument("--set", action="append", metavar="k=v")

    ac = add.add_parser("comment", help="attach a freeform comment to a node")
    ac.add_argument("target")
    ac.add_argument("--content")
    ac.add_argument("--content-file")
    ac.add_argument("--author")
    ac.add_argument("--title", help="short heading for the note")
    ac.add_argument("--set", action="append", metavar="k=v", help="extra metadata")

    ar = add.add_parser("review", help="attach a Maths/Lean good-or-bad review to a node")
    ar.add_argument("target")
    ar.add_argument("--author")
    ar.add_argument("--maths", choices=["good", "bad"], help="is the mathematics right")
    ar.add_argument("--maths-comment", dest="maths_comment")
    ar.add_argument("--lean", choices=["good", "bad"], help="is the Lean formalization right")
    ar.add_argument("--lean-comment", dest="lean_comment")
    ar.add_argument("--set", action="append", metavar="k=v", help="extra metadata")

    # ---- get / modify / delete / list ------------------------------------ #
    gp = sub.add_parser("get", help="render a node (content + links + deps + notes)")
    gp.add_argument("id")
    gp.add_argument("--json", action="store_true", help="emit the full neighbourhood as JSON")

    mo = sub.add_parser("modify", help="modify a node").add_subparsers(
        dest="what", required=True).add_parser("node")
    mo.add_argument("id")
    mo.add_argument("--title")
    mo.add_argument("--type")
    mo.add_argument("--content")
    mo.add_argument("--content-file")
    mo.add_argument("--set", action="append", metavar="k=v")
    mo.add_argument("--unset", action="append", metavar="key", default=[])

    de = sub.add_parser("delete", help="delete a node, edge, comment, or review"
                        ).add_subparsers(dest="what", required=True)
    de.add_parser("node").add_argument("id")
    de.add_parser("edge").add_argument("id", help="the edge id 'source__target' (see `hgraph edges`)")
    for kind in ("comment", "review"):
        dk = de.add_parser(kind, help=f"delete one {kind} attached to a node")
        dk.add_argument("target", help="the node the note is attached to")
        dk.add_argument("--n", type=int, required=True,
                        help=f"which {kind} to delete (its number, shown by `get`)")

    ls = sub.add_parser("list", help="list / query nodes")
    ls.add_argument("--type", help="tex, lean, …")
    ls.add_argument("--status", help="filter by the authored `status` field")
    ls.add_argument("--lean-status", dest="lean_status",
                    help="mathlib_ok | lean_ok | sorry | empty")
    ls.add_argument("--tag", help="only nodes carrying this tag")
    ls.add_argument("--match", help="case-insensitive substring in title or content")
    ls.add_argument("--stale", action="store_true", help="only nodes whose source vanished")
    ls.add_argument("--generated", choices=["blueprint", "lean", "manual"],
                    help="filter by provenance (manual = hand-added)")
    ls.add_argument("--state", choices=["closed", "ready", "blocked",
                                        "formalized_open", "informal"],
                    help="dependency-closure state (see `frontier`/`stats`)")
    ls.add_argument("--sort", choices=["id", "title", "type", "chapter", "order"],
                    default="id", help="sort key (default: id)")
    ls.add_argument("--limit", type=int, metavar="N", help="show only the first N")
    ls.add_argument("--json", action="store_true", help="emit JSON")

    el = sub.add_parser("edges", help="list / query edges")
    el.add_argument("--source", help="node ref (id or label:/decl:/key:)")
    el.add_argument("--target", help="node ref (id or label:/decl:/key:)")
    el.add_argument("--type")
    elg = el.add_mutually_exclusive_group()
    elg.add_argument("--hard", action="store_true"); elg.add_argument("--soft", action="store_true")
    el.add_argument("--limit", type=int, metavar="N", help="show only the first N")
    el.add_argument("--json", action="store_true", help="emit JSON")

    for name in ("ancestors", "descendants"):
        q = sub.add_parser(name, help=f"transitive {name} over dependency edges")
        q.add_argument("id")
        q.add_argument("--names", action="store_true", help="show titles too")
        q.add_argument("--limit", type=int, metavar="N", help="show only the first N")
        q.add_argument("--json", action="store_true", help="emit JSON")

    stp = sub.add_parser("stats", help="summary counts (types, lean_status, "
                         "closure states, reviews, stale)")
    stp.add_argument("--json", action="store_true", help="emit JSON")

    fr = sub.add_parser("frontier", help="what to work on next: nodes whose "
                        "prerequisites are done, ranked by downstream impact")
    fr.add_argument("--type", help="restrict to a node type (e.g. tex)")
    fr.add_argument("--limit", type=int, metavar="N", help="show only the first N")
    fr.add_argument("--json", action="store_true", help="emit JSON")

    vw = sub.add_parser("view", help="tex | lean | union view")
    vw.add_argument("kind", choices=["tex", "lean", "union"])
    vw.add_argument("--dot", action="store_true", help="emit Graphviz DOT")

    sy = sub.add_parser(
        "sync", help="parse a blueprint/Lean source and (re)generate nodes+edges")
    sy.add_argument("--blueprint", help="path to the leanblueprint .tex")
    sy.add_argument("--lean", action="append", default=[], metavar="PATH",
                    help="a .lean file or a directory of them (repeatable)")

    return p


def main(argv=None, *, prog: str = "horizon graph") -> int:
    args = build_parser(prog=prog).parse_args(argv)
    g = Graph.open(args.root)

    try:
        if args.cmd == "add" and args.what == "node":
            nid = g.add_node(
                args.title, type=args.type, id=args.id, key=args.key,
                content=_content(args) or "", author=args.author,
                origin=args.origin, content_type=args.content_type,
                **_kv(args.set, {"title", "type", "id", "key", "content",
                                 "author", "origin", "content_type"}))
            print(nid)

        elif args.cmd == "add" and args.what == "edge":
            hard = True if args.hard else (False if args.soft else None)
            print(g.add_edge(g.resolve(args.source), g.resolve(args.target),
                             args.type, hard=hard, replace=args.replace,
                             **_kv(args.set, {"source", "target", "type", "hard"})))

        elif args.cmd == "add" and args.what == "comment":
            c = _content(args)
            if not c:
                raise SystemExit("comment needs --content or --content-file")
            extra = {"title": args.title, **_kv(args.set, {"author", "title"})}
            print(g.add_attachment(g.resolve(args.target), "comment", c,
                                   author=args.author,
                                   **{k: v for k, v in extra.items() if v is not None}))

        elif args.cmd == "add" and args.what == "review":
            if not args.maths and not args.lean:
                raise SystemExit("review needs --maths good|bad and/or --lean good|bad")
            extra = {"maths_verdict": args.maths, "maths_comment": args.maths_comment,
                     "lean_verdict": args.lean, "lean_comment": args.lean_comment,
                     **_kv(args.set, {"author", "maths_verdict", "maths_comment",
                                      "lean_verdict", "lean_comment"})}
            print(g.add_attachment(g.resolve(args.target), "review", "",
                                   author=args.author,
                                   **{k: v for k, v in extra.items() if v is not None}))

        elif args.cmd == "get":
            nid = g.resolve(args.id)
            _emit_json(get_json(g, nid)) if args.json else print(render_get(g, nid))

        elif args.cmd == "modify":
            # same reservation `add node` applies: title/type/content have
            # real flags; letting --set title=x write them through set_meta
            # would silently shadow the node's actual title field
            g.modify_node(g.resolve(args.id), title=args.title, type=args.type,
                          content=_content(args),
                          set_meta=_kv(args.set, {"title", "type", "content", "id"}),
                          unset=args.unset)
            print(f"modified {args.id}")

        elif args.cmd == "delete" and args.what == "node":
            nid = g.resolve(args.id)
            generated = g.get_node(nid).meta.get("generated")
            g.delete_node(nid); print(f"deleted node {args.id}")
            if generated:
                print("  note: this node is derived from source; the next `sync` "
                      "will recreate it (without its comments). Remove it from the "
                      "blueprint/Lean source, or leave it to be marked stale instead.",
                      file=sys.stderr)
        elif args.cmd == "delete" and args.what == "edge":
            g.delete_edge(args.id); print(f"deleted edge {args.id}")
        elif args.cmd == "delete" and args.what in ("comment", "review"):
            g.delete_attachment(g.resolve(args.target), args.what, args.n)
            print(f"deleted {args.what} #{args.n} on {args.target}")

        elif args.cmd == "list":
            picked = _filter_nodes(g, args)
            if args.state:                       # closure state → needs the graph analysis
                a = Analysis(g)
                if args.state == "informal":
                    picked = [n for n in picked
                              if (n.meta.get("lean_status") or "empty") == "empty"]
                else:
                    picked = [n for n in picked if a.states.get(n.id) == args.state]
            nodes, total = _limited(picked, args)
            if args.json:
                _emit_json([node_json(n) for n in nodes])
            else:
                for n in nodes:
                    flags = " (stale)" if n.meta.get("stale") else ""
                    print(f"{n.id}\t[{n.type or '?'}]\t{n.title or ''}{flags}")
                if len(nodes) < total:
                    print(f"… {len(nodes)} of {total} (use --limit to change)",
                          file=sys.stderr)

        elif args.cmd == "edges":
            hard = True if args.hard else (False if args.soft else None)
            src = g.resolve(args.source) if args.source else None
            tgt = g.resolve(args.target) if args.target else None
            found = g.edges(source=src, target=tgt, type=args.type, hard=hard)
            edges, total = _limited(found, args)
            if args.json:
                _emit_json([edge_json(e) for e in edges])
            else:
                for e in edges:
                    kind = "hard" if e.hard else "soft"
                    print(f"{e.id}\t{e.source} --{e.type}({kind})--> {e.target}")
                if len(edges) < total:
                    print(f"… {len(edges)} of {total} (use --limit to change)",
                          file=sys.stderr)

        elif args.cmd in ("ancestors", "descendants"):
            ids, total = _limited(getattr(g, args.cmd)(g.resolve(args.id)), args)
            if args.json:
                _emit_json([{"id": i, "title": g.get_node(i).title} for i in ids]
                           if args.names else ids)
            else:
                for i in ids:
                    print(f"{i}\t{g.get_node(i).title or ''}" if args.names else i)
                if len(ids) < total:
                    print(f"… {len(ids)} of {total} (use --limit to change)",
                          file=sys.stderr)

        elif args.cmd == "stats":
            s = stats_json(g)
            s["closure"] = Analysis(g).state_counts()
            if args.json:
                _emit_json(s)
            else:
                print(f"nodes: {s['nodes']}   edges: {s['edges']} "
                      f"({s['hard_edges']} hard)   reviewed: {s['reviewed']}"
                      + (f"   stale: {s['stale']}" if s['stale'] else ""))
                c = s["closure"]
                print(f"  closure: closed={c['closed']}, ready={c['ready']}, "
                      f"blocked={c['blocked']}, formalized-but-open={c['formalized_open']}, "
                      f"informal={c['informal']}")
                for label, key in (("by type", "by_type"),
                                   ("tex lean_status", "tex_lean_status"),
                                   ("by status", "by_status")):
                    if s[key]:
                        print(f"  {label}: "
                              + ", ".join(f"{k}={v}" for k, v in s[key].items()))

        elif args.cmd == "frontier":
            rows = Analysis(g).frontier()
            if args.type:
                rows = [r for r in rows if r["type"] == args.type]
            rows, total = _limited(rows, args)
            if args.json:
                _emit_json(rows)
            else:
                if not rows:
                    print("frontier empty — nothing is unblocked "
                          "(all remaining work has open prerequisites).")
                for r in rows:
                    kind = f"({r['kind']})" if r['kind'] else ""
                    print(f"  {r['unlocks']:>4} unlocks · {r['direct_uses']:>2} uses  "
                          f"[{r['lean_status']:<10}] {r['title'] or r['id']} {kind}")
                if len(rows) < total:
                    print(f"… {len(rows)} of {total} ready (use --limit to change)",
                          file=sys.stderr)

        elif args.cmd == "view":
            print(view_dot(g, args.kind) if args.dot else view_text(g, args.kind))

        elif args.cmd == "sync":
            from .sync import sync, load_config
            blueprint, lean = args.blueprint, list(args.lean)
            if not blueprint and not lean:          # fall back to config
                cfg = load_config(args.root)
                blueprint, lean = cfg["blueprint"], cfg["lean"]
            if not blueprint and not lean:
                print("nothing to sync: no --blueprint / --lean given, and no "
                      "hgraph/config.yaml found.\n"
                      "  pass e.g.  horizon graph sync --lean Lean --blueprint blueprint/main.tex\n"
                      "  or create  <root>/hgraph/config.yaml  with `blueprint:` / `lean:` keys.",
                      file=sys.stderr)
                return 2
            r = sync(g, blueprint=blueprint, lean_paths=lean, root=args.root)
            print(f"synced: {r['blueprint']} blueprint node(s), "
                  f"{r['lean']} lean node(s), {r['edges']} generated edge(s)"
                  + (f", {r['stale']} marked stale" if r['stale'] else ""))
            for w in r["warnings"]:
                print(f"  warning: {w}", file=sys.stderr)

    except HGraphError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
