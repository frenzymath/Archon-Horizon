import React from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter, HashRouter } from 'react-router-dom';
import { App } from './App';
import { installStaticFetch, isStaticDashboard } from './staticMode';
import './styles.css';

installStaticFetch(); // must run before any fetch

const Router = isStaticDashboard() ? HashRouter : BrowserRouter;

createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <Router>
      <App />
    </Router>
  </React.StrictMode>,
);
