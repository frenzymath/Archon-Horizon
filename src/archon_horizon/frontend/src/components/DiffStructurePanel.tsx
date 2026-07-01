import styles from './DiffStructurePanel.module.css';
import type { DiffStructureItem } from '../utils/diffStructure';

interface Props {
  title: string;
  items: DiffStructureItem[];
  activeId?: string;
  onJump: (id: string) => void;
}

export default function DiffStructurePanel({ title, items, activeId, onJump }: Props) {
  const counts = items.reduce<Record<string, number>>((acc, item) => {
    acc[item.kind] = (acc[item.kind] || 0) + 1;
    return acc;
  }, {});

  return (
    <div className={styles.panel}>
      <div className={styles.header}>{title}</div>
      {items.length > 0 && (
        <div className={styles.summary}>
          {Object.entries(counts).map(([kind, count]) => (
            <span key={kind} className={styles.badge}>{kind} {count}</span>
          ))}
        </div>
      )}
      <div className={styles.list}>
        {items.length === 0 ? (
          <div className={styles.empty}>No structure markers found in this view.</div>
        ) : items.map(item => (
          <div
            key={item.id}
            className={`${styles.item} ${activeId === item.id ? styles.itemActive : ''}`}
            onClick={() => onJump(item.id)}
          >
            <span className={`${styles.kind} ${styles[`kind_${item.kind}`] || ''}`}>{item.kind}</span>
            <span className={styles.label}>{item.label}</span>
            <span className={styles.meta}>{item.lineLabel}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
