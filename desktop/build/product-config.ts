import { createRequire } from 'node:module';
import { isAbsolute, relative, resolve } from 'node:path';
import type { Plugin } from 'vite';

export type ProductFlavor = 'normal' | 'windows-simulation';
const require = createRequire(import.meta.url);
const catalog = require(resolve('build/products.cjs')) as { normal: Record<string, unknown>; simulation: Record<string, unknown> };

export function productIdentityPlugin(flavor: ProductFlavor): Plugin {
  const selected = flavor === 'normal' ? catalog.normal : catalog.simulation;
  return {
    name: 'product-identity',
    resolveId(id) { if (id === '@product/identity') return '\0smartvoice-product-identity'; },
    load(id) {
      if (id === '\0smartvoice-product-identity') return `export const product = Object.freeze(${JSON.stringify(selected)});`;
    },
  };
}

export function productAliases(flavor: ProductFlavor) {
  const simulation = flavor === 'windows-simulation';
  return {
    '@root': resolve('.'), '@shared': resolve('src/shared'),
    '@main': resolve('src/main'), '@renderer': resolve('src/renderer'),
    '@product/capture': resolve(simulation ? 'src/renderer/simulation/synthetic-capture.ts' : 'src/renderer/product/normal-capture.ts'),
    '@product/capture-access': resolve(simulation ? 'src/main/simulation/capture-access.ts' : 'src/main/product/normal-capture-access.ts'),
    '@product/service': resolve(simulation ? 'src/main/simulation/service.ts' : 'src/main/product/normal-service.ts'),
    '@product/presentation': resolve(simulation ? 'src/renderer/simulation/presentation.ts' : 'src/renderer/product/normal-presentation.ts'),
  };
}

export function moduleGraphPlugin(): Plugin {
  return {
    name: 'relative-build-module-graph',
    generateBundle() {
      const modules = [...this.getModuleIds()].flatMap(id => {
        const clean = id.split('?')[0];
        if (!isAbsolute(clean) || clean.replaceAll('\\', '/').includes('/node_modules/')) return [];
        const path = relative(resolve('.'), clean).replaceAll('\\', '/');
        return path.startsWith('../') ? [] : [path];
      }).sort();
      this.emitFile({ type: 'asset', fileName: 'build-modules.json', source: JSON.stringify({ modules }, null, 2) + '\n' });
    },
  };
}
