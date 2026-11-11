/** Durable SDK entry, sharing the exact host/worker/runtime used by the TUI. */
import { ApplicationSession } from '../../source/apps/agent/src/sdk/index.ts';
import { prepareApplication } from '../../source/apps/agent/src/application/host.mjs';

const [task, profile, output] = process.argv.slice(2);
if (!task || !profile || !output) throw new Error('Usage: node application.mjs ABS_TASK ABS_PROFILE ABS_OUTPUT');
const client = await prepareApplication({ task, profile, output, sessionOnly: false });
const session = new ApplicationSession(client);
try {
  await session.observe();
  session.subscribe(event => {
    if (event.type === 'model_delta') process.stdout.write(event.delta.delta ?? '');
    if (event.type === 'state') process.stderr.write(`\rGoal ${event.state.goal?.status ?? 'session'} · credits ${event.state.budget.spent}/${event.state.budget.limit}   `);
  });
  const result = await session.run();
  process.stdout.write('\n' + JSON.stringify({ application_session_id: session.applicationSessionId,
    goal: result.goal, best_accepted: result.best, single_best: result.single_best, platform: result.platform }, null, 2) + '\n');
} finally { session.dispose(); }
