import { contextBridge, ipcRenderer, type IpcRendererEvent } from 'electron';
import type { ElectronAPI } from './api';

// A thin, typed bridge: renderer code never touches ipcRenderer directly and
// never receives access or refresh tokens.
const api: ElectronAPI = {
  invoke: (channel, ...args) => ipcRenderer.invoke(channel, ...args),
  eventSend: (channel, data) => ipcRenderer.send(channel, data),
  eventOn: (channel, cb) => {
    const wrapped = (_: IpcRendererEvent, data: unknown) => cb(data);
    ipcRenderer.on(channel, wrapped);
    return () => ipcRenderer.removeListener(channel, wrapped);
  },
};

contextBridge.exposeInMainWorld('electronAPI', api);
