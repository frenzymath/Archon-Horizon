import { createSorryScannerState, scanLineForSorry } from './sorryScanner';

export interface HighlightToken {
  text: string;
  cls?: string;
}

const KEYWORD_RE = /\b(import|universe|namespace|section|end|open|variable|variables|parameter|parameters|axiom|theorem|lemma|example|def|instance|class|structure|inductive|abbrev|alias|noncomputable|by|where|fun|match|with|if|then|else|let|in|have|show|from|intro|simp|simpa|rw|rfl|exact|apply|constructor|cases|induction|calc|do|termination_by|decreasing_by)\b/g;
const DECL_KIND_RE = /\b(lemma|theorem|example|def|instance|class|structure|inductive|abbrev)\b/g;

interface MatchSpan {
  start: number;
  end: number;
  cls: string;
}

interface HighlightState {
  blockDepth: number;
  inString: boolean;
}

function collect(regex: RegExp, text: string, cls: string): MatchSpan[] {
  regex.lastIndex = 0;
  const spans: MatchSpan[] = [];
  let match: RegExpExecArray | null;
  while ((match = regex.exec(text)) !== null) {
    spans.push({ start: match.index, end: match.index + match[0].length, cls });
    if (match.index === regex.lastIndex) regex.lastIndex++;
  }
  return spans;
}

function collectCommentSpans(text: string, state: HighlightState): MatchSpan[] {
  const spans: MatchSpan[] = [];
  let i = 0;
  let commentStart: number | null = state.blockDepth > 0 ? 0 : null;

  while (i < text.length) {
    const ch = text[i];
    const next = text[i + 1];

    if (state.blockDepth > 0) {
      if (ch === '/' && next === '-') {
        state.blockDepth += 1;
        i += 2;
        continue;
      }
      if (ch === '-' && next === '/') {
        state.blockDepth -= 1;
        i += 2;
        if (state.blockDepth === 0 && commentStart != null) {
          spans.push({ start: commentStart, end: i, cls: 'comment' });
          commentStart = null;
        }
        continue;
      }
      i += 1;
      continue;
    }

    if (state.inString) {
      if (ch === '\\' && next) {
        i += 2;
        continue;
      }
      if (ch === '"') state.inString = false;
      i += 1;
      continue;
    }

    if (ch === '"') {
      state.inString = true;
      i += 1;
      continue;
    }

    if (ch === '-' && next === '-') {
      spans.push({ start: i, end: text.length, cls: 'comment' });
      return spans;
    }

    if (ch === '/' && next === '-') {
      state.blockDepth = 1;
      commentStart = i;
      i += 2;
      continue;
    }

    i += 1;
  }

  if (commentStart != null && state.blockDepth > 0) {
    spans.push({ start: commentStart, end: text.length, cls: 'comment' });
  }

  return spans;
}

function toTokens(text: string, spans: MatchSpan[]): HighlightToken[] {
  const filtered: MatchSpan[] = [];
  for (const span of spans) {
    const overlap = filtered.some(existing => !(span.end <= existing.start || span.start >= existing.end));
    if (!overlap) filtered.push(span);
  }
  filtered.sort((a, b) => a.start - b.start);

  const out: HighlightToken[] = [];
  let cursor = 0;
  for (const span of filtered) {
    if (span.start > cursor) out.push({ text: text.slice(cursor, span.start) });
    out.push({ text: text.slice(span.start, span.end), cls: span.cls });
    cursor = span.end;
  }
  if (cursor < text.length) out.push({ text: text.slice(cursor) });
  return out.length ? out : [{ text }];
}

function highlightLeanLineWithState(text: string, state: HighlightState): HighlightToken[] {
  if (!text) return [{ text: '' }];

  const commentSpans = collectCommentSpans(text, state);
  const sorrySpans = scanLineForSorry(text, createSorryScannerState()).map(({ column }) => ({
    start: column - 1,
    end: column - 1 + 'sorry'.length,
    cls: 'sorry',
  }));

  const spans = [
    ...commentSpans,
    ...sorrySpans,
    ...collect(DECL_KIND_RE, text, 'decl'),
    ...collect(KEYWORD_RE, text, 'kw'),
  ].sort((a, b) => a.start - b.start || b.end - a.end);

  return toTokens(text, spans);
}

export function highlightLeanLine(text: string): HighlightToken[] {
  return highlightLeanLineWithState(text, { blockDepth: 0, inString: false });
}

export function highlightLeanLines(lines: string[]): HighlightToken[][] {
  const state: HighlightState = { blockDepth: 0, inString: false };
  return lines.map(line => highlightLeanLineWithState(line, state));
}
