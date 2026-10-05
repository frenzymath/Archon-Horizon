import { lazy, Suspense, type ReactNode } from "react";
import { ChevronLeft, ChevronRight, Copy, ExternalLink } from "lucide-react";
import type { Command, Run } from "./api";

export type ViewProps = {
  accountId: string;
  projectId: string;
  run: Run | undefined;
  writable: boolean;
  command: (value: Command) => Promise<boolean>;
  search: string;
};

const Markdown = lazy(() => import("../components/BlueprintMarkdown"));
export function RichText({ children }: { children: string }) {
  return (
    <Suspense fallback={<p>{children}</p>}>
      <Markdown content={children} />
    </Suspense>
  );
}
export function State({ value }: { value: string }) {
  return (
    <span className={`pl-state pl-state-${value}`}>
      {value.replace(/_/g, " ")}
    </span>
  );
}
export function Time({ value }: { value?: string | null }) {
  return value ? (
    <time
      dateTime={value}
      title={new Date(value).toLocaleString(undefined, {
        timeZoneName: "long",
      })}
    >
      {new Date(value).toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      })}
    </time>
  ) : (
    <span className="pl-muted">Not observed</span>
  );
}
export function IconButton({
  title,
  children,
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & {
  title: string;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      className="pl-icon"
      title={title}
      aria-label={title}
      {...props}
    >
      {children}
    </button>
  );
}
export function CopyRef({ value }: { value: string }) {
  return (
    <IconButton
      title={`Copy ${value}`}
      onClick={() => void navigator.clipboard.writeText(value)}
    >
      <Copy size={14} />
    </IconButton>
  );
}
export function External({
  url,
  children,
}: {
  url?: string | null;
  children: ReactNode;
}) {
  const safe = url && /^(https?:\/\/|\/[^/])/.test(url);
  return safe ? (
    <a href={url} target="_blank" rel="noopener noreferrer">
      {children}
      <ExternalLink size={13} />
    </a>
  ) : (
    <span>{children}</span>
  );
}
export function Empty({ children }: { children: ReactNode }) {
  return <p className="pl-empty">{children}</p>;
}
export function ErrorNotice({
  error,
  stale,
}: {
  error: Error | null;
  stale: boolean;
}) {
  return error ? (
    <p role="alert" className="pl-error">
      {stale ? "Showing last received data. " : ""}
      {error.message}
    </p>
  ) : null;
}
export function Pager({
  next,
  previous,
  onNext,
  onPrevious,
}: {
  next?: string | null;
  previous: boolean;
  onNext: (cursor: string) => void;
  onPrevious: () => void;
}) {
  return (
    <div className="pl-pager">
      <IconButton
        title="Previous page"
        disabled={!previous}
        onClick={onPrevious}
      >
        <ChevronLeft size={16} />
      </IconButton>
      <IconButton
        title="Next page"
        disabled={!next}
        onClick={() => next && onNext(next)}
      >
        <ChevronRight size={16} />
      </IconButton>
    </div>
  );
}
export function Json({ value }: { value: unknown }) {
  return <pre className="pl-json">{JSON.stringify(value, null, 2)}</pre>;
}
