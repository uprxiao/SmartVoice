const path = require('node:path');
if (!process.env.SVI_PROBE_BUNDLE || !process.env.CARGO_TARGET_DIR) throw new Error('probe output and Cargo target directories required');
module.exports = {
  appId: 'com.smartvoiceinput.windows-probe',
  productName: 'SVI Windows Probe',
  electronVersion: require(path.resolve(__dirname, '../../../node_modules/electron/package.json')).version,
  directories: { app: path.join(__dirname, 'electron'), output: process.env.SVI_PROBE_BUNDLE },
  files: ['package.json', 'main.cjs', 'index.html'],
  asar: true,
  npmRebuild: false,
  extraResources: [{ from: path.join(process.env.CARGO_TARGET_DIR, 'release/smartvoiceinput-input.exe'), to: 'voice/smartvoiceinput-input.exe' }],
  win: { signAndEditExecutable: false },
  publish: null
};
