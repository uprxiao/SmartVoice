import { product } from '@product/identity';
import { join } from 'node:path';
import { startSimulationHost } from './host';

export async function startProductService(directory: string) {
  if (!product.storageIdentity) throw new Error('模拟产品缺少稳定存储身份');
  return startSimulationHost({ directory: join(directory, 'simulation-service'), storageIdentity: product.storageIdentity });
}
