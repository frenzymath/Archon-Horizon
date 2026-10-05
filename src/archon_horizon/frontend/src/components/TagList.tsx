import { progressLabelPriority, progressLabelTitles } from "../formalizationGraph";
import "./blueprint.css";

const normalizeTag = (value: string) => value.trim().toLowerCase().replace(/\s+/g, "_");

function tagTone(tag: string): string {
  if (/\b(?:ok|pass|checked)\b/i.test(tag)) return "green";
  if (/\b(?:fail|failed|error)\b/i.test(tag)) return "red";
  if (/\b(?:sketch|pending|unknown)\b/i.test(tag)) return "amber";
  let hash = 0;
  for (const character of tag.toLowerCase()) hash = (hash * 31 + character.charCodeAt(0)) >>> 0;
  return ["green", "blue", "violet", "red", "amber", "neutral"][hash % 6];
}

export function isProgressLabel(tag: string): boolean {
  const normalized = normalizeTag(tag);
  return normalized === "milestone" || normalized in progressLabelTitles;
}

export function NodeProgressLabels({ labels, className = "" }: { labels: string[]; className?: string }) {
  const unique = new Map<string, string>();
  for (const raw of labels) {
    const normalized = normalizeTag(raw);
    if (!normalized || unique.has(normalized)) continue;
    unique.set(normalized, normalized);
  }
  const ordered = [...unique.keys()].sort((left, right) => {
    if (left === "milestone") return -1;
    if (right === "milestone") return 1;
    const leftRank = (progressLabelPriority as readonly string[]).indexOf(left);
    const rightRank = (progressLabelPriority as readonly string[]).indexOf(right);
    return (leftRank === -1 ? 99 : leftRank) - (rightRank === -1 ? 99 : rightRank);
  });
  if (!ordered.length) return null;
  return <div className={`platform-document-tags ${className}`}>{ordered.map((label) => {
    const title = label === "milestone" ? "Milestone" : (progressLabelTitles[label] || label.replace(/_/g, " "));
    const status = label === "formally_proved" ? "formalized" : label;
    return <span key={label} className={`platform-document-tag progress-${status}`}>{title}</span>;
  })}</div>;
}

export function TagList({ tags, className = "" }: { tags: string[]; className?: string }) {
  const unique = new Map<string, string>();
  for (const raw of tags) { const tag = raw.trim(); if (tag && !unique.has(tag.toLocaleLowerCase())) unique.set(tag.toLocaleLowerCase(), tag); }
  return <div className={`platform-document-tags ${className}`}>{[...unique.values()].map((tag) => <span key={tag} className={`platform-document-tag tone-${tagTone(tag)}`}>{tag}</span>)}</div>;
}

export default TagList;
