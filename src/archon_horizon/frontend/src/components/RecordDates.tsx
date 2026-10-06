import "./recordDates.css";

type Item = Record<string, any>;

export default function RecordDates({ item }: { item: Item }) {
  const values = [["Created", item.created_at], ["Updated", item.updated_at || item.created_at]];
  return <div className="platform-record-dates">{values.map(([label, value]) => {
    const date = typeof value === "string" ? new Date(value) : null;
    if (!date || !Number.isFinite(date.getTime())) return null;
    return <span key={label}>{label} <time dateTime={value} title={date.toLocaleString()}>{date.toLocaleString([], { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}</time></span>;
  })}</div>;
}
