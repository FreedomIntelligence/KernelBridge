/** Resolve the frozen Goal candidate by content identity, independently of workspace drafts. */
import { createHash } from 'node:crypto';

export const candidateHash = bytes => createHash('sha256').update(bytes).digest('hex');

export function materializedCandidate(files, state) {
  const reference = state?.materialization?.result?.manifest_ref;
  if (!reference) return null;
  const artifact = [...files].find(([, bytes]) => bytes.length === reference.byte_length
    && 'sha256:' + candidateHash(bytes) === reference.digest);
  if (!artifact) throw new Error('Materialized candidate bytes are missing or altered');
  const program = JSON.parse(artifact[1]);
  const sources = program.files?.filter(file => file.path === 'solution.py');
  if (sources?.length !== 1 || typeof sources[0].text !== 'string') {
    throw new Error('Expected one materialized solution.py');
  }
  const bytes = Buffer.from(sources[0].text);
  return { bytes, identity: { sha256: candidateHash(bytes), bytes: bytes.length,
    basis: 'sdk/state.json#materialization.result.manifest_ref',
    program_digest: reference.digest, program_path: artifact[0] } };
}
