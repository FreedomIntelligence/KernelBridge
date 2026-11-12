import type { ChildProcess } from 'node:child_process';

export interface WorkerStart {
  host: string;
  ticket: string;
  environment?: { KERNELBRIDGE_SESSIONS?: string; KERNELBRIDGE_TUI_MODE?: string };
}
export interface PrewarmedWorker {
  start(message: WorkerStart, beforeHandOver?: () => void): Promise<ChildProcess>;
  cancel(): void;
}
export interface StartupStatus { clear(): void; restore(): void }
export function startsInterface(args: readonly string[]): boolean;
export function prewarmWorker(): PrewarmedWorker;
export function startupStatus(): StartupStatus;
