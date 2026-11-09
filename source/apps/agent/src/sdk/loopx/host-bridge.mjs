import { connect } from "node:net";

// A one-shot authenticated local transport. It has no model, provider or worker configuration.
const [socketPath, token] = process.argv.slice(2);
let input = "";
for await (const chunk of process.stdin) {
  input += chunk;
  if (Buffer.byteLength(input) > 1_048_576) throw new Error("Host request exceeds bound");
}
const request = JSON.parse(input);
const socket = connect(socketPath);
socket.setTimeout(3_600_000, () => socket.destroy(new Error("Host bridge timeout")));
socket.on("connect", () => socket.write(JSON.stringify({ token, request }) + "\n"));
let output = "";
socket.setEncoding("utf8");
socket.on("data", chunk => {
  output += chunk;
  if (Buffer.byteLength(output) > 1_048_576) socket.destroy(new Error("Host result exceeds bound"));
});
socket.on("error", () => { process.exitCode = 2; });
socket.on("end", () => {
  const value = JSON.parse(output);
  if (value.error) { process.exitCode = 2; return; }
  process.stdout.write(JSON.stringify(value) + "\n");
});
