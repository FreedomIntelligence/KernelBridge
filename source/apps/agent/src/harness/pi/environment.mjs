/** Serialize temporary process environment changes; nested calls share the same owner. */
import { AsyncLocalStorage } from 'node:async_hooks';

const owner = new AsyncLocalStorage();
let tail = Promise.resolve();

export async function withProcessEnvironment(environment, execute) {
  const run = async () => {
    const selected = typeof environment === 'function' ? environment() : environment;
    const previous = Object.fromEntries(Object.keys(selected).map(name => [name, process.env[name]]));
    try {
      for (const [name, value] of Object.entries(selected)) {
        if (value === undefined) delete process.env[name];
        else process.env[name] = value;
      }
      return await execute();
    } finally {
      for (const [name, value] of Object.entries(previous)) {
        if (value === undefined) delete process.env[name];
        else process.env[name] = value;
      }
    }
  };
  if (owner.getStore()) return run();
  const pending = tail.then(() => owner.run(true, run));
  tail = pending.then(() => undefined, () => undefined);
  return pending;
}
