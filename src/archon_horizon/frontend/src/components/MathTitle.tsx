import { lazy, memo, Suspense } from "react";

const MathTitleFormula = lazy(() => import("./MathTitleFormula"));

export default memo(function MathTitle({ title, metadata = {} }: { title: string; metadata?: Record<string, unknown> }) {
  if (!title.includes("$")) return <>{title}</>;
  return <Suspense fallback={title}><MathTitleFormula title={title} metadata={metadata} /></Suspense>;
});
