"""Send local hgraph reviews/comments to collaborators through GitHub issues.

The repository's default branch is the comparison baseline. Each local
attachment is hashed as a Git blob and compared with the matching path in the
GitHub tree, so both untracked additions and locally modified attachments are
included without requiring a commit or ``git fetch``.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

import yaml

from .dashboard import _normalize_repo
from .graph import Graph, HGraphError, Node, _read_doc
from .sync import load_config


@dataclass(frozen=True)
class PendingAttachment:
    kind: str
    path: Path
    repo_path: str
    target: Node
    meta: dict
    content: str
    blob_sha: str
    remote_sha: str | None


def _run(args: list[str], *, cwd: Path, input_text: str | None = None) -> str:
    try:
        p = subprocess.run(args, cwd=cwd, input=input_text, text=True,
                           capture_output=True, check=True)
    except FileNotFoundError:
        raise HGraphError(
            f"required command not found: {args[0]!r}; install and authenticate GitHub CLI (`gh`)")
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or e.stdout or str(e)).strip()
        raise HGraphError(f"{' '.join(args[:3])} failed: {detail}")
    return p.stdout.strip()


def _git_blob_sha(raw: bytes) -> str:
    header = f"blob {len(raw)}\0".encode("ascii")
    return hashlib.sha1(header + raw).hexdigest()


def _git_root(root: Path) -> Path:
    return Path(_run(["git", "rev-parse", "--show-toplevel"], cwd=root)).resolve()


def _repo_name(root: Path, override: str | None) -> str:
    configured = override or (load_config(root).get("site") or {}).get("repo")
    if configured:
        repo = _normalize_repo(configured)
        if not repo:
            raise HGraphError(f"invalid GitHub repository: {configured!r}; expected owner/name")
        return repo
    out = _run(["gh", "repo", "view", "--json", "nameWithOwner"], cwd=root)
    try:
        return json.loads(out)["nameWithOwner"]
    except (KeyError, TypeError, json.JSONDecodeError):
        raise HGraphError("could not determine the GitHub repository; pass --repo owner/name")


def _default_branch(root: Path, repo: str) -> str:
    out = _run(["gh", "repo", "view", repo, "--json", "defaultBranchRef"], cwd=root)
    try:
        return json.loads(out)["defaultBranchRef"]["name"]
    except (KeyError, TypeError, json.JSONDecodeError):
        raise HGraphError(f"could not determine {repo}'s default branch; pass --base BRANCH")


def _remote_blobs(root: Path, repo: str, branch: str) -> dict[str, str]:
    ref = urllib.parse.quote(branch, safe="")
    out = _run(["gh", "api", f"repos/{repo}/git/trees/{ref}?recursive=1"], cwd=root)
    try:
        data = json.loads(out)
    except json.JSONDecodeError as e:
        raise HGraphError(f"GitHub returned invalid tree data for {repo}@{branch}: {e}")
    if data.get("truncated"):
        raise HGraphError(
            f"GitHub truncated the recursive tree for {repo}@{branch}; refusing to guess which reviews are new")
    return {item["path"]: item["sha"] for item in data.get("tree", [])
            if item.get("type") == "blob" and item.get("path") and item.get("sha")}


def _attachment_files(g: Graph, kinds: set[str]) -> list[tuple[str, Path]]:
    found: list[tuple[str, Path]] = []
    for kind in sorted(kinds):
        for path in g.nodes_dir.glob(f"*/{kind}-*.md"):
            if path.is_file():
                found.append((kind, path))
    return sorted(found, key=lambda row: str(row[1]))


def collect_pending(root: str | Path, *, repo: str | None = None,
                    base: str | None = None,
                    kinds: set[str] | None = None) -> tuple[str, str, Path, list[PendingAttachment]]:
    """Return ``(repo, branch, git_root, pending attachments)`` for one project."""
    root = Path(root).resolve()
    kinds = kinds or {"review", "comment"}
    if not kinds <= {"review", "comment"}:
        raise HGraphError("attachment kinds must be review and/or comment")
    if not (root / "hgraph" / "nodes").is_dir():
        raise HGraphError(f"not an hgraph project: {root} (no hgraph/nodes/ directory)")
    if shutil.which("gh") is None:
        raise HGraphError("GitHub CLI (`gh`) is required; install it and run `gh auth login`")

    g = Graph.open(root)
    files = _attachment_files(g, kinds)
    git_root = _git_root(root)
    try:
        root.relative_to(git_root)
    except ValueError:
        raise HGraphError(f"project {root} is outside its Git repository {git_root}")

    repo_name = _repo_name(root, repo)
    branch = base or _default_branch(root, repo_name)
    remote = _remote_blobs(root, repo_name, branch)
    pending: list[PendingAttachment] = []
    for kind, path in files:
        try:
            repo_path = path.resolve().relative_to(git_root).as_posix()
        except ValueError:
            raise HGraphError(f"attachment is outside the Git repository: {path}")
        raw = path.read_bytes()
        local_sha = _git_blob_sha(raw)
        remote_sha = remote.get(repo_path)
        if remote_sha == local_sha:
            continue
        target_id = path.parent.name
        if not g.has_node(target_id):
            raise HGraphError(f"attachment target node is missing: {path}")
        meta, content = _read_doc(path)
        pending.append(PendingAttachment(
            kind=kind, path=path, repo_path=repo_path, target=g.get_node(target_id),
            meta=dict(meta), content=content, blob_sha=local_sha, remote_sha=remote_sha))
    return repo_name, branch, git_root, pending


def _inline(value) -> str:
    return str(value).replace("`", "'").replace("\n", " ").strip()


def _target_name(a: PendingAttachment) -> str:
    return a.target.meta.get("label") or a.target.title or a.target.id


def render_issue_body(project: str, repo: str, branch: str, commit: str,
                      attachments: list[PendingAttachment]) -> str:
    reviews = sum(a.kind == "review" for a in attachments)
    comments = sum(a.kind == "comment" for a in attachments)
    lines = [
        "## hgraph collaboration batch",
        "",
        "This issue collects locally authored hgraph feedback that differs from the repository baseline. "
        "Each attachment is posted as a separate issue comment so collaborators can discuss it independently.",
        "",
        f"- **Project:** {_inline(project)}",
        f"- **Compared with:** `{repo}@{branch}`",
        f"- **Local commit:** `{commit}`",
        f"- **Contents:** {reviews} review(s), {comments} comment(s)",
        "",
        "### Attachment manifest",
        "",
        "| Type | Target | Local path | State |",
        "| --- | --- | --- | --- |",
    ]
    for a in attachments:
        state = "new" if a.remote_sha is None else "modified"
        lines.append(
            f"| {a.kind} | `{_inline(_target_name(a))}` | `{_inline(a.repo_path)}` | {state} |")
    lines += [
        "",
        "After discussion, apply the accepted feedback by adding these attachment files to the repository.",
        "",
        f"<!-- hgraph-batch repo={repo} branch={branch} commit={commit} -->",
    ]
    return "\n".join(lines)


_COMMON_META = {"author", "created", "updated", "date", "title", "maths_verdict",
                "maths_comment", "lean_verdict", "lean_comment"}


def render_attachment_comment(a: PendingAttachment) -> str:
    target = a.target
    label = target.meta.get("label")
    heading = "Review" if a.kind == "review" else "Comment"
    title = a.meta.get("title") or target.title or label or target.id
    state = "New attachment" if a.remote_sha is None else "Modified attachment"
    lines = [
        f"## {heading}: {_inline(title)}",
        "",
        f"- **Target:** `{_inline(label or target.id)}`",
        f"- **Node ID:** `{target.id}`",
        f"- **Declaration type:** {_inline(target.meta.get('content_type') or target.type or 'unknown')}",
        f"- **Local path:** `{_inline(a.repo_path)}`",
        f"- **State:** {state}",
    ]
    for key, label_name in (("chapter", "Chapter"), ("ref", "Source reference"),
                            ("status", "Workflow status"), ("lean_status", "Lean status")):
        if target.meta.get(key) not in (None, ""):
            lines.append(f"- **{label_name}:** {_inline(target.meta[key])}")
    if a.meta.get("author"):
        lines.append(f"- **Author:** {_inline(a.meta['author'])}")
    when = a.meta.get("updated") or a.meta.get("created") or a.meta.get("date")
    if when:
        lines.append(f"- **Updated:** {_inline(when)}")

    if a.kind == "review":
        lines += ["", "### Mathematics", "",
                  f"**Verdict:** {_inline(a.meta.get('maths_verdict') or 'not reviewed')}"]
        if a.meta.get("maths_comment"):
            lines += ["", str(a.meta["maths_comment"]).strip()]
        lines += ["", "### Lean", "",
                  f"**Verdict:** {_inline(a.meta.get('lean_verdict') or 'not reviewed')}"]
        if a.meta.get("lean_comment"):
            lines += ["", str(a.meta["lean_comment"]).strip()]
        if a.content.strip():
            lines += ["", "### Additional note", "", a.content.strip()]
    else:
        lines += ["", "### Comment", "", a.content.strip() or "_No comment text._"]

    extra = {k: v for k, v in a.meta.items() if k not in _COMMON_META and v is not None}
    if extra:
        dumped = yaml.safe_dump(extra, sort_keys=True, allow_unicode=True).rstrip()
        lines += ["", "<details><summary>Additional metadata</summary>", "", "```yaml",
                  dumped, "```", "", "</details>"]
    lines += ["", f"<!-- hgraph-attachment path={a.repo_path} sha={a.blob_sha} -->"]
    return "\n".join(lines)


def send_review_batch(root: str | Path, *, repo: str | None = None,
                      base: str | None = None, kinds: set[str] | None = None,
                      title: str | None = None, labels: list[str] | None = None,
                      dry_run: bool = False) -> dict:
    """Create one issue and one issue comment per pending review/comment."""
    root = Path(root).resolve()
    repo_name, branch, git_root, pending = collect_pending(
        root, repo=repo, base=base, kinds=kinds)
    project = (load_config(root).get("site") or {}).get("title") or root.name
    commit = _run(["git", "rev-parse", "HEAD"], cwd=root)
    issue_title = title or f"hgraph feedback: {project} ({len(pending)} attachment(s))"
    issue_body = render_issue_body(project, repo_name, branch, commit, pending)
    comments = [render_attachment_comment(a) for a in pending]
    result = {"repo": repo_name, "branch": branch, "project": project,
              "pending": pending, "title": issue_title, "body": issue_body,
              "comments": comments, "issue": None}
    if dry_run or not pending:
        return result

    cmd = ["gh", "issue", "create", "--repo", repo_name,
           "--title", issue_title, "--body-file", "-"]
    for label in labels or []:
        cmd += ["--label", label]
    issue = _run(cmd, cwd=git_root, input_text=issue_body).splitlines()[-1]
    result["issue"] = issue
    posted = 0
    try:
        for comment in comments:
            _run(["gh", "issue", "comment", issue, "--repo", repo_name,
                  "--body-file", "-"], cwd=git_root, input_text=comment)
            posted += 1
    except HGraphError as e:
        raise HGraphError(
            f"created {issue}, but posted only {posted}/{len(comments)} attachment comments: {e}")
    return result
