export type Phase = {kind: string};

export function phaseLabel(phase?: Phase | null): string {
  switch (phase?.kind) {
    case "preprocessing": return "Pre-processing";
    case "formalization": return "Main formalization";
    case "postprocessing": return "Post-processing";
    default: return phase?.kind ? phase.kind.replace(/_/g, " ") : "Not recorded";
  }
}

export default function RunPhase({phase}: {phase?: Phase | null}) {
  return <span className="run-phase" aria-label={`Phase: ${phaseLabel(phase)}`}>
    <span>Phase: </span><strong>{phaseLabel(phase)}</strong>
  </span>;
}
