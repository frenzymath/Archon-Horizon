import { mathMacros } from "../utils/document";
import { mathTitleHtml } from "../utils/mathTitle";
import "katex/dist/katex.min.css";
import "./math-title.css";

export default function MathTitleFormula({ title, metadata }: { title: string; metadata: Record<string, unknown> }) {
  return <span className="platform-math-title" dangerouslySetInnerHTML={{ __html: mathTitleHtml(title, mathMacros(metadata)) }} />;
}
