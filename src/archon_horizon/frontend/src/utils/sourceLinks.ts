export type WorkspaceRepository = { url: string };
export type SourceContext = { repository: WorkspaceRepository; revision: string };

const commitId = /^[0-9a-f]{7,64}$/i;

export function repositoryUrl(repository: WorkspaceRepository | undefined): string | undefined {
  if (!repository?.url) return undefined;
  try {
    const url = new URL(repository.url);
    if (!["https:", "http:"].includes(url.protocol) || url.username || url.password) return undefined;
    url.search = "";
    url.hash = "";
    return url.href.replace(/\/$/, "");
  } catch { return undefined; }
}

export function sourceContext(repository: WorkspaceRepository | undefined, revision: unknown): SourceContext | undefined {
  return repositoryUrl(repository) && repository && typeof revision === "string" && commitId.test(revision)
    ? { repository, revision } : undefined;
}

export function sourceHref(reference: string, context?: SourceContext): string | undefined {
  if (!context) return undefined;
  const { repository, revision } = context;
  const base = repositoryUrl(repository);
  if (!base) return undefined;
  if (reference === revision) return `${base}/commit/${revision}`;
  const [rawPath, fragment] = reference.split("#", 2);
  const path = rawPath.startsWith("./") ? rawPath.slice(2) : rawPath;
  if (!path || path.startsWith("/") || /[:\\\x00-\x1f]/.test(path) || path.split("/").some(part => !part || part === ".." || part === ".")) return undefined;
  if (!/\.(?:lean|md|tex|bib|pdf|json|ya?ml|toml|txt)$/i.test(path)) return undefined;
  const anchor = fragment && /^L\d+(?:-L\d+)?$/.test(fragment) ? `#${fragment}` : "";
  return `${base}/src/commit/${revision}/${path.split("/").map(encodeURIComponent).join("/")}${anchor}`;
}

type MarkdownNode = { type: string; value?: string; url?: string; children?: MarkdownNode[] };

export function sourceLinks(context?: SourceContext) {
  return (tree: MarkdownNode) => {
    if (!context) return;
    const visit = (node: MarkdownNode) => {
      if (node.type === "link") {
        const href = sourceHref(node.url || "", context);
        if (href) node.url = href;
        return;
      }
      if (!node.children || ["code", "html", "linkReference"].includes(node.type)) return;
      node.children = node.children.flatMap(child => {
        if (child.type === "inlineCode") {
          const href = sourceHref(child.value || "", context);
          return href ? [{ type: "link", url: href, children: [child] }] : [child];
        }
        visit(child);
        return [child];
      });
    };
    visit(tree);
  };
}
