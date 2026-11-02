import { app } from 'electron';
import { join } from 'node:path';
import { product } from '@product/identity';
import { configureWindowsAppIdentity } from '../../../native/system-settings';

export function configureProductProfile(): void {
  app.setName(product.productName);
  configureWindowsAppIdentity();
  if (product.flavor === 'normal') {
    if (process.env.SVI_USER_DATA) app.setPath('userData', process.env.SVI_USER_DATA);
    return;
  }
  const directory = join(app.getPath('appData'), product.profileDirectoryName);
  app.setPath('userData', directory);
  app.setPath('sessionData', directory);
}
