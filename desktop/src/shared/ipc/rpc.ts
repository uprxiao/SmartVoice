// Ported from Yoda (Apache-2.0) src/shared/ipc/rpc.ts; trimmed to what SmartDD uses.
import type { IpcMain } from 'electron';

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type ProcedureMap = Record<string, (...args: any[]) => unknown>;
type RouterMap = Record<string, ProcedureMap>;

export function createRPCController<T extends ProcedureMap>(handlers: T): T {
  return handlers;
}

export function createRPCRouter<T extends RouterMap>(routers: T): T {
  return routers;
}

/** Each procedure becomes an `ipcMain.handle` channel named `<namespace>.<procedure>`. */
export function registerRPCRouter(router: RouterMap, ipcMain: IpcMain, authorize?: (event: Electron.IpcMainInvokeEvent, channel: string) => boolean): void {
  for (const [ns, handlers] of Object.entries(router)) {
    for (const [key, fn] of Object.entries(handlers)) {
      const channel = `${ns}.${key}`;
      ipcMain.handle(channel, (event, ...args: unknown[]) => {
        if (authorize && !authorize(event, channel)) throw new Error('Untrusted IPC sender');
        return fn(...args);
      });
    }
  }
}

export type IpcClient<R extends RouterMap> = {
  [NS in keyof R]: {
    [P in keyof R[NS]]: R[NS][P] extends (...args: infer A) => infer Ret
      ? (...args: A) => Promise<Awaited<Ret>>
      : never;
  };
};

export function createRPCClient<Router extends RouterMap>(
  invoke: (channel: string, ...args: unknown[]) => Promise<unknown>
): IpcClient<Router> {
  return new Proxy(
    {},
    {
      get(_, ns) {
        // 'then' must stay undefined or `await client` would treat the proxy as a thenable.
        if (typeof ns !== 'string' || ns === 'then') return undefined;
        return new Proxy(
          {},
          {
            get(_, procedure) {
              if (typeof procedure !== 'string' || procedure === 'then') return undefined;
              return (...args: unknown[]) => invoke(`${ns}.${procedure}`, ...args);
            },
          }
        );
      },
    }
  ) as IpcClient<Router>;
}

/**
 * Electron flattens a thrown Error to "Error invoking remote method '<channel>': Error: <message>".
 * Strip that prefix before showing it to the user.
 */
export function rpcErrorMessage(error: unknown): string {
  const raw = error instanceof Error ? error.message : String(error);
  return raw.replace(/^Error invoking remote method '[^']*': (?:\w*Error: )?/, '');
}
