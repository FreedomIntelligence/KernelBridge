// Newline-delimited JSON frames. Kept free of the payload schemas so the CLI's
// application client does not compile the RPC validators on every start.
import { TextDecoder } from 'node:util';

export const DEFAULT_FRAME_BYTES = 1048576;
export function validateFrameLimit(limit) {
  if (!Number.isSafeInteger(limit) || limit < 4096 || limit > 16777216) {
    throw new Error('Invalid frame limit');
  }
}
export async function* frames(input, limit = DEFAULT_FRAME_BYTES) {
  validateFrameLimit(limit);
  let pending = Buffer.alloc(0);
  const decoder = new TextDecoder('utf-8', {fatal:true});
  for await (const chunk of input) {
    pending = Buffer.concat([pending, Buffer.from(chunk)]);
    for (;;) {
      const end = pending.indexOf(10);
      if (end < 0) break;
      if (end > limit) throw new Error('Response exceeds frame limit');
      const value = JSON.parse(decoder.decode(pending.subarray(0,end)));
      pending = pending.subarray(end+1);
      yield value;
    }
    if (pending.length > limit) throw new Error('Response exceeds frame limit');
  }
  if (pending.length) throw new Error('Truncated protocol frame');
}
