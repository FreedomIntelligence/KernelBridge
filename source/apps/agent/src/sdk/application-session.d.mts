export interface ApplicationOptions {
  readonly task: string;
  readonly config: string;
  readonly output: string;
  readonly python: string;
  readonly expectedPythonModules: string;
  readonly applicationSessionId?: string;
  readonly node?: string;
  /** Defaults to true. Set false explicitly to start a persistent Goal. */
  readonly sessionOnly?: boolean;
  readonly onDetach?: 'pause' | 'continue';
  readonly maxTrials?: number;
  readonly maxModelTurns?: number;
  readonly maxForks?: number;
  readonly forkPolicy?: { enabled: boolean; minimum_observations: number; cooldown_turns: number };
}
export interface ApplicationInputOptions { inputId?: string; }
export class ApplicationSession {
  readonly applicationSessionId: string;
  readonly capabilities: Readonly<{ events: boolean; tokenStreaming: boolean; nativeFork: boolean; persistentNativeSession: boolean }>;
  static create(options: ApplicationOptions): Promise<ApplicationSession>;
  static attach(identity: string, options?: { restart?: boolean }): Promise<ApplicationSession>;
  state(): Promise<Record<string, any>>;
  subscribe(listener: (event: Record<string, any>) => void): () => void;
  observe(after?: number): Promise<Record<string, any>>;
  prompt(text: string, options?: ApplicationInputOptions): Promise<Record<string, any>>;
  steer(text: string, options?: ApplicationInputOptions): Promise<Record<string, any>>;
  followUp(text: string, options?: ApplicationInputOptions): Promise<Record<string, any>>;
  pause(): Promise<Record<string, any>>;
  resume(): Promise<Record<string, any>>;
  interrupt(): Promise<Record<string, any>>;
  clearGoal(): Promise<Record<string, any>>;
  setGoal(objective: string, options?: { acceptance?: { evidence_tier: 'E0' | 'E1' | 'E2' | 'E3' | 'E4'; target?: number | null } }): Promise<Record<string, any>>;
  onDetach(value: 'pause' | 'continue'): Promise<Record<string, any>>;
  command(text: string, options?: ApplicationInputOptions): Promise<Record<string, any>>;
  fork(entryId?: string): Promise<Record<string, any>>;
  run(): Promise<Record<string, any>>;
  events(after?: number): AsyncIterable<Record<string, any>>;
  dispose(): void;
}
