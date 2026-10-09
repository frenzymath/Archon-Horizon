import React, { lazy, Suspense } from 'react';
import { createRoot } from 'react-dom/client';
import { installDemoNavigation, installDemoTransport } from './transport';
import '../styles.css';
import './showcase.css';

installDemoTransport();
installDemoNavigation();
const Dashboard = lazy(() => import('../pipeline/PipelineApp'));

createRoot(document.getElementById('root')!).render(<React.StrictMode>
  <aside className="showcase-banner" aria-label="Demo notice"><strong>Synthetic demo · read-only</strong>
    <span>No account, agents, external integrations, or live project data.</span>
    <a href="../dashboard-demo/">About this demo</a>
  </aside>
  <div className="showcase-dashboard"><Suspense fallback={<main role="status">Loading dashboard demo…</main>}><Dashboard/></Suspense></div>
</React.StrictMode>);
