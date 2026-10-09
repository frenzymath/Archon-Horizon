import { parseDocument as parseYamlDocument, stringify } from "yaml";

export type DocumentMetadata = Record<string, unknown>;
export type ParsedDocument = { metadata: DocumentMetadata; body: string; hasFrontmatter: boolean; error?: string };
export type MathMacros = Record<string, string>;

const macroName = /^\\[A-Za-z]+$/;
const prohibitedMacroCommand = /\\(?:def|gdef|edef|xdef|newcommand|renewcommand|providecommand|require|include|input)\b/i;

function validatedMathMacros(value: unknown): MathMacros {
  // These input budgets keep untrusted document macros small. KaTeX also gets
  // maxExpand=1000 and trust=false at rendering sites; neither is a TeX engine.
  if (value == null) return {};
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("math_macros must be a mapping from TeX macro names to expansions.");
  const entries = Object.entries(value);
  if (entries.length > 64) throw new Error("math_macros may define at most 64 macros.");
  const macros: MathMacros = {};
  let totalLength = 0;
  for (const [name, expansion] of entries) {
    if (!macroName.test(name)) throw new Error(`Invalid math macro name: ${name}.`);
    if (typeof expansion !== "string" || !expansion.trim()) throw new Error(`Math macro ${name} must have a non-empty string expansion.`);
    if (expansion.length > 512 || prohibitedMacroCommand.test(expansion)) throw new Error(`Unsafe or oversized expansion for math macro ${name}.`);
    totalLength += name.length + expansion.length;
    if (totalLength > 8192) throw new Error("math_macros exceeds the 8 KiB document limit.");
    macros[name] = expansion;
  }
  return macros;
}

/** Returns a fresh macro mapping for one render; KaTeX must not share it across documents. */
export function mathMacros(metadata: DocumentMetadata): MathMacros {
  try { return validatedMathMacros(metadata.math_macros); }
  catch { return {}; }
}

export function splitDocument(raw: string): ParsedDocument {
  const first = raw.match(/^\uFEFF?---[ \t]*\r?\n/);
  if (!first) return { metadata: {}, body: raw, hasFrontmatter: false };
  const rest = raw.slice(first[0].length);
  const closing = /^(?:---|\.\.\.)[ \t]*(?:\r?\n|$)/m.exec(rest);
  if (!closing) return { metadata: {}, body: "", hasFrontmatter: true, error: "YAML frontmatter has no closing delimiter." };
  const body = rest.slice(closing.index + closing[0].length).replace(/^\r?\n/, "");
  try {
    const document = parseYamlDocument(rest.slice(0, closing.index), { uniqueKeys: true });
    if (document.errors.length) throw document.errors[0];
    // Bound YAML alias expansion rather than allowing recursive/expansive input
    // to consume the browser's memory. 50 follows the parser's small-input use.
    const metadata = document.toJS({ maxAliasCount: 50 }) ?? {};
    if (!metadata || typeof metadata !== "object" || Array.isArray(metadata)) throw new Error("YAML frontmatter must be a mapping of field names to values.");
    validatedMathMacros((metadata as DocumentMetadata).math_macros);
    return { metadata, body, hasFrontmatter: true };
  } catch (error) {
    return { metadata: {}, body, hasFrontmatter: true, error: error instanceof Error ? error.message : "Invalid YAML frontmatter." };
  }
}

export function composeDocument(metadata: DocumentMetadata, body: string): string {
  return `---\n${stringify(metadata, { lineWidth: 0 })}---\n\n${body}`;
}

export function documentBody(parsed: ParsedDocument): string {
  return parsed.body;
}
