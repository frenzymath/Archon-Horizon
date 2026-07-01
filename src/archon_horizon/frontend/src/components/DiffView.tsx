/**
 * DiffView — renders a unified diff as a styled table.
 *
 * This component stays presentation-focused:
 * - consumes already-parsed diff structure
 * - renders line anchors / active highlight
 * - reuses LeanCodeLine for lightweight syntax emphasis
 */
import { useMemo } from 'react';
import styles from './DiffView.module.css';
import LeanCodeLine from './LeanCodeLine';
import { parseDiffWithStructure } from '../utils/diffStructure';
import { highlightLeanLines } from '../utils/leanHighlight';

interface DiffViewProps {
  diff: string;
  fromFile?: string;
  toFile?: string;
  addedLines?: number;
  removedLines?: number;
  activeId?: string;
}

export default function DiffView({ diff, fromFile, toFile, addedLines, removedLines, activeId }: DiffViewProps) {
  const { lines } = useMemo(() => parseDiffWithStructure(diff), [diff]);
  const highlightedLines = useMemo(
    () => highlightLeanLines(lines.map(line => line.type === 'hunk' ? '' : line.content)),
    [lines],
  );

  if (!diff || lines.length === 0) {
    return (
      <div className={styles.diffView}>
        <div className={styles.emptyDiff}>No changes</div>
      </div>
    );
  }

  return (
    <div className={styles.diffView}>
      {(fromFile || toFile || addedLines !== undefined || removedLines !== undefined) && (
        <div className={styles.diffHeader}>
          {fromFile && toFile && <span>{fromFile} → {toFile}</span>}
          <div className={styles.diffStats}>
            {addedLines !== undefined && addedLines > 0 && (
              <span className={styles.diffAdded}>+{addedLines}</span>
            )}
            {removedLines !== undefined && removedLines > 0 && (
              <span className={styles.diffRemoved}>−{removedLines}</span>
            )}
          </div>
        </div>
      )}

      <div className={styles.diffBody}>
        <table className={styles.diffTable}>
          <tbody>
            {lines.map((line, i) => {
              const activeClass = activeId && line.id === activeId ? styles.lineActive : '';

              if (line.type === 'hunk') {
                return (
                  <tr key={i} id={line.id} className={activeClass}>
                    <td colSpan={3} className={styles.hunkSep}>{line.content}</td>
                  </tr>
                );
              }

              const rowClass =
                line.type === 'add' ? styles.lineAdd :
                line.type === 'remove' ? styles.lineRemove :
                styles.lineKeep;

              return (
                <tr key={i} id={line.id} className={`${styles.diffLine} ${rowClass} ${activeClass}`}>
                  <td className={styles.lineNum}>{line.type !== 'add' ? line.oldNum : ''}</td>
                  <td className={styles.lineNum}>{line.type !== 'remove' ? line.newNum : ''}</td>
                  <td className={styles.lineContent}><LeanCodeLine text={line.content} tokens={highlightedLines[i]} /></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
