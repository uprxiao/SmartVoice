const normal = Object.freeze({
  flavor: 'normal',
  appId: 'com.smartvoiceinput.desktop',
  productName: 'SmartVoiceInput',
  executableName: 'SmartVoiceInput',
  profileDirectoryName: 'SmartVoiceInput',
  storageIdentity: null,
});

const simulation = Object.freeze({
  flavor: 'windows-simulation',
  appId: 'com.smartvoiceinput.desktop.simulation',
  productName: 'SmartVoiceInput Simulation',
  executableName: 'SmartVoiceInput Simulation',
  profileDirectoryName: 'SmartVoiceInput Simulation',
  storageIdentity: 'smartvoiceinput:windows-simulation:v1',
});

module.exports = Object.freeze({ normal, simulation });
