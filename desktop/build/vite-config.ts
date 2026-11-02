import { resolve } from 'node:path';
import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import { defineConfig, externalizeDepsPlugin } from 'electron-vite';
import { moduleGraphPlugin, productAliases, productIdentityPlugin, type ProductFlavor } from './product-config';

export function desktopConfig(flavor: ProductFlavor) {
  const simulation = flavor === 'windows-simulation';
  const output = simulation ? 'out-simulation' : 'out';
  const alias = productAliases(flavor);
  const apiBase = (process.env.VITE_API_BASE_URL ?? 'http://127.0.0.1:17863').replace(/\/+$/, '');
  const csp = {
    name: 'csp-api-origin',
    transformIndexHtml(html: string) {
      return html.replaceAll('%API_ORIGIN%', simulation ? '' : `${apiBase} ${apiBase.replace(/^http/, 'ws')}`);
    },
  };
  return defineConfig({
    main: {
      plugins: [externalizeDepsPlugin(), productIdentityPlugin(flavor), moduleGraphPlugin()],
      resolve: { alias },
      build: { outDir: resolve(output, 'main'), rollupOptions: { input: { index: resolve('src/main/index.ts') } } },
    },
    preload: {
      plugins: [externalizeDepsPlugin(), productIdentityPlugin(flavor), moduleGraphPlugin()],
      resolve: { alias },
      build: { outDir: resolve(output, 'preload'), rollupOptions: { input: { index: resolve('src/preload/index.ts') } } },
    },
    renderer: {
      root: 'src/renderer',
      ...(simulation ? { publicDir: false as const } : {}),
      plugins: [react(), tailwindcss(), csp, productIdentityPlugin(flavor), moduleGraphPlugin()],
      resolve: { alias },
      build: {
        outDir: resolve(output, 'renderer'),
        rollupOptions: { input: { index: resolve('src/renderer/index.html'), hud: resolve('src/renderer/hud.html') } },
      },
    },
  });
}
