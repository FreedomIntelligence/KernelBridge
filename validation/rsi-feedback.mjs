import test from 'node:test';
import assert from 'node:assert/strict';
import { discoveryFeedback } from '../source/apps/agent/src/rsi/feedback.ts';

test('discovery exposes usable diagnostics and scores without confirmation evidence', () => {
  const measurement = {measurement_id:'original',protocol_id:'fixture',sample_ids:['a','b'],
    keep_digest:'candidate',remove_digest:'baseline',keep_correct:false,remove_correct:true,
    keep_log_scores:[0,0],remove_log_scores:[2,4],independent_confirmation:false,
    features:{surface:[],lowered:null,runtime:['failure_stage:compile_error','diagnostic:unknown intrinsic']},
    remove_features:{surface:[],lowered:null,runtime:['correct']},evidence_ids:['private-raw-result']};
  const result = discoveryFeedback(measurement,'unit');
  assert.equal(result.keep_mean_log_score,null);
  assert.equal(result.remove_mean_log_score,3);
  assert.match(result.keep_features.runtime.join(' '),/unknown intrinsic/);
  assert.ok(!JSON.stringify(result).includes('private-raw-result'));
  assert.throws(()=>discoveryFeedback({...measurement,independent_confirmation:true},'unit'),/Confirmation/);
});
