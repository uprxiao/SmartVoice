import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  base: './',
  plugins: [react()],
  server: {
    port: 17864,
    proxy: {
      '/api': { target: 'http://127.0.0.1:17863' },
      '/health': { target: 'http://127.0.0.1:17863' },
    },
  },
});
