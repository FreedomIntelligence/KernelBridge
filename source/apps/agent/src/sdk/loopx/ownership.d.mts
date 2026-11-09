export function claimGoalOwner(runtimeRoot: string, goalId: string, binding: Readonly<Record<string, unknown>>): Promise<string>;

export function claimApplicationGoal(runtimeRoot: string, applicationSessionId: string, goalId: string): Promise<void>;
export function assertApplicationGoal(runtimeRoot: string, applicationSessionId: string, goalId: string): Promise<void>;
export function clearApplicationGoal(runtimeRoot: string, applicationSessionId: string, goalId: string): Promise<void>;
