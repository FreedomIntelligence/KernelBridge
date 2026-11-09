/** Events are observations of one invocation, never permission to perform an effect. */
export interface SessionEvent {
  readonly applicationSessionId: string;
  readonly invocationId: string;
  readonly sequence: number;
  readonly type: "started" | "ready" | "runtime" | "harness" | "input" | "interrupted" | "completed" | "failed" | "observer_error";
  readonly payload: unknown;
}

export type SessionListener = (event: SessionEvent) => void;

/** A bounded observer queue. Overflow fails this stream, never retries the run. */
export class SessionEventStream implements AsyncIterable<SessionEvent> {
  private readonly queue: SessionEvent[] = [];
  private wake: (() => void) | undefined;
  private ended = false;
  private failure: Error | undefined;
  private claimed = false;

  push(event: SessionEvent): void {
    if (this.ended) return;
    if (this.queue.length >= 1024) {
      this.failure = new Error("SDK event consumer exceeded its 1024-event buffer; use the run result and retained receipts");
      this.close();
      return;
    }
    this.queue.push(structuredClone(event));
    this.wake?.();
  }

  close(): void {
    this.ended = true;
    this.wake?.();
  }

  async *[Symbol.asyncIterator](): AsyncIterator<SessionEvent> {
    if (this.claimed) throw new Error("An SDK event stream has one consumer; use subscribe for additional observers");
    this.claimed = true;
    try {
      while (true) {
        if (this.failure) throw this.failure;
        const event = this.queue.shift();
        if (event !== undefined) yield event;
        else if (this.ended) return;
        else await new Promise<void>(resolve => { this.wake = resolve; });
      }
    } finally {
      this.close();
      this.queue.length = 0;
      this.wake = undefined;
    }
  }
}
