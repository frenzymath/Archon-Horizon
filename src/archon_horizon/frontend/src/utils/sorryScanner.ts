export interface SorryScannerState {
  blockDepth: number;
  inString: boolean;
  inLineComment: boolean;
}

export interface SorryOccurrence {
  column: number;
}

function isIdentChar(ch: string | undefined) {
  return !!ch && /[A-Za-z0-9_!?']/.test(ch);
}

export function createSorryScannerState(): SorryScannerState {
  return {
    blockDepth: 0,
    inString: false,
    inLineComment: false,
  };
}

export function scanLineForSorry(line: string, state: SorryScannerState): SorryOccurrence[] {
  const out: SorryOccurrence[] = [];
  let i = 0;
  let col = 1;

  const bump = (ch: string) => {
    if (ch === '\n') {
      state.inLineComment = false;
      col = 1;
    } else {
      col += 1;
    }
  };

  while (i < line.length) {
    const ch = line[i];
    const next = line[i + 1];

    if (state.inLineComment) {
      bump(ch);
      i += 1;
      continue;
    }

    if (state.blockDepth > 0) {
      if (ch === '/' && next === '-') {
        state.blockDepth += 1;
        bump(ch); bump(next);
        i += 2;
        continue;
      }
      if (ch === '-' && next === '/') {
        state.blockDepth -= 1;
        bump(ch); bump(next);
        i += 2;
        continue;
      }
      bump(ch);
      i += 1;
      continue;
    }

    if (state.inString) {
      if (ch === '\\' && next) {
        bump(ch); bump(next);
        i += 2;
        continue;
      }
      if (ch === '"') state.inString = false;
      bump(ch);
      i += 1;
      continue;
    }

    if (ch === '-' && next === '-') {
      state.inLineComment = true;
      bump(ch); bump(next);
      i += 2;
      continue;
    }

    if (ch === '/' && next === '-') {
      state.blockDepth = 1;
      bump(ch); bump(next);
      i += 2;
      continue;
    }

    if (ch === '"') {
      state.inString = true;
      bump(ch);
      i += 1;
      continue;
    }

    if (line.startsWith('sorry', i)) {
      const prev = line[i - 1];
      const after = line[i + 5];
      if (!isIdentChar(prev) && !isIdentChar(after)) {
        out.push({ column: col });
        for (const c of 'sorry') bump(c);
        i += 5;
        continue;
      }
    }

    bump(ch);
    i += 1;
  }

  state.inLineComment = false;
  return out;
}

export function scanLinesForSorry(lines: string[]): Map<number, SorryOccurrence[]> {
  const state = createSorryScannerState();
  const result = new Map<number, SorryOccurrence[]>();

  lines.forEach((line, idx) => {
    const occurrences = scanLineForSorry(line, state);
    if (occurrences.length > 0) result.set(idx + 1, occurrences);
  });

  return result;
}
