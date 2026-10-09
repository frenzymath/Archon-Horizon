import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { fileURLToPath } from 'node:url';

// Relative asset URLs keep this standalone artifact valid below any Pages path.
export default defineConfig({
  root: fileURLToPath(new URL('./showcase', import.meta.url)),
  base: './',
  publicDir: fileURLToPath(new URL('./showcase/public', import.meta.url)),
  plugins: [react()],
  build: { outDir: '../build/dashboard-demo', emptyOutDir: true },
});
