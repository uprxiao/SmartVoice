export interface RecorderCallbacks {
  onChunk: (buffer: ArrayBuffer, capturedAtEpochMs: number) => void;
  onLevel: (level: number) => void;
  onLimit?: () => void;
  onError?: (error: Error) => void;
}

export class RecorderError extends Error {
  constructor(readonly code: string, message: string) { super(message); }
}

export interface CaptureSource {
  readonly active: boolean;
  start(callbacks: RecorderCallbacks, options?: { maxSamples?: number }): Promise<void>;
  acknowledge(totalSamples: number): void;
  stop(options?: { discard?: boolean }): Promise<void>;
}
