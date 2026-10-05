import { Fragment, useMemo } from "react";
import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import { highlightLeanLines } from "../utils/leanHighlight";
import { blueprintCallouts, blueprintDisplayMath } from "../utils/blueprintSyntax";
import { type BibliographyEntry } from "../utils/bibliography";
import { type MathMacros } from "../utils/document";
import { sourceLinks, type SourceContext, type WorkspaceRepository } from "../utils/sourceLinks";
import { horizonReferences } from "../utils/horizonReferences";
import { NodeReference, ReferenceCitation, roadmapSyntax } from "./RoadmapMarkdown";
import MarkdownPre from "./MarkdownPre";
import "katex/dist/katex.min.css";
import "./blueprint.css";

type Item = Record<string, any>;

export default function BlueprintMarkdown({ content, nodes = [], projectId = "", onNode, className = "", bibliographyEntries = [], mathMacros = {}, nodeResolver, referenceLookupComplete, sourceContext, repository }: {
  content: string;
  nodes?: Item[];
  projectId?: string;
  onNode?: (node: Item) => void;
  className?: string;
  bibliographyEntries?: BibliographyEntry[];
  mathMacros?: MathMacros;
  nodeResolver?: (id: string) => Promise<Item | null>;
  referenceLookupComplete?: boolean;
  sourceContext?: SourceContext;
  repository?: WorkspaceRepository;
}) {
  const hasMath = useMemo(() => /\$|\\\[|\\\(/.test(content), [content]);
  return <div className={`platform-blueprint-markdown ${className}`}>
    <ReactMarkdown
      skipHtml
      remarkPlugins={hasMath
        ? [remarkGfm, remarkMath, blueprintCallouts, blueprintDisplayMath, [roadmapSyntax, { checklist: false }], [horizonReferences, { projectId, repository: repository || sourceContext?.repository }], [sourceLinks, sourceContext]]
        : [remarkGfm, blueprintCallouts, [roadmapSyntax, { checklist: false }], [horizonReferences, { projectId, repository: repository || sourceContext?.repository }], [sourceLinks, sourceContext]]}
      rehypePlugins={hasMath ? [[rehypeKatex, { trust: false, throwOnError: false, strict: "ignore", macros: { ...mathMacros }, maxExpand: 1000 }]] : []}
      urlTransform={(url) => /^(?:node:|ref:)/.test(url) ? url : defaultUrlTransform(url)}
      components={{
        pre: MarkdownPre,
        a: ({ href, children, title }) => href?.startsWith("ref:")
          ? <ReferenceCitation href={href} entries={bibliographyEntries} />
        : href?.startsWith("node:") && (onNode || nodeResolver)
          ? <NodeReference id={href.slice(5)} nodes={nodes} projectId={projectId} onNode={onNode} nodeResolver={nodeResolver} referenceLookupComplete={referenceLookupComplete}>{children}</NodeReference>
          : <a href={href?.startsWith("node:") ? `?project=${encodeURIComponent(projectId)}&node=${encodeURIComponent(href.slice(5))}` : href} title={title} rel="noreferrer">{children}</a>,
        blockquote: ({ node, children }) => {
          const kind = String(node?.properties?.["data-callout"] || "");
          return kind
            ? <blockquote className={`platform-math-callout callout-${kind}`} data-callout={kind}><strong className="platform-math-callout-label">{kind[0].toUpperCase() + kind.slice(1)}</strong>{children}</blockquote>
            : <blockquote>{children}</blockquote>;
        },
        code: ({ className: codeClass, children }) => {
          if (codeClass !== "language-lean" && codeClass !== "language-lean4") return <code className={codeClass}>{children}</code>;
          const lines = highlightLeanLines(String(children).replace(/\n$/, "").split("\n"));
          return <code className={codeClass}>{lines.map((tokens, row) => <Fragment key={row}>{row > 0 ? "\n" : null}{tokens.map(({ text, cls }, column) => cls ? <span key={column} className={`hl-${cls}`}>{text}</span> : text)}</Fragment>)}</code>;
        },
      }}
    >{content}</ReactMarkdown>
  </div>;
}
