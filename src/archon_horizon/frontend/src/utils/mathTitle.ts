import katex from "katex";
import { unified } from "unified";
import remarkParse from "remark-parse";
import remarkMath from "remark-math";
import type { MathMacros } from "./document";

const parser = unified().use(remarkParse).use(remarkMath);
type TitlePart = { value: string; math: boolean };

export function mathTitleParts(title: string): TitlePart[] {
  if (!title.includes("$")) return [{ value: title, math: false }];
  const parts: TitlePart[] = [];
  let offset = 0;
  const visit = (node: any) => {
    if (node.type === "inlineMath" || node.type === "math") {
      const start = node.position.start.offset, end = node.position.end.offset;
      if (start > offset) parts.push({ value: title.slice(offset, start), math: false });
      parts.push({ value: node.value, math: true });
      offset = end;
    } else node.children?.forEach(visit);
  };
  visit(parser.parse(title));
  if (offset < title.length) parts.push({ value: title.slice(offset), math: false });
  return parts;
}

const escapeHtml = (value: string) => value.replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]!));

export function mathTitleHtml(title: string, macros: MathMacros = {}): string {
  return mathTitleParts(title).map((part) => part.math
    ? katex.renderToString(part.value, { displayMode: false, throwOnError: false, trust: false, strict: "ignore", maxExpand: 1000, macros: { ...macros } })
    : escapeHtml(part.value)).join("");
}
