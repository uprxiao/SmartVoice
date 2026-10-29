const path = require('node:path');

module.exports = {
  // Offline packaging uses the Electron distribution already installed in
  // node_modules instead of re-downloading from GitHub.
  ...(process.env.SVI_ELECTRON_DIST ? { electronDist: path.resolve(process.env.SVI_ELECTRON_DIST) } : {}),
  appId: 'com.smartvoiceinput.desktop',
  productName: 'SmartVoiceInput',
  directories: { output: 'release', buildResources: 'build' },
  files: ['out/**/*', 'package.json'],
  extraResources: [
    { from: 'resources/service-ca.crt', to: 'service-ca.crt' },
    { from: 'resources/icon.png', to: 'icon.png' },
  ],
  artifactName: '${productName}-${version}-${os}-${arch}.${ext}',
  publish: null,
  afterPack: require('./scripts/prepare-mac-signing.cjs'),
  mac: {
    extraResources: [{ from: 'resources/voice/smartvoiceinput-input', to: 'voice/smartvoiceinput-input' }],
    icon: 'build/icon.icns',
    category: 'public.app-category.productivity',
    target: ['dmg'],
    // Local previews still need a bound application identity for TCC/icons.
    // Ad-hoc is not Developer ID signing or notarization.
    identity: process.env.CSC_LINK ? undefined : '-',
    hardenedRuntime: Boolean(process.env.CSC_LINK),
    notarize: Boolean(process.env.APPLE_ID),
    gatekeeperAssess: false,
  },
  dmg: {
    title: '${productName} ${version}',
  },
  win: {
    extraResources: [{ from: 'resources/voice/smartvoiceinput-input.exe', to: 'voice/smartvoiceinput-input.exe' }],
    icon: 'build/icon.ico',
    target: ['nsis'],
  },
  nsis: {
    oneClick: false,
    allowToChangeInstallationDirectory: true,
    perMachine: false,
    createDesktopShortcut: true,
    createStartMenuShortcut: true,
  },
  linux: {
    icon: 'build/icon.png',
    target: ['AppImage'],
    category: 'Utility',
  },
};
