import { unified } from "unified";
import remarkParse from "remark-parse";

type MarkdownNode = {
  type: string;
  value?: string;
  url?: string;
  data?: Record<string, any>;
  position?: { start: { offset?: number }; end: { offset?: number } };
  children?: MarkdownNode[];
};

const nodeId = /^[A-Za-z0-9][A-Za-z0-9_.:-]*$/;
const calloutTypes = new Set([
  "definition", "theorem", "lemma", "proposition", "corollary", "conjecture",
  "remark", "example", "note", "warning", "proof", "sketch", "intuition", "formalization",
]);
const processor = unified().use(remarkParse);

export function referencedNodeIds(markdown: string): string[] {
  const ids = new Set<string>();
  const visit = (node: MarkdownNode) => {
    if (["code", "inlineCode", "html", "image", "imageReference", "definition"].includes(node.type)) return;
    if (node.type === "link") {
      if (node.url?.startsWith("node:") && nodeId.test(node.url.slice(5))) ids.add(node.url.slice(5));
      return;
    }
    if (node.type === "text") {
      for (const match of (node.value || "").matchAll(/\[\[node:([A-Za-z0-9][A-Za-z0-9_.:-]*)\]\]/g)) ids.add(match[1]);
    }
    node.children?.forEach(visit);
  };
  visit(processor.parse(markdown) as MarkdownNode);
  return [...ids];
}

export const extractNodeReferences = referencedNodeIds;

export function blueprintDisplayMath() {
  return (tree: MarkdownNode, file: { value: unknown }) => {
    const source = String(file.value);
    const visit = (node: MarkdownNode) => {
      if (node.type === "inlineMath" && node.position) {
        const raw = source.slice(node.position.start.offset, node.position.end.offset);
        if (raw.startsWith("$$") && raw.endsWith("$$")) {
          node.data = { ...node.data, hProperties: { ...node.data?.hProperties, className: ["language-math", "math-display"] } };
        }
      }
      node.children?.forEach(visit);
    };
    visit(tree);
  };
}

export function blueprintCallouts() {
  return (tree: MarkdownNode) => {
    const visit = (node: MarkdownNode) => {
      if (node.type === "blockquote") {
        const first = node.children?.[0]?.children?.[0];
        const marker = first?.type === "text" && first.value?.match(/^\[!([a-z]+)\](?:[ \t]*\n|[ \t]+|$)/i);
        const kind = marker && marker[1].toLowerCase();
        if (marker && kind && calloutTypes.has(kind) && first) {
          first.value = first.value!.slice(marker[0].length);
          node.data = { ...node.data, hProperties: { ...node.data?.hProperties, "data-callout": kind } };
          if (!first.value && node.children?.[0].children?.length === 1) node.children.shift();
        }
      }
      node.children?.forEach(visit);
    };
    visit(tree);
  };
}
