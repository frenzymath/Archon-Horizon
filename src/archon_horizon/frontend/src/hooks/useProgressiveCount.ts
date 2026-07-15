import { useEffect, useRef, useState } from 'react';

/**
 * Progressive rendering: paint the first `initial` items right away, then grow
 * the visible count toward `total` a `step` at a time on each animation frame.
 * A huge list (a long transcript, a big Lean file, a KaTeX-heavy chapter) shows
 * its first screenful immediately instead of blocking the main thread on the
 * whole render, so it *feels* like it starts loading the moment data arrives.
 *
 * `resetKey` restarts the ramp from `initial` — pass the identity of whatever is
 * being viewed (selected session / file / chapter) so switching targets starts
 * fresh. `total` growing on its own (e.g. a live-tailed transcript gaining new
 * events) does NOT reset the ramp: the count simply keeps climbing to include
 * the new items, so already-rendered rows are never torn down and re-mounted.
 *
 * For the growth to stay O(n) rather than O(n²), the rendered children must be
 * memoized (React.memo) so bumping the count doesn't re-render the rows already
 * on screen.
 */
export function useProgressiveCount(
  total: number,
  { initial = 60, step = 120, resetKey }: { initial?: number; step?: number; resetKey?: unknown } = {},
): number {
  const [count, setCount] = useState(() => Math.min(initial, total));
  const initialRef = useRef(initial);
  initialRef.current = initial;

  // Restart the ramp only when the viewed target changes — never merely because
  // `total` grew (that would re-collapse a live-tailing transcript every poll).
  useEffect(() => {
    setCount(Math.min(initialRef.current, total));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resetKey]);

  useEffect(() => {
    if (count >= total) return;
    const raf = requestAnimationFrame(() => setCount((c) => Math.min(total, c + step)));
    return () => cancelAnimationFrame(raf);
  }, [count, total, step]);

  return Math.min(count, total);
}
