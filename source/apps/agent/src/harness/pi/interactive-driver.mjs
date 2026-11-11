import { loadInteractiveMode } from './interactive-patch.mjs';

export async function runInteractive(governed, actions) {
  if (!process.stdin.isTTY || !process.stdout.isTTY) throw new Error('Interactive mode requires a real TTY; use the SDK for headless operation');
  const { InteractiveMode } = await loadInteractiveMode();
  await actions.bind(governed, { interactive: true });
  // The versioned adapter replays durable inputs only after UI initialization.
  // regular: inline rendering with terminal scrollback (no app-level scroll
  // capture); fullscreen: Pi's alternate screen with in-app scrolling.
  const requested = process.env.KERNELBRIDGE_TUI_MODE;
  const tuiMode = requested === 'regular' || requested === 'fullscreen' ? requested : 'regular';
  const mode = new InteractiveMode(governed.runtime, { hostActions: actions, tuiMode });
  await mode.run();
}
