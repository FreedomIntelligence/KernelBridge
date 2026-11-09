import test from 'node:test';
import assert from 'node:assert/strict';
import { assistant } from '../source/apps/agent/src/clients/protocol.mjs';
test('OpenAI-compatible usage reaches Pi without double-counting cached input',()=>{
 const m=assistant({text:'ok',calls:[],usage:{prompt_tokens:100,completion_tokens:15,total_tokens:115,prompt_cache_hit_tokens:70}});
 assert.equal(m.usage.input,30);assert.equal(m.usage.cacheRead,70);assert.equal(m.usage.output,15);assert.equal(m.usage.totalTokens,115);
});
test('Anthropic input excludes cache creation and read tokens',()=>{
 const m=assistant({text:'ok',calls:[],usage:{input_tokens:20,output_tokens:5,cache_read_input_tokens:40,cache_creation_input_tokens:10}});
 assert.equal(m.usage.input,20);assert.equal(m.usage.totalTokens,75);
});
