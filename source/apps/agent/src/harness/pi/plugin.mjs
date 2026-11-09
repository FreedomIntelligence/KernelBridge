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
export function createPluginBridge(rpc,provider,cwd) {
  let api;
  const registrations=[];
  const extension=(pi)=>{
    api=pi;
    pi.registerProvider('kernelcat',provider);
    for(const name of ['kernel-scout','kernel-reviewer']) {
      registrations.push(registerAgentViaEvents({pi,name,definition:{
        description:'Analyze kernel evidence with a host-restricted read capability',
        systemPrompt:'Use supplied material and host-permitted reads. Never invent measurements. Return an advisory answer, not independent validation.',
        tools:['kernelcat_child_effect'],excludeTools:[],allowNestedSubagents:false,model:'kernelcat/controlled',thinking:'off',
        systemPromptMode:'replace',inheritProjectContext:false,inheritGlobalContext:false,inheritSkills:false,
        defaultContext:'fresh',defaultAsync:false,defaultTimeoutMs:60000,
        skills:[],extensions:[childExtension],defaultReads:[],maxSubagentDepth:1,
      }}));
    }
    pi.on('session_start',(_event,ctx)=>{
      registrations.push(registerRequiredChildExtensions({sessionId:ctx.sessionManager.getSessionId(),
        extensions:[{id:'kernelcat-readonly-rpc',path:childExtension}],requireForAllRunners:true}));
      registrations.push(registerSubagentCapabilityCeiling({
        sessionId:ctx.sessionManager.getSessionId(),source:'kernelcat',
        ceiling:{allowedAgents:['kernel-scout','kernel-reviewer'],allowedTools:['kernelcat_child_effect'],denyExtensions:false},
      }));
    });
    pi.on('session_shutdown',()=>{for(const r of registrations.splice(0))r.dispose();});
  };
  async function execute(call,signal) {
    const permission=await rpc.call('plugin_begin',call);
    if(permission.cached) return permission;
    if(!api||typeof permission.permit!=='string') throw new Error('Plugin context or execution permit unavailable');
    const identity={requestId:call.id,ownerRunId:permission.permit,nodeId:'leaf'};
    const response=await childContext.run({...permission,rpc},()=>new Promise((resolve,reject)=>{
      let settled=false;
      const finish=(value,error)=>{
        if(settled)return;settled=true;off();clearTimeout(timer);signal?.removeEventListener('abort',abort);
        error?reject(error):resolve(value);
      };
      const abort=()=>{api.events.emit(CANCEL,identity);finish(undefined,new Error('Delegation cancelled; receipt reconciliation required'));};
      const off=api.events.on(RESPONSE,(value)=>{
        if(value?.requestId===identity.requestId&&value?.ownerRunId===identity.ownerRunId&&value?.nodeId===identity.nodeId) finish(value);
      });
      const timer=setTimeout(()=>{api.events.emit(CANCEL,identity);finish(undefined,new Error('Delegation deadline; effect is unknown'));},permission.timeout_ms+5000);
      signal?.addEventListener('abort',abort,{once:true});
      if(signal?.aborted){abort();return;}
      api.events.emit(REQUEST,{...identity,agent:permission.agent,task:permission.task,context:'fresh',cwd,
        model:'kernelcat/controlled',thinking:'off',timeoutMs:permission.timeout_ms,
        toolBudget:{hard:permission.mode==='read_only'?16:0,block:'*'},skill:false,artifacts:false,intercomBridge:{mode:'off'},result:{kind:'text'}});
    }));
    const completed=response.status==='completed'&&response.result?.kind==='text';
    return rpc.call('plugin_settle',{permit:permission.permit,status:completed?'completed':'failed',
      text:completed?response.result.text:''});
  }
  return {factories:[subagents,extension],execute};
}
