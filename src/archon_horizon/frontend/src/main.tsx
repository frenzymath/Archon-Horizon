import React from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './App';
import { installStaticFetch } from './staticMode';
import './styles.css';

installStaticFetch(); // must run before any fetch
createRoot(document.getElementById('root')!).render(<App />);
