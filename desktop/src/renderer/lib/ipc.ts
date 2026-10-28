import type { RpcRouter } from '@main/rpc';
import { createEventEmitter } from '@shared/ipc/events';
import { createRPCClient } from '@shared/ipc/rpc';

export const rpc = createRPCClient<RpcRouter>((channel, ...args) =>
  window.electronAPI.invoke(channel, ...args)
);

export const events = createEventEmitter({
  emit: (channel, data) => window.electronAPI.eventSend(channel, data),
  on: (channel, cb) => window.electronAPI.eventOn(channel, cb),
});
