/** One command replays the original experiment recipe through the current SDK. */
import { fileURLToPath } from 'node:url';
import { runCannTask } from '../run.mjs';
const args = process.argv.slice(2);
const options = { task: fileURLToPath(new URL('./task.json', import.meta.url)), mode: 'optimize' };
try {
  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--prepare-only') { options.prepareOnly = true; continue; }
    if (!['--profile', '--output'].includes(args[i]) || !args[i + 1]) throw new Error('node reproduce.mjs --profile ABS --output NEW_ABS [--prepare-only]');
    options[args[i].slice(2)] = args[++i];
  }
  console.log(JSON.stringify(await runCannTask(options)));
} catch (error) { console.error(error.message); process.exitCode = 1; }
