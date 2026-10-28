// Ported from Yoda (Apache-2.0) src/shared/ipc/events.ts.
type EventDefinition<TData> = {
  name: string;
  _data?: TData;
};

export function defineEvent<TData>(name: string): EventDefinition<TData> {
  return { name };
}

export type EmitterAdapter = {
  emit: (eventName: string, data: unknown) => void;
  on: (eventName: string, cb: (data: unknown) => void) => () => void;
};

export function createEventEmitter(adapter: EmitterAdapter) {
  // One adapter listener per channel; all JS subscribers fan out from it.
  const listeners = new Map<string, Set<(data: unknown) => void>>();
  const adapterOff = new Map<string, () => void>();

  function getOrAttach(channel: string): Set<(data: unknown) => void> {
    let set = listeners.get(channel);
    if (!set) {
      const created = new Set<(data: unknown) => void>();
      listeners.set(channel, created);
      adapterOff.set(
        channel,
        adapter.on(channel, (data) => {
          for (const cb of created) {
            try {
              cb(data);
            } catch (e) {
              console.error(`[events] listener for ${channel} threw`, e);
            }
          }
        })
      );
      set = created;
    }
    return set;
  }

  function maybePrune(channel: string): void {
    if (listeners.get(channel)?.size === 0) {
      listeners.delete(channel);
      adapterOff.get(channel)?.();
      adapterOff.delete(channel);
    }
  }

  return {
    emit: <TData>(event: EventDefinition<TData>, data: TData): void => {
      adapter.emit(event.name, data);
    },
    on: <TData>(event: EventDefinition<TData>, cb: (data: TData) => void): (() => void) => {
      const wrapped = (data: unknown) => cb(data as TData);
      getOrAttach(event.name).add(wrapped);
      return () => {
        listeners.get(event.name)?.delete(wrapped);
        maybePrune(event.name);
      };
    },
  };
}
