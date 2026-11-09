import { createServer, type Socket } from "node:net";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { randomBytes } from "node:crypto";
import { fileURLToPath } from "node:url";
import { object, type JsonObject } from "./client.ts";

export async function hostBridge<T>(node: string, execute: (request: JsonObject) => Promise<JsonObject>,
  invoke: (argv: readonly string[]) => Promise<T>): Promise<T> {
  const directory = await mkdtemp(join(tmpdir(), "kc-goal-"));
  const path = join(directory, "host.sock");
  const token = randomBytes(32).toString("hex");
  const sockets = new Set<Socket>();
  let admitted = false;
  let host: Promise<void> | undefined;
  const server = createServer(socket => {
    sockets.add(socket);
    socket.on("error", () => {});
    socket.on("close", () => sockets.delete(socket));
    let input = "";
    socket.setEncoding("utf8");
    socket.on("data", chunk => {
      input += chunk;
      if (Buffer.byteLength(input) > 1_048_576) { socket.destroy(); return; }
      if (!input.endsWith("\n")) return;
      socket.removeAllListeners("data");
      host = (async () => {
        try {
          const message = object(JSON.parse(input), "host bridge packet");
          if (message.token !== token || admitted) throw new Error("Host bridge identity conflict");
          admitted = true;
          const result = await execute(object(message.request, "host request"));
          socket.end(JSON.stringify(result));
        } catch { socket.end(JSON.stringify({ error: "Managed host refused; inspect original receipts" })); }
      })();
    });
  });
  try {
    await new Promise<void>((resolve, reject) => { server.once("error", reject); server.listen(path, resolve); });
    return await invoke([node, fileURLToPath(new URL("./host-bridge.mjs", import.meta.url)), path, token]);
  } finally {
    for (const socket of sockets) socket.destroy();
    await new Promise<void>(resolve => server.close(() => resolve()));
    await host;
    await rm(directory, { recursive: true, force: true });
  }
}
