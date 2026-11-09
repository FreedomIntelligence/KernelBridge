import { runtimeValue } from "../clients/runtime.ts";
import type { PrepareTrialRequest, ReplaceSessionRequest } from "../generated/runtime.ts";
import { MAX_CONTROL_BYTES, readBoundedUtf8 } from "../tasks/launch-manifest.ts";
import type { LoadedManifest } from "../tasks/launch-manifest.ts";
import { loadTrialRequest, validateTrialRequest } from "../tasks/trial-request.ts";

export interface RunOptions {
  readonly resume?: string | ReplaceSessionRequest;
  readonly nextTrial?: string | PrepareTrialRequest;
  readonly signal?: AbortSignal;
}

export async function runRequests(options: RunOptions, loaded: LoadedManifest): Promise<{
  readonly resume?: ReplaceSessionRequest;
  readonly nextTrial?: PrepareTrialRequest;
}> {
  if (options.resume !== undefined && options.nextTrial !== undefined) {
    throw new Error("Choose one explicit resume or next-Trial request");
  }
  const resume = options.resume === undefined ? undefined : runtimeValue("ReplaceSessionRequest",
    typeof options.resume === "string" ? JSON.parse(await readBoundedUtf8(options.resume, "Session replacement request", MAX_CONTROL_BYTES)) : options.resume);
  if (resume !== undefined && (resume.expected.run_id !== loaded.manifest.run_id ||
      resume.expected.account_id !== loaded.manifest.account_id)) {
    throw new Error("Session replacement request belongs to another manifest Run/account");
  }
  const nextTrial = options.nextTrial === undefined ? undefined : typeof options.nextTrial === "string" ?
    await loadTrialRequest(options.nextTrial, loaded) : validateTrialRequest(options.nextTrial, loaded);
  return { resume, nextTrial };
}
