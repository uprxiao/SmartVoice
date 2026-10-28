import { resolve } from 'node:path';
import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import { defineConfig, externalizeDepsPlugin } from 'electron-vite';

const alias = {
  '@root': resolve('.'),
  '@shared': resolve('src/shared'),
  '@main': resolve('src/main'),
  '@renderer': resolve('src/renderer'),
};

const apiBase = (process.env.VITE_API_BASE_URL ?? 'http://127.0.0.1:17863').replace(
  /\/+$/,
  ''
);
const apiWs = apiBase.replace(/^http/, 'ws');

const cspApiOrigin = {
  name: 'csp-api-origin',
  transformIndexHtml(html: string) {
    return html.replaceAll('%API_ORIGIN%', `${apiBase} ${apiWs}`);
  },
};

export default defineConfig({
  main: {
    plugins: [externalizeDepsPlugin()],
    resolve: { alias },
    build: { rollupOptions: { input: { index: resolve('src/main/index.ts') } } },
  },
  preload: {
    plugins: [externalizeDepsPlugin()],
    resolve: { alias },
    build: { rollupOptions: { input: { index: resolve('src/preload/index.ts') } } },
  },
  renderer: {
    root: 'src/renderer',
    plugins: [react(), tailwindcss(), cspApiOrigin],
    resolve: { alias },
    build: {
      rollupOptions: {
        input: {
          index: resolve('src/renderer/index.html'),
          hud: resolve('src/renderer/hud.html'),
        },
      },
    },
  },
});
