/**
 * BlueprintDoc — a leanblueprint-quality reading renderer for a whole blueprint.
 *
 * Unlike the per-block `BlueprintRendered` (used by the DAG sidebar), this is a
 * document-level renderer: it runs a numbering pass over every chapter to assign
 * Chapter / Section / Theorem numbers and to build a label → number map, so that
 * `\ref` / `\cref` resolve to clickable, numbered cross-references (comma-
 * separated multi-label args included). It also:
 *   • renders `\cite` / `\citet` / `\citep` / `\citeauthor` / `\citeyear` against
 *     the project's `.bib` (and a bibliography block at the end of the doc);
 *   • renders amsthm-style environments with the optional `[name]` italic in the
 *     header, `\lean` as expandable code chips, `\uses` as dependency tag links;
 *   • renders `\section` / `\subsection` headings and itemize/enumerate lists;
 *   • normalises text LaTeX: `~`→ space, `---`→ —, `--`→ –, ``…''→ “…”, etc.;
 *   • hides `\label{}` (anchors), KaTeX-renders math (incl. titles & align envs);
 *   • surfaces `% SOURCE` / `% NOTE` comments as expandable chips.
 */
import { memo, useMemo, useState } from 'react';
import katex from 'katex';
import { useProgressiveCount } from '../hooks/useProgressiveCount';
import 'katex/dist/katex.min.css';
import styles from './BlueprintDoc.module.css';

export interface DocChapter { slug: string; title: string; tex: string; }

const ENV_LABELS: Record<string, string> = {
  theorem: 'Theorem', lemma: 'Lemma', proposition: 'Proposition',
  corollary: 'Corollary', definition: 'Definition', proposition_: 'Proposition',
  remark: 'Remark', example: 'Example', conjecture: 'Conjecture',
  notation: 'Notation', convention: 'Convention', exercise: 'Exercise',
  claim: 'Claim', proof: 'Proof',
};
// Environments that get a statement number.
const NUMBERED = new Set([
  'theorem', 'lemma', 'proposition', 'corollary', 'definition', 'remark',
  'example', 'conjecture', 'notation', 'convention', 'exercise', 'claim',
]);
const LIST_ENVS = new Set(['itemize', 'enumerate', 'description']);
const MATH_ENVS = new Set([
  'align', 'align*', 'aligned', 'equation', 'equation*', 'gather', 'gather*',
  'array', 'matrix', 'pmatrix', 'bmatrix', 'vmatrix', 'cases', 'split', 'multline', 'multline*',
]);

// ── bibliography ─────────────────────────────────────────────────────────────
/** One parsed BibTeX entry, as shipped by `/api/blueprint/chapters`. */
export interface BibEntry {
  key: string;
  type?: string;
  title?: string | null;
  author?: string | null;
  year?: string | null;
  journal?: string | null;
  booktitle?: string | null;
  publisher?: string | null;
  volume?: string | null;
  number?: string | null;
  pages?: string | null;
  url?: string | null;
}

export type BibMap = Map<string, BibEntry>;

export function bibMapFrom(entries: BibEntry[] | null | undefined): BibMap {
  const map: BibMap = new Map();
  for (const e of entries ?? []) {
    if (e?.key) map.set(e.key, e);
  }
  return map;
}

/** Split a LaTeX comma-list (`a, b,c`) into trimmed non-empty keys. */
function splitTexList(raw: string): string[] {
  return raw.split(',').map((s) => s.trim()).filter(Boolean);
}

/** "Last, First and Foo, Bar" → short author chip ("Last et al." when ≥3). */
function shortAuthors(author: string | null | undefined): string {
  if (!author) return '';
  const parts = author.split(/\s+and\s+/i).map((p) => p.trim()).filter(Boolean);
  const lastOf = (p: string) => {
    const comma = p.indexOf(',');
    if (comma !== -1) return p.slice(0, comma).trim();
    const bits = p.split(/\s+/);
    return bits[bits.length - 1] || p;
  };
  if (parts.length === 0) return '';
  if (parts.length === 1) return lastOf(parts[0]);
  if (parts.length === 2) return `${lastOf(parts[0])} and ${lastOf(parts[1])}`;
  return `${lastOf(parts[0])} et al.`;
}

function citeLabel(entry: BibEntry | undefined, key: string, style: 'cite' | 'citet' | 'citep' | 'citeauthor' | 'citeyear'): string {
  if (!entry) return key;
  const authors = shortAuthors(entry.author);
  const year = entry.year?.trim() || '';
  switch (style) {
    case 'citet':
      return authors && year ? `${authors} (${year})` : authors || year || key;
    case 'citep':
      return authors && year ? `${authors}, ${year}` : authors || year || key;
    case 'citeauthor':
      return authors || key;
    case 'citeyear':
      return year || key;
    case 'cite':
    default:
      // Numeric-looking keys stay bare; otherwise author-year in brackets.
      if (/^\d+$/.test(key) && !authors) return key;
      if (authors && year) return `${authors}, ${year}`;
      return authors || year || key;
  }
}

// ── inline / block node types ────────────────────────────────────────────────
type Inline =
  | { t: 'text'; v: string }
  | { t: 'math'; v: string; display: boolean }
  | { t: 'strong'; c: Inline[] }
  | { t: 'em'; c: Inline[] }
  | { t: 'code'; v: string }
  | { t: 'ref'; labels: string[]; cref: boolean }
  | { t: 'cite'; keys: string[]; style: 'cite' | 'citet' | 'citep' | 'citeauthor' | 'citeyear'; prenote?: string; postnote?: string }
  | { t: 'comment'; v: string };

interface EnvMeta {
  label?: string; human?: string; lean: string[]; uses: string[];
  leanok: boolean; mathlibok: boolean; notready: boolean;
}
type Block =
  | { t: 'section'; level: 2 | 3; title: Inline[]; label?: string; num?: string; anchor?: string }
  | { t: 'paragraph'; title: Inline[] }
  | { t: 'env'; name: string; meta: EnvMeta; body: Block[]; num?: string; anchor?: string }
  | { t: 'list'; ordered: boolean; items: Block[][] }
  | { t: 'center'; body: Block[] }
  | { t: 'pre'; v: string }
  | { t: 'table'; rows: Inline[][][]; columnSpec: string }
  | { t: 'displaymath'; v: string }
  | { t: 'para'; c: Inline[] };

interface RefTarget { kind: string; num: string; anchor: string; slug: string; }
type LabelMap = Map<string, RefTarget>;

