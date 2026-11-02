const base = require('./electron-builder.config.cjs');
const { simulation } = require('./build/products.cjs');

module.exports = {
  ...base,
  appId: simulation.appId,
  productName: simulation.productName,
  directories: { ...base.directories, output: 'release-simulation' },
  files: ['out-simulation/**/*', 'package.json'],
  extraMetadata: {
    name: 'smartvoiceinput-windows-simulation',
    main: 'out-simulation/main/index.js',
    buildFlavor: simulation.flavor,
  },
  extraResources: [{ from: 'resources/icon.png', to: 'icon.png' }],
  artifactName: 'SmartVoiceInput-Simulation-${version}-win-${arch}.${ext}',
  win: { ...base.win, executableName: simulation.executableName },
};
