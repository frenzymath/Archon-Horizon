import { repositoryUrl, type WorkspaceRepository } from "./sourceLinks";

type MarkdownNode = { type: string; value?: string; url?: string; title?: string; children?: MarkdownNode[] };
export type ReferenceContext = { projectId?: string; repository?: WorkspaceRepository };

export function workspaceRepository(workspace: unknown): WorkspaceRepository | undefined {
  if (!workspace || typeof workspace !== "object" || !("url" in workspace) || typeof workspace.url !== "string") return undefined;
  const repository = {url: workspace.url};
  return repositoryUrl(repository) ? repository : undefined;
}

// Only prose references are shortened. Existing links, code blocks and mathematics stay intact.
export function horizonReferences(context: ReferenceContext = {}) {
  const reference = (id: string): MarkdownNode | undefined => {
    const base = repositoryUrl(context.repository);
    if (!base) return;
    const url = `${base}/commit/${id}`, label = id.slice(0, 8);
    return { type: "link", url, title: id, children: [{ type: "text", value: label }] };
  };
  return (tree: MarkdownNode) => {
    const visit = (node: MarkdownNode) => {
      if (["link", "linkReference", "code", "html", "inlineMath", "math", "image", "imageReference", "definition"].includes(node.type) || !node.children) return;
      node.children = node.children.flatMap(child => {
        if (child.type === "inlineCode") {
          return /^[a-f0-9]{40}$/.test(child.value || "")
            ? [reference(child.value!) || child] : [child];
        }
        if (child.type !== "text") { visit(child); return [child]; }
        const text = child.value || "", parts: MarkdownNode[] = [];
        let offset = 0;
        for (const match of text.matchAll(/(?<![\w/.-])[a-f0-9]{40}(?![\w/-]|\.[\w])/g)) {
          const link = reference(match[0]);
          if (!link) continue;
          if (match.index! > offset) parts.push({ type: "text", value: text.slice(offset, match.index) });
          parts.push(link); offset = match.index! + match[0].length;
        }
        if (!offset) return [child];
        if (offset < text.length) parts.push({ type: "text", value: text.slice(offset) });
        return parts;
      });
    };
    visit(tree);
  };
}
