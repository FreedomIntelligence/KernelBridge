import type { PrewarmedWorker, StartupStatus } from './prewarm.mjs';
export const HELP: string;
export function applicationMain(args: readonly string[], early?: { worker?: PrewarmedWorker; status?: StartupStatus }): Promise<number>;
