import { defineConfig } from 'vitest/config';
import { productAliases, productIdentityPlugin } from './build/product-config';

export default defineConfig({
  plugins: [productIdentityPlugin('normal')],
  resolve: { alias: productAliases('normal') },
  test: { include: ['src/**/*.test.ts'], environment: 'node' },
});
