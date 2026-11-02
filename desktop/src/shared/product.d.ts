declare module '@product/identity' {
  export const product: Readonly<{
    flavor: 'normal' | 'windows-simulation';
    appId: string;
    productName: string;
    executableName: string;
    profileDirectoryName: string;
    storageIdentity: string | null;
  }>;
}

declare module '@product/capture' {
  export const CaptureSource: new () => import('../renderer/audio/recorder-contract').CaptureSource;
}

declare module '@product/capture-access' {
  export function prepareCaptureAccess(isCurrent: () => boolean): void | Promise<void>;
  export function microphoneInfo(): import('./types').AppInfo['microphone'];
  export function openMicrophoneSettings(): Promise<void>;
}

declare module '@product/service' {
  export function startProductService(directory: string): Promise<{
    endpoint: import('../main/core/service-endpoint').ServiceEndpoint;
    close(): Promise<void>;
  }>;
}

declare module '@product/presentation' {
  export const presentation: Readonly<{
    banner: string | null;
    hudLabel: string | null;
    authDescription: string | null;
    accountHint: string | null;
    accountPlaceholder: string | null;
    serviceLabel: string | null;
    serviceAddress: string | null;
    serviceHint: string | null;
    microphoneHint: string | null;
  }>;
}
