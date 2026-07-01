import { scanLinesForSorry } from './sorryScanner';

export type LeanStructureKind =
  | 'lemma'
  | 'theorem'
  | 'example'
  | 'def'
  | 'instance'
  | 'class'
  | 'structure'
  | 'inductive'
  | 'abbrev'
  | 'sorry';

export interface LeanStructureMatch {
  id: string;
  kind: LeanStructureKind;
  label: string;
  line: number;
}

const DECL_RE = /^\s*(lemma|theorem|example|def|instance|class|structure|inductive|abbrev)\s+([^\s:(\[{]+)/;

export function extractLeanStructureFromLines(lines: string[]): LeanStructureMatch[] {
  const items: LeanStructureMatch[] = [];
  const sorryByLine = scanLinesForSorry(lines);
  let sorryCount = 0;

  lines.forEach((line, idx) => {
    const decl = line.match(DECL_RE);
    if (decl) {
      const kind = decl[1] as LeanStructureKind;
      const name = decl[2];
      items.push({
        id: `${kind}-${idx + 1}-${name}`,
        kind,
        label: name,
        line: idx + 1,
      });
    }

    if (sorryByLine.has(idx + 1)) {
      sorryCount += 1;
      items.push({
        id: `sorry-${idx + 1}-${sorryCount}`,
        kind: 'sorry',
        label: `sorry @ line ${idx + 1}`,
        line: idx + 1,
      });
    }
  });

  return items;
}

export function groupStructureCounts(items: LeanStructureMatch[]) {
  return items.reduce<Record<string, number>>((acc, item) => {
    acc[item.kind] = (acc[item.kind] || 0) + 1;
    return acc;
  }, {});
}