// ── text normalisation (applied only to text nodes, never to math) ───────────
// TeX accent commands → combining characters: \'e é, \`e è, \^e ê, \"o ö,
// \~n ñ, \c{c} ç, \v{C} Č ("\v{C}ech"). Braced and bare one-letter args.
const ACCENT_MAP: Record<string, string> = {
  "'": '\u0301', '`': '\u0300', '^': '\u0302', '"': '\u0308', '~': '\u0303',
  '=': '\u0304', '.': '\u0307', 'v': '\u030c', 'u': '\u0306', 'c': '\u0327',
  'H': '\u030b', 'r': '\u030a',
};
function applyAccents(s: string): string {
  // Braced arg may follow whitespace (\v {C}); a bare arg must be immediate
  // (\'e) so prose like "etc\. Next" never accents the following word.
  return s.replace(
    /\\(['`^"~=.]|[vucHr](?![A-Za-z]))(?:\s*\{([A-Za-z])\}|([A-Za-z]))/g,
    (_, acc: string, braced?: string, bare?: string) =>
      ((braced ?? bare ?? '') + ACCENT_MAP[acc]).normalize('NFC'),
  );
}

function normText(s: string): string {
  return applyAccents(s)
    .replace(/---/g, '—')      // em dash
    .replace(/--/g, '–')       // en dash
    .replace(/``/g, '“').replace(/''/g, '”')  // “ ”
    .replace(/~/g, ' ')        // non-breaking space
    .replace(/\\(ldots|dots)\b/g, '…')
    .replace(/\\S(?![A-Za-z])/g, '§')
    .replace(/\\P(?![A-Za-z])/g, '¶')
    .replace(/\\([%&#_$])/g, '$1')  // escaped specials
    .replace(/\\\\\s*/g, ' ')        // \\ line break
    .replace(/\s+/g, ' ');
}
function normCodeText(s: string): string {
  return s
    .replace(/\\textbackslash\b/g, '\\')
    .replace(/\\([%&#_$])/g, '$1')
    .replace(/\\_/g, '_')
    .replace(/\\\{/g, '{')
    .replace(/\\\}/g, '}');
}

// ── comment preservation: fold `%` runs into inline tokens ───────────────────
function b64e(s: string): string {
  const bytes = new TextEncoder().encode(s);
  let bin = '';
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin);
}
function b64d(s: string): string {
  try { return new TextDecoder().decode(Uint8Array.from(atob(s), c => c.charCodeAt(0))); }
  catch { return ''; }
}
function stripTrailing(line: string): string {
  let out = '';
  for (let i = 0; i < line.length; i++) {
    if (line[i] === '%' && (i === 0 || line[i - 1] !== '\\')) break;
    out += line[i];
  }
  return out;
}
function encodeComments(src: string, show: boolean): string {
  const out: string[] = [];
  let run: string[] = [];
  const flush = () => {
    if (!run.length) return;
    const joined = run.map(l => l.replace(/^\s*%\s?/, ''))
      .filter(l => !/^archon:/i.test(l.trim())).join('\n');
    run = [];
    if (show && joined.trim()) out.push(`\\archoncomment{${b64e(joined)}}`);
  };
  for (const line of src.split('\n')) {
    if (/^\s*%/.test(line)) run.push(line);
    else { flush(); out.push(stripTrailing(line)); }
  }
  flush();
  return out.join('\n');
}

// ── brace / bracket / env helpers ────────────────────────────────────────────
function matchBrace(src: string, open: number): number {
  if (src[open] !== '{') return -1;
  let d = 1;
  for (let i = open + 1; i < src.length; i++) {
    if (src[i] === '\\') { i++; continue; }
    if (src[i] === '{') d++;
    else if (src[i] === '}') { d--; if (d === 0) return i; }
  }
  return -1;
}
function matchBracket(src: string, open: number): number {
  if (src[open] !== '[') return -1;
  let d = 1;
  for (let i = open + 1; i < src.length; i++) {
    if (src[i] === '\\') { i++; continue; }
    if (src[i] === '[') d++;
    else if (src[i] === ']') { d--; if (d === 0) return i; }
  }
  return -1;
}
function findEnvEnd(src: string, from: number, name: string): number {
  const begin = `\\begin{${name}}`, end = `\\end{${name}}`;
  let depth = 1, i = from;
  while (i < src.length) {
    const nb = src.indexOf(begin, i), ne = src.indexOf(end, i);
    if (ne === -1) return -1;
    if (nb !== -1 && nb < ne) { depth++; i = nb + begin.length; }
    else { depth--; if (depth === 0) return ne; i = ne + end.length; }
  }
  return -1;
}

const ACCENTS: Record<string, Record<string, string>> = {
  "'": { 'a': 'á', 'e': 'é', 'i': 'í', 'o': 'ó', 'u': 'ú', 'y': 'ý', 'A': 'Á', 'E': 'É', 'I': 'Í', 'O': 'Ó', 'U': 'Ú', 'Y': 'Ý', 'c': 'ć', 'C': 'Ć' },
  "`": { 'a': 'à', 'e': 'è', 'i': 'ì', 'o': 'ò', 'u': 'ù', 'A': 'À', 'E': 'È', 'I': 'Ì', 'O': 'Ò', 'U': 'Ù' },
  "^": { 'a': 'â', 'e': 'ê', 'i': 'î', 'o': 'ô', 'u': 'û', 'A': 'Â', 'E': 'Ê', 'I': 'Î', 'O': 'Ô', 'U': 'Û', 'c': 'ĉ', 'C': 'Ĉ', 'g': 'ĝ', 'G': 'Ĝ', 'h': 'ĥ', 'H': 'Ĥ', 'j': 'ĵ', 'J': 'Ĵ', 's': 'ŝ', 'S': 'Ŝ', 'w': 'ŵ', 'W': 'Ŵ', 'y': 'ŷ', 'Y': 'Ŷ' },
  "\"": { 'a': 'ä', 'e': 'ë', 'i': 'ï', 'o': 'ö', 'u': 'ü', 'y': 'ÿ', 'A': 'Ä', 'E': 'Ë', 'I': 'Ï', 'O': 'Ö', 'U': 'Ü', 'Y': 'Ÿ' },
  "~": { 'a': 'ã', 'n': 'ñ', 'o': 'õ', 'A': 'Ã', 'N': 'Ñ', 'O': 'Õ' },
  "c": { 'c': 'ç', 'C': 'Ç', 's': 'ş', 'S': 'Ş' },
  "v": { 'c': 'č', 's': 'š', 'z': 'ž', 'C': 'Č', 'S': 'Š', 'Z': 'Ž', 'r': 'ř', 'R': 'Ř', 'n': 'ň', 'N': 'Ň' },
  "u": { 'a': 'ă', 'e': 'ĕ', 'i': 'ĭ', 'o': 'ŏ', 'u': 'ŭ', 'A': 'Ă', 'E': 'Ĕ', 'I': 'Ĭ', 'O': 'Ŏ', 'U': 'Ŭ', 'g': 'ğ', 'G': 'Ğ' },
  "H": { 'o': 'ő', 'u': 'ű', 'O': 'Ő', 'U': 'Ű' },
};

// ── inline parser ────────────────────────────────────────────────────────────
function parseInline(src: string): Inline[] {
  const out: Inline[] = [];
  let i = 0;
  const push = (s: string) => {
    if (!s) return;
    const last = out[out.length - 1];
    if (last && last.t === 'text') last.v += s; else out.push({ t: 'text', v: s });
  };
  while (i < src.length) {
    if (src.startsWith('$$', i)) { const e = src.indexOf('$$', i + 2); if (e !== -1) { out.push({ t: 'math', v: src.slice(i + 2, e), display: true }); i = e + 2; continue; } }
    if (src.startsWith('\\[', i)) { const e = src.indexOf('\\]', i + 2); if (e !== -1) { out.push({ t: 'math', v: src.slice(i + 2, e), display: true }); i = e + 2; continue; } }
    if (src[i] === '$') { const e = src.indexOf('$', i + 1); if (e !== -1) { out.push({ t: 'math', v: src.slice(i + 1, e), display: false }); i = e + 1; continue; } }
    if (src.startsWith('\\(', i)) { const e = src.indexOf('\\)', i + 2); if (e !== -1) { out.push({ t: 'math', v: src.slice(i + 2, e), display: false }); i = e + 2; continue; } }

    // archon comment token
    if (src.startsWith('\\archoncomment{', i)) {
      const bs = i + '\\archoncomment'.length, c = matchBrace(src, bs);
      if (c !== -1) { out.push({ t: 'comment', v: b64d(src.slice(bs + 1, c)) }); i = c + 1; continue; }
    }
    // \label{} — anchor only, drop here
    const lab = /^\\label\s*\{[^{}]*\}/.exec(src.slice(i));
    if (lab) { i += lab[0].length; continue; }
    // blueprint metadata commands — surfaced elsewhere as badges/uses chips, so
    // strip them here rather than leaking their text (e.g. a stray "ok" from
    // \leanok, or the raw label list from \uses{}).
    const metaArg = /^\\(lean|uses|discussion|dcref|source|group|level|proves)\s*\{[^{}]*\}/.exec(src.slice(i));
    if (metaArg) { i += metaArg[0].length; continue; }
    const metaBare = /^\\(leanok|mathlibok|notready)\b/.exec(src.slice(i));
    if (metaBare) { i += metaBare[0].length; continue; }
    // references — multi-label `\cref{a, b}` is common in blueprints
    const ref = /^\\(ref|cref|Cref|eqref)\s*\{([^{}]*)\}/.exec(src.slice(i));
    if (ref) {
      const labels = splitTexList(ref[2]);
      if (labels.length) out.push({ t: 'ref', labels, cref: /cref/i.test(ref[1]) });
      i += ref[0].length;
      continue;
    }
    // citations: \cite[post]{k}, \cite[pre][post]{k}, \citet/\citep/\citeauthor/\citeyear
    // Also absorb a leading optional * (\cite*) and drop unknown cite-* variants softly.
    const cite = /^\\(cite|citet|citep|citeauthor|citeyear)\*?\s*/.exec(src.slice(i));
    if (cite) {
      let j = i + cite[0].length;
      let prenote = '';
      let postnote = '';
      // up to two optional [...] notes before the mandatory {keys}
      const opt1 = /^\[([^\]]*)\]\s*/.exec(src.slice(j));
      if (opt1) {
        j += opt1[0].length;
        const opt2 = /^\[([^\]]*)\]\s*/.exec(src.slice(j));
        if (opt2) {
          prenote = opt1[1].trim();
          postnote = opt2[1].trim();
          j += opt2[0].length;
        } else {
          postnote = opt1[1].trim();
        }
      }
      const keysM = /^\{([^{}]*)\}/.exec(src.slice(j));
      if (keysM) {
        const keys = splitTexList(keysM[1]);
        if (keys.length) {
          out.push({
            t: 'cite',
            keys,
            style: cite[1] as 'cite' | 'citet' | 'citep' | 'citeauthor' | 'citeyear',
            prenote: prenote || undefined,
            postnote: postnote || undefined,
          });
        }
        i = j + keysM[0].length;
        continue;
      }
      // Malformed \cite with no {…}: skip the command name only.
      i = j;
      continue;
    }
    // text markup
    const mk = /^\\(textbf|emph|textit|texttt|text)\s*\{/.exec(src.slice(i));
    if (mk) {
      const bs = i + mk[0].length - 1, c = matchBrace(src, bs);
      if (c !== -1) {
        const inner = src.slice(bs + 1, c);
        if (mk[1] === 'textbf') out.push({ t: 'strong', c: parseInline(inner) });
        else if (mk[1] === 'texttt') out.push({ t: 'code', v: normCodeText(inner) });
        else if (mk[1] === 'text') out.push(...parseInline(inner));
        else out.push({ t: 'em', c: parseInline(inner) });
        i = c + 1; continue;
      }
    }
    // spacing / escaped chars
    const sp = /^\\[,;:!> ]/.exec(src.slice(i));
    if (sp) { push(' '); i += sp[0].length; continue; }
    const esc = /^\\([%&_#${}])/.exec(src.slice(i));
    if (esc) { push(esc[1]); i += 2; continue; }
    // unknown \cmd{...} → keep inner content
    if (src[i] === '\\') {
      const tops = /^\\texorpdfstring\s*\{/.exec(src.slice(i));
      if (tops) {
        const bs = i + tops[0].length - 1, c = matchBrace(src, bs);
        if (c !== -1) {
          out.push(...parseInline(src.slice(bs + 1, c)));
          i = c + 1;
          const m2 = /^\s*\{/.exec(src.slice(i));
          if (m2) {
            const c2 = matchBrace(src, i + m2[0].length - 1);
            if (c2 !== -1) i = c2 + 1;
          }
          continue;
        }
      }
      // Accents. Symbol accents (' ` ^ " ~) never start a command name, so the
      // tight form \'e is unambiguous. Letter accents (\c \v \u \H) DO collide
      // with command names (\cite, \vspace, \underline, \Huge), so only treat
      // them as accents when the letter is braced (\c{c}) or space-separated
      // (\c c) — never \cc, which must stay a command.
      const accSym = /^\\(['`^"~])\s*(?:\{([A-Za-z])\}|([A-Za-z]))/.exec(src.slice(i));
      if (accSym) {
        const char = accSym[2] || accSym[3];
        push(ACCENTS[accSym[1]]?.[char] || char);
        i += accSym[0].length;
        continue;
      }
      const accLet = /^\\([cvuH])(?:\s*\{([A-Za-z])\}|\s+([A-Za-z]))/.exec(src.slice(i));
      if (accLet) {
        const char = accLet[2] || accLet[3];
        push(ACCENTS[accLet[1]]?.[char] || char);
        i += accLet[0].length;
        continue;
      }
      const m = /^\\([A-Za-z]+)\*?\s*/.exec(src.slice(i));
      if (m) {
        i += m[0].length;
        if (src[i] === '{') { const c = matchBrace(src, i); if (c !== -1) { out.push(...parseInline(src.slice(i + 1, c))); i = c + 1; continue; } }
        continue;
      }
      if (i + 1 < src.length) { push(src[i + 1]); i += 2; continue; }
    }
    push(src[i]); i++;
  }
  return out;
}

// ── block parser ─────────────────────────────────────────────────────────────
function splitParas(text: string): string[] {
  return text.split(/\n\s*\n/).map(s => s.trim()).filter(Boolean);
}

function isEmptyInlines(nodes: Inline[]): boolean {
  return nodes.every((n) => n.t === 'text' && !n.v.trim());
}

function parseBlocks(src: string): Block[] {
  const out: Block[] = [];
  let buf = '';
  const flushBuf = () => {
    for (const p of splitParas(buf)) {
      const c = parseInline(p);
      // Drop paragraphs that only held stripped commands (\label, \leanok, …).
      if (!isEmptyInlines(c)) out.push({ t: 'para', c });
    }
    buf = '';
  };
  let i = 0;
  while (i < src.length) {
    // Math regions first, so a `\begin{array}` / `\begin{aligned}` inside math
    // is never mistaken for a block environment.
    if (src.startsWith('\\[', i)) { const e = src.indexOf('\\]', i + 2); if (e !== -1) { flushBuf(); out.push({ t: 'displaymath', v: src.slice(i + 2, e) }); i = e + 2; continue; } }
    if (src.startsWith('$$', i)) { const e = src.indexOf('$$', i + 2); if (e !== -1) { flushBuf(); out.push({ t: 'displaymath', v: src.slice(i + 2, e) }); i = e + 2; continue; } }
    if (src.startsWith('\\(', i)) { const e = src.indexOf('\\)', i + 2); if (e !== -1) { buf += src.slice(i, e + 2); i = e + 2; continue; } }
    if (src[i] === '$') { const e = src.indexOf('$', i + 1); if (e !== -1) { buf += src.slice(i, e + 1); i = e + 1; continue; } }
    // section / subsection
    const sec = /^\\(sub)?section\*?\s*\{/.exec(src.slice(i));
    if (sec) {
      flushBuf();
      const bs = i + sec[0].length - 1, c = matchBrace(src, bs);
      if (c !== -1) {
        let j = c + 1;
        // capture a following \label{} as the section anchor
        const after = src.slice(j);
        const labM = /^\s*\\label\s*\{([^{}]*)\}/.exec(after);
        let label: string | undefined;
        if (labM) { label = labM[1].trim(); j += labM[0].length; }
        out.push({ t: 'section', level: sec[1] ? 3 : 2, title: parseInline(src.slice(bs + 1, c)), label });
        i = j; continue;
      }
    }
    const paraHead = /^\\(sub)?paragraph\*?\s*\{/.exec(src.slice(i));
    if (paraHead) {
      flushBuf();
      const bs = i + paraHead[0].length - 1, c = matchBrace(src, bs);
      if (c !== -1) {
        out.push({ t: 'paragraph', title: parseInline(src.slice(bs + 1, c)) });
        i = c + 1; continue;
      }
    }
    if (src.startsWith('\\begin{', i)) {
      const tagEnd = src.indexOf('}', i + 7);
      if (tagEnd !== -1) {
        const name = src.slice(i + 7, tagEnd);
        let bodyStart = tagEnd + 1;
        let columnSpec = '';
        if (name === 'tabular' || name === 'tabular*') {
          const parsed = parseTabularArgs(src, bodyStart, name === 'tabular*');
          bodyStart = parsed.bodyStart;
          columnSpec = parsed.columnSpec;
        }
        const endIdx = findEnvEnd(src, bodyStart, name);
        if (endIdx !== -1) {
          flushBuf();
          const rawBody = src.slice(bodyStart, endIdx);
          i = endIdx + `\\end{${name}}`.length;
          if (LIST_ENVS.has(name)) { out.push(parseList(rawBody, name === 'enumerate')); continue; }
          if (name === 'verbatim') { out.push({ t: 'pre', v: rawBody.replace(/^\n|\n$/g, '') }); continue; }
          if (name === 'center') { out.push({ t: 'center', body: parseBlocks(rawBody) }); continue; }
          if (name === 'quote' || name === 'quotation') { out.push({ t: 'env', name: 'quote', meta: { lean: [], uses: [], leanok: false, mathlibok: false, notready: false }, body: parseBlocks(rawBody) }); continue; }
          if (name === 'tabular' || name === 'tabular*') { out.push(parseTable(rawBody, columnSpec)); continue; }
          if (MATH_ENVS.has(name)) {
            // align/equation/gather → KaTeX `aligned`; matrix-family keep their
            // own environment (so e.g. array's `{ccc}` column spec survives).
            const bare = name.replace(/\*$/, '');
            const v = (bare === 'align' || bare === 'equation' || bare === 'gather' || bare === 'multline' || bare === 'split')
              ? `\\begin{aligned}${rawBody}\\end{aligned}`
              : `\\begin{${name}}${rawBody}\\end{${name}}`;
            out.push({ t: 'displaymath', v });
            continue;
          }
          // amsthm-like env (or unknown — render its body)
          out.push(parseEnv(name, rawBody));
          continue;
        }
      }
    }
    buf += src[i]; i++;
  }
  flushBuf();
  return out;
}

function parseList(body: string, ordered: boolean): Block {
  // Split on top-level \item (ignore \item inside nested envs by tracking begin/end depth).
  const parts: string[] = [];
  let depth = 0, cur = '';
  let k = 0;
  while (k < body.length) {
    if (body.startsWith('\\begin{', k)) { depth++; cur += '\\begin{'; k += 7; continue; }
    if (body.startsWith('\\end{', k)) { depth = Math.max(0, depth - 1); cur += '\\end{'; k += 5; continue; }
    if (depth === 0 && body.startsWith('\\item', k)) {
      if (cur.trim()) parts.push(cur);
      cur = '';
      k += 5;
      // optional \item[...] label
      if (body[k] === '[') { const c = matchBracket(body, k); if (c !== -1) k = c + 1; }
      continue;
    }
    cur += body[k]; k++;
  }
  if (cur.trim()) parts.push(cur);
  return { t: 'list', ordered, items: parts.map(p => parseBlocks(p)) };
}

function parseTabularArgs(src: string, start: number, hasWidth: boolean): { bodyStart: number; columnSpec: string } {
  let i = start;
  const skip = () => { while (i < src.length && /\s/.test(src[i])) i++; };
  skip();
  if (hasWidth && src[i] === '{') {
    const c = matchBrace(src, i);
    if (c !== -1) i = c + 1;
    skip();
  }
  let columnSpec = '';
  if (src[i] === '{') {
    const c = matchBrace(src, i);
    if (c !== -1) {
      columnSpec = src.slice(i + 1, c);
      i = c + 1;
    }
  }
  return { bodyStart: i, columnSpec };
}

function splitTopLevel(src: string, sep: string): string[] {
  const out: string[] = [];
  let cur = '', brace = 0, bracket = 0, math = false;
  for (let i = 0; i < src.length; i++) {
    if (src[i] === '\\') {
      if (sep === '\\\\' && src[i + 1] === '\\' && brace === 0 && bracket === 0 && !math) {
        out.push(cur); cur = ''; i++; continue;
      }
      cur += src[i];
      if (i + 1 < src.length) cur += src[++i];
      continue;
    }
    if (src[i] === '$') math = !math;
    else if (!math && src[i] === '{') brace++;
    else if (!math && src[i] === '}') brace = Math.max(0, brace - 1);
    else if (!math && src[i] === '[') bracket++;
    else if (!math && src[i] === ']') bracket = Math.max(0, bracket - 1);
    if (sep === '&' && src[i] === '&' && brace === 0 && bracket === 0 && !math) {
      out.push(cur); cur = ''; continue;
    }
    cur += src[i];
  }
  out.push(cur);
  return out;
}

function cleanTableCell(src: string): string {
  return src
    .replace(/\\hline\b/g, '')
    .replace(/\\cline\s*\{[^{}]*\}/g, '')
    .replace(/\\toprule\b|\\midrule\b|\\bottomrule\b/g, '')
    .trim();
}

function parseTable(body: string, columnSpec: string): Block {
  const withoutRules = body
    .replace(/\\hline\b/g, '\n')
    .replace(/\\toprule\b|\\midrule\b|\\bottomrule\b/g, '\n');
  const rows = splitTopLevel(withoutRules, '\\\\')
    .map((row) => splitTopLevel(row, '&').map((cell) => parseInline(cleanTableCell(cell))))
    .filter((row) => row.some((cell) => cell.some((n) => n.t !== 'text' || n.v.trim())));
  return { t: 'table', rows, columnSpec };
}

function parseEnv(name: string, raw: string): Block {
  const meta: EnvMeta = { lean: [], uses: [], leanok: false, mathlibok: false, notready: false };
  // Proof-side commands are meaningful to hgraph's dependency association,
  // but they are not declaration metadata in the document UI. In particular,
  // never let a proof equation's \label become the enclosing theorem label.
  if (name === 'proof') {
    let body = raw;
    const lead = body.replace(/^\s+/, '');
    if (lead[0] === '[') {
      const close = matchBracket(lead, 0);
      if (close !== -1) { meta.human = lead.slice(1, close).trim(); body = lead.slice(close + 1); }
    }
    return { t: 'env', name, meta, body: parseBlocks(body) };
  }

  // Metadata belongs to the environment header. The previous implementation
  // searched the entire body, so an unlabelled statement could inherit a
  // `\label`, `\lean`, or `\leanok` from its proof. Consume only a prefix made
  // of whitespace, optional comments, an optional human title, and known
  // annotation commands; ordinary statement text ends the header.
  let body = raw;
  const removed: Array<[number, number]> = [];
  let i = 0;
  let titleSeen = false;
  const skipWhitespace = () => { while (i < raw.length && /\s/.test(raw[i])) i++; };
  while (i < raw.length) {
    skipWhitespace();
    if (raw.startsWith('\\archoncomment{', i)) {
      const close = matchBrace(raw, i + '\\archoncomment'.length);
      if (close !== -1) { i = close + 1; continue; }
    }
    if (raw[i] === '[' && !titleSeen) {
      const close = matchBracket(raw, i);
      if (close !== -1) {
        meta.human = raw.slice(i + 1, close).trim();
        removed.push([i, close + 1]);
        titleSeen = true;
        i = close + 1;
        continue;
      }
    }
    const command = /^\\(label|lean|uses|discussion|dcref|source|group|level)\b/.exec(raw.slice(i));
    if (command) {
      let end = i + command[0].length;
      while (end < raw.length && /\s/.test(raw[end])) end++;
      if (raw[end] !== '{') break;
      const close = matchBrace(raw, end);
      if (close === -1) break;
      const value = raw.slice(end + 1, close).trim();
      switch (command[1]) {
        case 'label': if (!meta.label) meta.label = value; break;
        case 'lean': meta.lean.push(...value.split(',').map((s) => s.trim()).filter(Boolean)); break;
        case 'uses': meta.uses.push(...value.split(',').map((s) => s.trim()).filter(Boolean)); break;
      }
      removed.push([i, close + 1]);
      i = close + 1;
      continue;
    }
    const bare = /^\\(leanok|mathlibok|notready|sketch)\b/.exec(raw.slice(i));
    if (bare) {
      if (bare[1] === 'leanok') meta.leanok = true;
      else if (bare[1] === 'mathlibok') meta.mathlibok = true;
      else if (bare[1] === 'notready') meta.notready = true;
      removed.push([i, i + bare[0].length]);
      i += bare[0].length;
      continue;
    }
    break;
  }
  for (let r = removed.length - 1; r >= 0; r--) {
    const [start, end] = removed[r];
    body = body.slice(0, start) + body.slice(end);
  }
  return { t: 'env', name, meta, body: parseBlocks(body) };
}

// ── numbering pass (cheap — no KaTeX; runs over ALL chapters so cross-refs and
//    numbers stay global even when only some chapters are rendered) ───────────
export interface TocSection { num: string; anchor: string; title: Inline[]; level: 2 | 3; }
export interface NumberedChapter { slug: string; num: number; anchor: string; title: Inline[]; blocks: Block[]; sections: TocSection[]; }
export interface BlueprintModel { doc: NumberedChapter[]; labels: LabelMap; }

export function buildBlueprintModel(chapters: DocChapter[], showComments = true): BlueprintModel {
  const labels: LabelMap = new Map();
  const doc: NumberedChapter[] = [];
  let chapNum = 0;
  for (const raw of chapters) {
    chapNum++;
    const tex = encodeComments(raw.tex, showComments);
    const anchor = `ch-${raw.slug}`;
    const cut = Math.min(
      ...['\\section', '\\begin{'].map(t => { const k = tex.indexOf(t); return k === -1 ? tex.length : k; }),
    );
    const chLab = /\\label\s*\{([^{}]*)\}/.exec(tex.slice(0, cut));
    if (chLab) labels.set(chLab[1].trim(), { kind: 'Chapter', num: String(chapNum), anchor, slug: raw.slug });

    const blocks = parseBlocks(tex);
    const sections: TocSection[] = [];
    let sec = 0, sub = 0, stmt = 0;
    for (const b of blocks) {
      if (b.t === 'section') {
        if (b.level === 2) { sec++; sub = 0; b.num = `${chapNum}.${sec}`; }
        else { sub++; b.num = `${chapNum}.${sec || 1}.${sub}`; }
        b.anchor = `sec-${anchor}-${b.num.replace(/\./g, '_')}`;
        if (b.label) labels.set(b.label, { kind: 'Section', num: b.num, anchor: b.anchor, slug: raw.slug });
        sections.push({ num: b.num, anchor: b.anchor, title: b.title, level: b.level });
      } else if (b.t === 'env' && NUMBERED.has(b.name)) {
        stmt++; b.num = `${chapNum}.${stmt}`; b.anchor = `stmt-${anchor}-${b.num.replace(/\./g, '_')}`;
        if (b.meta.label) labels.set(b.meta.label, { kind: ENV_LABELS[b.name] ?? b.name, num: b.num, anchor: b.anchor, slug: raw.slug });
      }
    }
    doc.push({ slug: raw.slug, num: chapNum, anchor, title: parseInline(raw.title), blocks, sections });
  }
  return { doc, labels };
}

// ── rendering ────────────────────────────────────────────────────────────────
function renderMath(tex: string, display: boolean, macros: Record<string, string>): string {
  try {
    return katex.renderToString(tex, { displayMode: display, throwOnError: false, strict: 'ignore', macros, trust: false });
  } catch {
    return `<code>${escapeHtml(tex)}</code>`;
  }
}
function escapeHtml(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
/** Drop `\\[2pt]`-style spacing args KaTeX chokes on. */
function cleanDisplay(v: string): string {
  return v.replace(/\\\\\s*\[[^\]]*\]/g, '\\\\');
}

// ── Lean syntax highlighting (mirrors the DAG view) ──────────────────────────
const LN_KW = /\b(def|lemma|theorem|instance|class|structure|inductive|abbrev|noncomputable|private|protected|section|namespace|end|open|variable|where|do|let|have|show|suffices|calc|if|then|else|return|for|in|fun|match|with|by|sorry)\b/g;
const LN_TAC = /\b(simp|ext|rfl|exact|intro|intros|apply|refine|constructor|use|rw|rewrite|rcases|rintro|obtain|push_neg|norm_num|ring|linarith|omega|decide|trivial|assumption|congr|tauto|aesop|cases|induction|revert|clear|all_goals|repeat|try|first|fin_cases|positivity|gcongr|field_simp)\b/g;
const LN_TY = /\b(Nat|Int|Bool|String|List|Array|Option|Type|Prop|Sort|True|False|And|Or|Not|Iff|Eq|Scheme|Module|Ring|Field|CommRing|Ideal|Set)\b/g;
const LN_NUM = /\b(\d+)\b/g;
const LN_STR = /("(?:[^"\\]|\\.)*")/g;
function highlightLean(raw: string): string {
  return raw.split('\n').map((line) => {
    let commentAt = -1, inStr = false;
    for (let i = 0; i < line.length - 1; i++) {
      if (line[i] === '"' && (i === 0 || line[i - 1] !== '\\')) inStr = !inStr;
      if (!inStr && line[i] === '-' && line[i + 1] === '-') { commentAt = i; break; }
    }
    const code = commentAt >= 0 ? line.slice(0, commentAt) : line;
    const comment = commentAt >= 0 ? line.slice(commentAt) : '';
    let h = escapeHtml(code);
    h = h.replace(LN_STR, m => `<span class="${styles.hlStr}">${m}</span>`);
    h = h.replace(LN_KW, m => `<span class="${styles.hlKw}">${m}</span>`);
    h = h.replace(LN_TAC, m => `<span class="${styles.hlTac}">${m}</span>`);
    h = h.replace(LN_TY, m => `<span class="${styles.hlTy}">${m}</span>`);
    h = h.replace(LN_NUM, m => `<span class="${styles.hlNum}">${m}</span>`);
    return h + (comment ? `<span class="${styles.hlComment}">${escapeHtml(comment)}</span>` : '');
  }).join('\n');
}

interface Ctx {
  macros: Record<string, string>;
  labels: LabelMap;
  bib: BibMap;
  lean: Map<string, string>;
  /** Open the target's chapter (lazy view) and scroll to its anchor. */
  onNavigate?: (slug: string, anchor: string) => void;
  /** Jump to this declaration's node on the DAG page. */
  onOpenInGraph?: (label: string) => void;
  /** Open this declaration in the Lean source view. */
  onOpenInLean?: (name: string) => void;
  /** Snapshot slug of this declaration's Lean file (null = no diff target). */
  diffSlugFor?: (label: string) => string | null;
  /** Open a Lean file's snapshot timeline on the Diffs page. */
  onOpenInDiffs?: (slug: string) => void;
  /** Last archon commit that touched this declaration's Lean file. */
  leanModFor?: (label: string) => BlueprintFileMod | undefined;
  /** Open an iteration's logs (from a ✎ iter-NNN chip). */
  onOpenLogs?: (iter: string) => void;
}

/** Mirrors hooks/useDag's FileMod (kept structural to avoid a hook import). */
export interface BlueprintFileMod {
  sha: string; date: string; subject: string; iteration?: string; phase?: string;
}

/** "✎ iter-NNN/phase" chip → the iteration's logs. */
function IterChip({ kind, mod, onOpenLogs }: {
  kind: string; mod: BlueprintFileMod | undefined; onOpenLogs?: (iter: string) => void;
}) {
  if (!mod?.iteration || !onOpenLogs) return null;
  return (
    <button className={styles.iterChip} title={`${kind} last modified by:\n${mod.subject}\n${mod.date} — click to open the logs`}
      onClick={() => onOpenLogs(mod.iteration!)}>
      ✎ {mod.iteration}{mod.phase ? `/${mod.phase}` : ''}
    </button>
  );
}

/** Cross-reference click: route through onNavigate so a target in a not-yet-
 *  rendered chapter gets its chapter opened first (plain #hash would no-op). */
function refClick(ctx: Ctx, tgt: RefTarget) {
  return (e: React.MouseEvent) => {
    if (!ctx.onNavigate) return; // fall back to the plain anchor
    e.preventDefault();
    ctx.onNavigate(tgt.slug, tgt.anchor);
  };
}

function Inlines({ nodes, ctx, k }: { nodes: Inline[]; ctx: Ctx; k: string }) {
  return <>{nodes.map((n, idx) => <InlineNode key={`${k}-${idx}`} n={n} ctx={ctx} k={`${k}-${idx}`} />)}</>;
}
function RefLink({ label, cref, ctx }: { label: string; cref: boolean; ctx: Ctx }) {
  const tgt = ctx.labels.get(label);
  if (!tgt) return <span className={styles.refBroken} title={`unresolved: ${label}`}>??</span>;
  const text = cref ? `${tgt.kind} ${tgt.num}` : tgt.num;
  return <a className={styles.ref} href={`#${tgt.anchor}`} onClick={refClick(ctx, tgt)} title={label}>{text}</a>;
}

function CiteLink({ keyName, style, ctx }: {
  keyName: string;
  style: 'cite' | 'citet' | 'citep' | 'citeauthor' | 'citeyear';
  ctx: Ctx;
}) {
  const entry = ctx.bib.get(keyName);
  const text = citeLabel(entry, keyName, style);
  if (!entry) {
    return <span className={styles.citeBroken} title={`unresolved citation: ${keyName}`}>[{keyName}]</span>;
  }
  const tip = [entry.title, entry.author, entry.year].filter(Boolean).join(' — ');
  return (
    <a className={styles.cite} href={`#bib-${encodeURIComponent(keyName)}`} title={tip || keyName}>
      {text}
    </a>
  );
}

function InlineNode({ n, ctx, k }: { n: Inline; ctx: Ctx; k: string }) {
  if (n.t === 'text') return <>{normText(n.v)}</>;
  if (n.t === 'math') return <span className={n.display ? styles.dmath : styles.imath} dangerouslySetInnerHTML={{ __html: renderMath(n.v, n.display, ctx.macros) }} />;
  if (n.t === 'strong') return <strong><Inlines nodes={n.c} ctx={ctx} k={k} /></strong>;
  if (n.t === 'em') return <em><Inlines nodes={n.c} ctx={ctx} k={k} /></em>;
  if (n.t === 'code') return <code className={styles.tt}>{n.v}</code>;
  if (n.t === 'comment') return <CommentChip value={n.v} ctx={ctx} />;
  if (n.t === 'ref') {
    return (
      <>
        {n.labels.map((label, i) => (
          <span key={`${k}-r-${i}`}>
            {i > 0 && <>, </>}
            <RefLink label={label} cref={n.cref} ctx={ctx} />
          </span>
        ))}
      </>
    );
  }
  if (n.t === 'cite') {
    const bracketed = n.style === 'cite' || n.style === 'citep';
    const body = n.keys.map((keyName, i) => (
      <span key={`${k}-c-${i}`}>
        {i > 0 && <>; </>}
        <CiteLink keyName={keyName} style={n.style} ctx={ctx} />
      </span>
    ));
    const note = (s?: string) => (s ? <>{normText(s)}</> : null);
    if (bracketed) {
      return (
        <span className={styles.citeGroup}>
          [{n.prenote ? <>{note(n.prenote)} </> : null}
          {body}
          {n.postnote ? <>, {note(n.postnote)}</> : null}]
        </span>
      );
    }
    // \citet / \citeauthor / \citeyear: prose form; postnote still parenthetical.
    return (
      <span className={styles.citeGroup}>
        {n.prenote ? <>{note(n.prenote)} </> : null}
        {body}
        {n.postnote ? <> ({note(n.postnote)})</> : null}
      </span>
    );
  }
  return null;
}

function LeanChip({ name, ctx }: { name: string; ctx: Ctx }) {
  const [open, setOpen] = useState(false);
  const src = ctx.lean.get(name);
  return (
    <span className={styles.leanWrap}>
      {ctx.onOpenInLean && (
        <button className={styles.graphChip} title="Open this declaration in the Lean page"
          onClick={() => ctx.onOpenInLean!(name)}>lean</button>
      )}
      <button className={`${styles.leanChip} ${open ? styles.leanOpen : ''}`} onClick={() => setOpen(o => !o)}
        title={src ? 'Show Lean source' : 'No Lean source found'}>
        λ {name.split('.').pop()}
      </button>
      {open && (src
        ? <pre className={styles.leanCode} dangerouslySetInnerHTML={{ __html: highlightLean(src) }} />
        : <pre className={styles.leanCode}>{`-- Lean source for ${name} not found in the cached graph.`}</pre>
      )}
    </span>
  );
}

function UsesChips({ uses, ctx }: { uses: string[]; ctx: Ctx }) {
  if (!uses.length) return null;
  return (
    <span className={styles.usesRow}>
      <span className={styles.usesLabel}>uses</span>
      {uses.map((u, i) => {
        const tgt = ctx.labels.get(u);
        return tgt
          ? <a key={i} className={styles.usesChip} href={`#${tgt.anchor}`} onClick={refClick(ctx, tgt)} title={`${tgt.kind} ${tgt.num}`}>{tgt.kind} {tgt.num}</a>
          : <span key={i} className={`${styles.usesChip} ${styles.usesUnknown}`} title={`unresolved: ${u}`}>{u.split(':').pop()}</span>;
      })}
    </span>
  );
}

function CommentChip({ value, ctx }: { value: string; ctx: Ctx }) {
  const [open, setOpen] = useState(false);
  const entries = useMemo(() => parseCommentEntries(value), [value]);
  if (!entries.length) return null;
  const tags = Array.from(new Set(entries.map(e => e.tag).filter(Boolean)));
  const label = tags.length ? tags.map(t => t.toLowerCase()).join(' · ') : 'note';
  return (
    <span className={styles.cWrap}>
      <button className={`${styles.cToggle} ${open ? styles.cOpen : ''}`} onClick={() => setOpen(o => !o)}
        title={`Note — ${label}`} aria-label={`Note — ${label}`}>
        <span className={styles.quote}>“</span>
      </button>
      {open && (
        <span className={styles.cBody}>
          {entries.map((e, i) => (
            <span key={i} className={styles.cEntry}>
              {e.tag && <span className={styles.cTag}>{e.tag.toLowerCase()}</span>}
              <Inlines nodes={parseInline(e.text)} ctx={ctx} k={`c${i}`} />
            </span>
          ))}
        </span>
      )}
    </span>
  );
}
const TAG_RE = /^(SOURCE QUOTE PROOF|SOURCE QUOTE|SOURCE|NOTE|TODO|FIXME)\b\s*:?\s*/i;
function parseCommentEntries(value: string): { tag: string; text: string }[] {
  const entries: { tag: string; text: string }[] = [];
  let cur: { tag: string; text: string } | null = null;
  for (const line of value.split('\n')) {
    const m = TAG_RE.exec(line);
    if (m) { if (cur) entries.push(cur); cur = { tag: m[1].toUpperCase(), text: line.slice(m[0].length) }; }
    else if (cur) cur.text += '\n' + line;
    else cur = { tag: '', text: line };
  }
  if (cur) entries.push(cur);
  return entries.filter(e => e.text.trim() || e.tag);
}

// Memoized so a chapter can stream its blocks in progressively (see ChapterView)
// without re-running KaTeX on the blocks already rendered — block objects and
// `ctx` keep a stable identity, so growing the visible count stays O(n).
const BlockNode = memo(function BlockNode({ b, ctx, k }: { b: Block; ctx: Ctx; k: string }) {
  if (b.t === 'para') return <p className={styles.p}><Inlines nodes={b.c} ctx={ctx} k={k} /></p>;
  if (b.t === 'paragraph') return <h5 className={styles.paragraphHead}><Inlines nodes={b.title} ctx={ctx} k={k} /></h5>;
  if (b.t === 'displaymath') return <div className={styles.dblock} dangerouslySetInnerHTML={{ __html: renderMath(cleanDisplay(b.v), true, ctx.macros) }} />;
  if (b.t === 'pre') return <pre className={styles.preBlock}>{b.v}</pre>;
  if (b.t === 'center') return <div className={styles.centerBlock}>{b.body.map((bb, j) => <BlockNode key={j} b={bb} ctx={ctx} k={`${k}-c-${j}`} />)}</div>;
  if (b.t === 'table') {
    return (
      <div className={styles.tableWrap}>
        <table className={styles.table}>
          <tbody>
            {b.rows.map((row, r) => (
              <tr key={r}>
                {row.map((cell, c) => {
                  const Tag = r === 0 ? 'th' : 'td';
                  return <Tag key={c}><Inlines nodes={cell} ctx={ctx} k={`${k}-t-${r}-${c}`} /></Tag>;
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  }
  if (b.t === 'list') {
    const Tag = b.ordered ? 'ol' : 'ul';
    return <Tag className={styles.list}>{b.items.map((it, i) => <li key={i}>{it.map((bb, j) => <BlockNode key={j} b={bb} ctx={ctx} k={`${k}-${i}-${j}`} />)}</li>)}</Tag>;
  }
  if (b.t === 'section') {
    const H = b.level === 2 ? 'h3' : 'h4';
    return <H id={b.anchor} className={b.level === 2 ? styles.h3 : styles.h4}>
      <span className={styles.secNum}>{b.num}</span> <Inlines nodes={b.title} ctx={ctx} k={k} />
    </H>;
  }
  // env
  const isProof = b.name === 'proof';
  // Non-obligation environments (documentation, conjectures, exercises, and
  // proofs) are not formalisation targets:
  // carry no DAG node worth focusing, so status and graph affordances would be
  // misleading. Keep rendering their prose and proof-side `\uses` as usual.
  const isProse = [
    'remark', 'notation', 'convention', 'example', 'conjecture', 'claim', 'fact',
    'exercise', 'note', 'proposition_',
  ].includes(b.name);
  const isNonFormalization = isProof || isProse;
  const labelText = ENV_LABELS[b.name] ?? (b.name[0]?.toUpperCase() + b.name.slice(1));
  const klass = `${styles.env} ${styles[`env_${b.name}`] ?? ''} ${isProof ? styles.envProof : ''}`;
  return (
    <div id={b.anchor} className={klass}>
      <div className={styles.envHead}>
        <span className={styles.envLabel}>
          {labelText}{b.num ? ` ${b.num}` : ''}.
        </span>
        {b.meta.human && <span className={styles.envHuman}><Inlines nodes={parseInline(b.meta.human)} ctx={ctx} k={`${k}-h`} /></span>}
        {!isNonFormalization && b.meta.leanok && <span className={styles.badgeOk} title="\leanok">✓ leanok</span>}
        {!isNonFormalization && b.meta.mathlibok && <span className={styles.badgeMathlib} title="\mathlibok">ⓜ mathlib</span>}
        {!isNonFormalization && b.meta.notready && <span className={styles.badgeNot} title="\notready">not ready</span>}
        {b.meta.label && ctx.onOpenInGraph && !isNonFormalization && (
          <button className={styles.graphChip} title="Show this node on the DAG page"
            onClick={() => ctx.onOpenInGraph!(b.meta.label!)}>⬡ graph</button>
        )}
        {b.meta.label && !isNonFormalization && ctx.onOpenInDiffs && ctx.diffSlugFor?.(b.meta.label) && (
          <button className={styles.graphChip} title="Open this declaration's Lean file on the Diffs page"
            onClick={() => ctx.onOpenInDiffs!(ctx.diffSlugFor!(b.meta.label!)!)}>± diff</button>
        )}
        {b.meta.label && !isNonFormalization && (
          <IterChip kind="Lean file" mod={ctx.leanModFor?.(b.meta.label)} onOpenLogs={ctx.onOpenLogs} />
        )}
        {!isNonFormalization && b.meta.lean.map((nm, i) => <LeanChip key={i} name={nm} ctx={ctx} />)}
      </div>
      <div className={styles.envBody}>
        {b.body.map((bb, j) => <BlockNode key={j} b={bb} ctx={ctx} k={`${k}-${j}`} />)}
        <UsesChips uses={b.meta.uses} ctx={ctx} />
      </div>
    </div>
  );
});

/** Render one already-numbered chapter (this is where KaTeX runs — call it only
 *  for chapters the user has actually selected, so the page loads lazily). */
export function ChapterView({
  chapter, macros, labels, bib, leanSource, onNavigate, onOpenInGraph, onOpenInLean, diffSlugFor, onOpenInDiffs, leanModFor, onOpenLogs, chapterMod,
}: {
  chapter: NumberedChapter;
  macros: Record<string, string>;
  labels: LabelMap;
  bib?: BibMap;
  leanSource: Map<string, string>;
  onNavigate?: (slug: string, anchor: string) => void;
  onOpenInGraph?: (label: string) => void;
  onOpenInLean?: (name: string) => void;
  diffSlugFor?: (label: string) => string | null;
  onOpenInDiffs?: (slug: string) => void;
  leanModFor?: (label: string) => BlueprintFileMod | undefined;
  onOpenLogs?: (iter: string) => void;
  /** Last archon commit that touched this chapter's .tex (statements + proofs). */
  chapterMod?: BlueprintFileMod;
}) {
  const ctx: Ctx = useMemo(
    () => ({
      macros,
      labels,
      bib: bib ?? new Map(),
      lean: leanSource,
      onNavigate,
      onOpenInGraph,
      onOpenInLean,
      diffSlugFor,
      onOpenInDiffs,
      leanModFor,
      onOpenLogs,
    }),
    [macros, labels, bib, leanSource, onNavigate, onOpenInGraph, onOpenInLean, diffSlugFor, onOpenInDiffs, leanModFor, onOpenLogs],
  );
  // Progressive rendering: paint the top of the chapter immediately and stream
  // the remaining (KaTeX-heavy) blocks in over the next frames, so opening a big
  // chapter doesn't freeze until every theorem has typeset. Reset per chapter.
  const shown = useProgressiveCount(chapter.blocks.length, {
    resetKey: chapter.anchor,
    initial: 12,
    step: 24,
  });
  return (
    <section id={chapter.anchor} className={`${styles.root} ${styles.chapter}`}>
      <h2 className={styles.chapterTitle}>
        <span className={styles.chapNum}>{chapter.num}</span> <Inlines nodes={chapter.title} ctx={ctx} k={`ch${chapter.num}`} />
        <IterChip kind="Chapter .tex (statements & proofs)" mod={chapterMod} onOpenLogs={onOpenLogs} />
      </h2>
      {chapter.blocks.slice(0, shown).map((b, j) => <BlockNode key={j} b={b} ctx={ctx} k={`ch${chapter.num}-${j}`} />)}
      {shown < chapter.blocks.length && <p className={styles.fragEmpty}>Rendering the rest of this chapter…</p>}
    </section>
  );
}

/** Render an inline-LaTeX title (math-aware) to React — for the TOC / headings. */
export function TitleInline({ nodes, tex, macros }: { nodes?: Inline[]; tex?: string; macros: Record<string, string> }) {
  const parsed = useMemo(() => nodes ?? parseInline(tex ?? ''), [nodes, tex]);
  const ctx: Ctx = { macros, labels: new Map(), bib: new Map(), lean: new Map() };
  return <Inlines nodes={parsed} ctx={ctx} k="t" />;
}

/** Render a project's bibliography (parsed `.bib`) under the open chapters. */
export function BibliographyView({ bib }: { bib: BibEntry[] }) {
  if (!bib.length) return null;
  const sorted = [...bib].sort((a, b) => a.key.localeCompare(b.key));
  return (
    <section className={`${styles.root} ${styles.bibSection}`} id="bibliography">
      <h2 className={styles.chapterTitle}>Bibliography</h2>
      <ol className={styles.bibList}>
        {sorted.map((e) => {
          const venue = e.journal || e.booktitle || e.publisher || '';
          const detail = [
            venue,
            e.volume ? (e.number ? `${e.volume}(${e.number})` : e.volume) : (e.number || ''),
            e.pages ? `pp. ${e.pages}` : '',
            e.year || '',
          ].filter(Boolean).join(', ');
          return (
            <li key={e.key} id={`bib-${encodeURIComponent(e.key)}`} className={styles.bibItem}>
              <span className={styles.bibKey}>[{e.key}]</span>
              <span className={styles.bibBody}>
                {e.author && <span className={styles.bibAuthor}>{e.author}. </span>}
                {e.title && (
                  e.url
                    ? <a className={styles.bibTitle} href={e.url} target="_blank" rel="noreferrer">{e.title}</a>
                    : <span className={styles.bibTitle}>{e.title}</span>
                )}
                {e.title && detail ? '. ' : null}
                {detail && <span className={styles.bibDetail}>{detail}.</span>}
              </span>
            </li>
          );
        })}
      </ol>
    </section>
  );
}

/** Render a loose LaTeX fragment (a statement or proof body from the DAG)
 *  through the same pipeline as the Blueprint page — math, \emph, comments,
 *  and \cref{} resolved against the full blueprint's label map. Used by the
 *  DAG node panel so the two pages read identically. */
/** A theorem body extracted for the DAG panel can still carry the environment's
 *  optional `[human name]` argument as a leading token. The panel already shows
 *  that name as the node title, so drop a leading `[...]` (after any stray
 *  metadata commands) rather than rendering raw brackets. */
function stripStmtHead(tex: string): string {
  const lead = tex.replace(/^\s+/, '');
  if (lead[0] !== '[') return tex;
  const close = matchBracket(lead, 0);
  return close === -1 ? tex : lead.slice(close + 1);
}

export function TexFragment({ tex, macros, labels, bib, onNavigate }: {
  tex: string | null | undefined;
  macros: Record<string, string>;
  labels?: LabelMap;
  bib?: BibMap;
  onNavigate?: (slug: string, anchor: string) => void;
}) {
  const blocks = useMemo(() => parseBlocks(encodeComments(stripStmtHead(tex ?? ''), true)), [tex]);
  const ctx: Ctx = useMemo(
    () => ({
      macros,
      labels: labels ?? new Map(),
      bib: bib ?? new Map(),
      lean: new Map(),
      onNavigate,
    }),
    [macros, labels, bib, onNavigate],
  );
  if (!tex || !tex.trim()) return <span className={styles.fragEmpty}>—</span>;
  return (
    <div className={styles.root}>
      {blocks.map((b, j) => <BlockNode key={j} b={b} ctx={ctx} k={`frag-${j}`} />)}
    </div>
  );
}
