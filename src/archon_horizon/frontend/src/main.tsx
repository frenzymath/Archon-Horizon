import React, { lazy, Suspense } from 'react';
import { createRoot } from 'react-dom/client';
import './styles.css';

const Entry = lazy(() => import('./pipeline/PipelineApp'));

createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <Suspense fallback={<main className="platform-startup" role="status">Loading Archon Horizon...</main>}><Entry /></Suspense>
  </React.StrictMode>,
);
