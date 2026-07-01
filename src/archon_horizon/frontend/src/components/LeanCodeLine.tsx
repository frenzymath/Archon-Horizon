import { useMemo } from 'react';
import { highlightLeanLine, type HighlightToken } from '../utils/leanHighlight';
import styles from './LeanCodeLine.module.css';

export default function LeanCodeLine({ text, tokens }: { text: string; tokens?: HighlightToken[] }) {
  const computedTokens = useMemo(() => tokens ?? highlightLeanLine(text), [text, tokens]);
  return (
    <span>
      {computedTokens.map((token, i) => (
        <span key={i} className={token.cls ? styles[token.cls] : undefined}>{token.text}</span>
      ))}
    </span>
  );
}
