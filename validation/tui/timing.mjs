// Node counterpart of timing.py: scenario deadlines scale with KB_TIMEOUT_SCALE,
// and an expired wait throws StepTimeout so the runner can tell it from a failure.
export const SCALE = Math.max(1, Number(process.env.KB_TIMEOUT_SCALE) || 1);
export class StepTimeout extends Error {
  constructor(message) { super(message); this.name = 'StepTimeout'; }
}
export const scaled = ms => Math.round(ms * SCALE);
