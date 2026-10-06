import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  // Release activation retains previous hashed assets separately for open tabs.
  build: { outDir: 'dist', emptyOutDir: true },
});
