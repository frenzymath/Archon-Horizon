import { lazy, Suspense, type ReactNode } from "react";
import type { Element } from "hast";

const Bibliography = lazy(() => import("./Bibliography"));

export default function MarkdownPre({ node, children, ...props }: { node?: Element; children?: ReactNode; className?: string }) {
  const code = node?.children.find((child): child is Element => child.type === "element" && child.tagName === "code");
  const classes = code?.properties.className;
  const language = Array.isArray(classes) ? classes : typeof classes === "string" ? classes.split(" ") : [];
  if (code && language.some((value) => String(value).toLowerCase() === "language-bibtex")) {
    const source = code.children.map((child) => child.type === "text" ? child.value : "").join("");
    return <Suspense fallback={<span className="platform-document-loading">Loading references...</span>}><Bibliography bibtex={source} /></Suspense>;
  }
  return <pre {...props}>{children}</pre>;
}
