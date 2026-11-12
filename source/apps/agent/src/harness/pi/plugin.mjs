import { AsyncLocalStorage } from 'node:async_hooks';
import { fileURLToPath } from 'node:url';
import { registerRequiredChildExtensions } from 'pi-subagents/required-child-extensions';
import subagents from 'pi-subagents';
import { registerAgentViaEvents } from 'pi-subagents/agents';
import { registerSubagentCapabilityCeiling } from 'pi-subagents/capability-ceiling';
import {
  SUBAGENT_DELEGATION_REQUEST_EVENT as REQUEST,
  SUBAGENT_DELEGATION_RESPONSE_EVENT as RESPONSE,
  SUBAGENT_DELEGATION_CANCEL_EVENT as CANCEL,
} from 'pi-subagents/delegation';

const childKey=Symbol.for('kernelcat.child.bridge/v1');
export const childContext=globalThis[childKey]??=new AsyncLocalStorage();
const childExtension=fileURLToPath(new URL('./child-tools.mjs',import.meta.url));
/** FIFO slots: delegate calls beyond the concurrency bound wait instead of failing. */
function slots(limit) {
  let active=0;const waiting=[];
  return {
    async acquire(signal) {
      if(!limit||active<limit){active++;return;}
      await new Promise((resolve,reject)=>{
        const entry={resolve,reject};waiting.push(entry);
        signal?.addEventListener('abort',()=>{const index=waiting.indexOf(entry);if(index>=0){waiting.splice(index,1);reject(new Error('Delegation cancelled while queued'));}},{once:true});
      });
      active++;
    },
    release() {active--;waiting.shift()?.resolve();},
  };
}

export function createPluginBridge(rpc,provider,cwd,{maxConcurrent,agents=['kernel-scout','kernel-reviewer']}={}) {
  let api;
  const queue=slots(maxConcurrent);
  const model = 'kernelcat/' + provider.models[0].id;
  const registrations=[];
  const extension=(pi)=>{
    api=pi;
    pi.registerProvider('kernelcat',provider);
    // The host composes each child's instructions and tools; the plugin only
    // needs every host-declared agent name to run its child session.
    for(const name of agents) {
      registrations.push(registerAgentViaEvents({pi,name,definition:{
        description:`KernelBridge host-governed subagent ${name}.`,
        systemPrompt:'Use supplied material and host-permitted discovery/read tools. Follow the host remaining-call instructions and return findings with paths, evidence and unresolved questions before the limit. Never invent measurements. Return an advisory answer, not independent validation.',
        tools:['kernelcat_child_effect'],excludeTools:[],allowNestedSubagents:false,model,thinking:'off',
        systemPromptMode:'replace',inheritProjectContext:false,inheritGlobalContext:false,inheritSkills:false,
        defaultContext:'fresh',defaultAsync:false,
        skills:[],extensions:[childExtension],defaultReads:[],maxSubagentDepth:1,
      }}));
    }
    pi.on('session_start',(_event,ctx)=>{
      registrations.push(registerRequiredChildExtensions({sessionId:ctx.sessionManager.getSessionId(),
        extensions:[{id:'kernelcat-readonly-rpc',path:childExtension}],requireForAllRunners:true}));
      registrations.push(registerSubagentCapabilityCeiling({
        sessionId:ctx.sessionManager.getSessionId(),source:'kernelcat',
        ceiling:{allowedAgents:[...agents],allowedTools:['kernelcat_child_effect'],denyExtensions:false},
      }));
    });
    pi.on('session_shutdown',()=>{for(const r of registrations.splice(0))r.dispose();});
  };
  async function execute(call,signal) {
    await queue.acquire(signal);
    let release=true;
    try {
      const permission=await rpc.call('plugin_begin',call);
      if(permission.cached) return permission;
      if(!api||typeof permission.permit!=='string') throw new Error('Plugin context or execution permit unavailable');
      if(permission.background===true){
        // The delegate call already settled as "started". The child keeps its
        // slot until it settles; Esc on the parent turn does not stop it.
        release=false;
        void settle(call,permission,undefined,{background:true}).catch(()=>{}).finally(()=>queue.release());
        return {outcome:permission.outcome};
      }
      return await settle(call,permission,signal);
    } finally { if(release) queue.release(); }
  }
  function child(call,permission,signal) {
    const identity={requestId:call.id,ownerRunId:permission.permit,nodeId:'leaf'};
    return childContext.run({...permission,rpc},()=>new Promise((resolve,reject)=>{
      let settled=false;
      const finish=(value,error)=>{
        if(settled)return;settled=true;off();clearTimeout(timer);signal?.removeEventListener('abort',abort);
        error?reject(error):resolve(value);
      };
      const abort=()=>{
        // Stop the plugin, then reconcile through the same ordered host lane
        // as its in-flight effects before allowing the parent to settle.
        finish({status:'cancelled'});
        api.events.emit(CANCEL,identity);
      };
      const off=api.events.on(RESPONSE,(value)=>{
        if(value?.requestId===identity.requestId&&value?.ownerRunId===identity.ownerRunId&&value?.nodeId===identity.nodeId) finish(value);
      });
      // The host enforces any configured deadline; this is only a transport backstop.
      // Past the host deadline, its final-summary grace and one provider call (backstop_ms).
      const backstop=typeof permission.backstop_ms==='number'?permission.backstop_ms
        :typeof permission.timeout_ms==='number'?permission.timeout_ms+5000:undefined;
      const timer=backstop!==undefined
        ?setTimeout(()=>{api.events.emit(CANCEL,identity);finish(undefined,new Error('Delegation deadline; effect is unknown'));},backstop)
        :undefined;
      signal?.addEventListener('abort',abort,{once:true});
      if(signal?.aborted){abort();return;}
      api.events.emit(REQUEST,{...identity,agent:permission.agent,task:permission.task,context:'fresh',cwd,
        model,thinking:'off',...(typeof permission.timeout_ms==='number'?{timeoutMs:permission.timeout_ms}:{}),
        // The host seals and counts every tool call. A second plugin budget can
        // suppress the RPC and leave an admitted host call without a receipt.
        skill:false,artifacts:false,intercomBridge:{mode:'off'},result:{kind:'text'}});
    }));
  }
  async function settle(call,permission,signal,{background=false}={}) {
    let response;
    // Foreground transport failures propagate (the effect may be unknown); a
    // background child reports failure, and the host still fences any
    // unresolved child action before accepting that settlement.
    try { response=await child(call,permission,signal); }
    catch(error) { if(!background) throw error; response={status:'failed'}; }
    const completed=response.status==='completed'&&response.result?.kind==='text';
    return rpc.call('plugin_settle',{permit:permission.permit,status:completed?'completed':response.status==='cancelled'?'cancelled':'failed',
      text:completed?response.result.text:''});
  }
  return {factories:[subagents,extension],execute};
}
