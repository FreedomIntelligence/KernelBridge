import { constants } from "node:fs";
import { open } from "node:fs/promises";
import { isAbsolute } from "node:path";
import type { PrepareTrialRequest } from "../generated/runtime.ts";
import { SCHEMA as TASK_SCHEMA } from "../generated/task-contract.ts";
import { runtimeValue } from "../clients/runtime.ts";
import { wireValidator } from "../clients/wire-schemas.ts";
import type { LoadedManifest } from "./launch-manifest.ts";

export const MAX_TRIAL_REQUEST_BYTES = 16 * 1024 * 1024;
const objectText = wireValidator<string>(TASK_SCHEMA.$id, "Text");

export function validateTrialRequest(value: unknown, loaded: LoadedManifest): PrepareTrialRequest {
  const request = runtimeValue("PrepareTrialRequest", value);
  if (request.manifest_digest !== loaded.digest || request.expected.run_id !== loaded.manifest.run_id ||
      request.expected.account_id !== loaded.manifest.account_id) {
    throw new Error("Next Trial request differs from the original manifest Run/account/digest");
  }
  if (request.trial_credit_limit > loaded.manifest.run_credit_limit) throw new Error("Trial credit limit exceeds Run credit limit");
  const proposal = request.proposal;
  for (const text of [proposal.conditions, proposal.operation, proposal.expected_outcome, ...proposal.stop_conditions, proposal.parameter_source]) {
    if (/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(text)) {
      throw new Error("Next Trial proposal contains an unpaired Unicode surrogate");
    }
  }
  if ([proposal.conditions, proposal.operation, proposal.expected_outcome, ...proposal.stop_conditions].some(text => !objectText(text))) {
    throw new Error("Next Trial proposal cannot fit the generated domain record contract");
  }
  return request;
}

/** Open one bounded regular file; a symlink/FIFO never starts the finite Worker. */
export async function loadTrialRequest(path: string, loaded: LoadedManifest): Promise<PrepareTrialRequest> {
  if (!isAbsolute(path)) throw new Error("Next Trial request must be an absolute path");
  const handle = await open(path, constants.O_RDONLY | constants.O_NONBLOCK | constants.O_NOFOLLOW);
  try {
    if (!(await handle.stat()).isFile()) throw new Error("Next Trial request must be a regular file");
    const bytes = Buffer.alloc(MAX_TRIAL_REQUEST_BYTES + 1);
    let size = 0;
    while (size < bytes.length) {
      const read = await handle.read(bytes, size, bytes.length - size, null);
      if (read.bytesRead === 0) break;
      size += read.bytesRead;
    }
    if (size > MAX_TRIAL_REQUEST_BYTES) throw new Error("Next Trial request exceeds its byte limit");
    const text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bytes.subarray(0, size));
    return validateTrialRequest(JSON.parse(text), loaded);
  } finally {
    await handle.close();
  }
}
