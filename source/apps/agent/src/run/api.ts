/** Pure product proposal rules. Importing this entry performs no effect or startup. */
export { proposeSearch, stageRouter } from "./router.ts";
export type { SearchState, SearchProposal, SearchPolicy } from "./router.ts";
export { planStateTransition } from "./transitions.ts";
export { score } from "./scoring/index.ts";
