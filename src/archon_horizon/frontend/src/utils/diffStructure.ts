import type { LeanStructureKind } from './leanStructure';
import { createSorryScannerState, scanLineForSorry } from './sorryScanner';

export interface DiffRenderableLine {
  id?: string;
  type: 'keep' | 'add' | 'remove' | 'hunk';
  oldNum?: number;
  newNum?: number;
  content: string;
}

export interface DiffStructureItem {
  id: string;
  kind: LeanStructureKind | 'hunk';
  label: string;
  lineLabel: string;
}

const DECL_RE = /^\s*(lemma|theorem|example|def|instance|class|structure|inductive|abbrev)\s+([^\s:(\[{]+)/;

export function parseDiffWithStructure(diff: string): {
  lines: DiffRenderableLine[];
  items: DiffStructureItem[];
} {
  const raw = diff.split('\n');
  const lines: DiffRenderableLine[] = [];
  const items: DiffStructureItem[] = [];
  let oldNum = 0;
  let newNum = 0;
  let hunkCount = 0;
  let sorryCount = 0;
  const sorryScannerState = createSorryScannerState();

  for (const line of raw) {
    if (line.startsWith('@@')) {
      const match = line.match(/@@ -(\d+),?\d* \+(\d+),?\d* @@/);
      if (match) {
        oldNum = parseInt(match[1], 10);
        newNum = parseInt(match[2], 10);
      }
      hunkCount += 1;
      const id = `hunk-${hunkCount}`;
      lines.push({ id, type: 'hunk', content: line });
      items.push({ id, kind: 'hunk', label: `change block ${hunkCount}`, lineLabel: line });
      continue;
    }
    if (line.startsWith('---') || line.startsWith('+++')) continue;

    let render: DiffRenderableLine;
    let analyzable = line;
    let kind: DiffRenderableLine['type'] = 'keep';

    if (line.startsWith('-')) {
      render = { type: 'remove', oldNum, content: line.slice(1) };
      analyzable = line.slice(1);
      kind = 'remove';
      oldNum++;
    } else if (line.startsWith('+')) {
      render = { type: 'add', newNum, content: line.slice(1) };
      analyzable = line.slice(1);
      kind = 'add';
      newNum++;
    } else if (line.startsWith(' ')) {
      render = { type: 'keep', oldNum, newNum, content: line.slice(1) };
      analyzable = line.slice(1);
      oldNum++;
      newNum++;
    } else {
      render = { type: 'keep', oldNum, newNum, content: '' };
      oldNum++;
      newNum++;
    }

    const lineNo = render.newNum ?? render.oldNum ?? 0;
    const decl = analyzable.match(DECL_RE);
    const hasSorry = scanLineForSorry(analyzable, sorryScannerState).length > 0;
    if (decl) {
      const declKind = decl[1] as LeanStructureKind;
      const name = decl[2];
      const id = `${declKind}-${kind}-${lineNo}-${name}`;
      render.id = id;
      items.push({ id, kind: declKind, label: name, lineLabel: `${kind} line ${lineNo}` });
    } else if (hasSorry) {
      sorryCount += 1;
      const id = `sorry-${kind}-${lineNo}-${sorryCount}`;
      render.id = id;
      items.push({ id, kind: 'sorry', label: `sorry @ line ${lineNo}`, lineLabel: `${kind} line ${lineNo}` });
    }

    lines.push(render);
  }

  return { lines, items };
}
