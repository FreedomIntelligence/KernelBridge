import { TextDecoder } from "node:util";
import { forkCheckpoint } from "./session-fork.ts";

try {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of process.stdin) {
    size += chunk.length;
    if (size > 512 * 1024) throw new Error("Fork request exceeds its byte bound");
    chunks.push(Buffer.from(chunk));
  }
  const request = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks)));
  process.stdout.write(JSON.stringify(await forkCheckpoint(request)) + "\n");
} catch {
  process.stderr.write("Native fork failed; reconcile its original effect before continuing.\n");
  process.exitCode = 2;
}
