import React from 'react';

/**
 * ProjectPicker — choose which project to view. Always a compact dropdown so it
 * scales to many projects and switching is deliberate (no row of click targets).
 */
export default function ProjectPicker({
  projects,
  value,
  onChange,
}: {
  projects: string[];
  value: string;
  onChange: (project: string) => void;
}) {
  if (projects.length === 0) return null;
  return (
    <label className="project-picker-select">
      <span>Project</span>
      <select value={value} onChange={(e) => onChange(e.target.value)} aria-label="Project">
        {projects.map((p) => (
          <option key={p} value={p}>{p}</option>
        ))}
      </select>
      {projects.length > 1 && <span className="project-picker-count">{projects.length}</span>}
    </label>
  );
}
