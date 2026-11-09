import { Type } from 'typebox';

/** Loaded only as a host-required child extension; no native tools or ambient IO. */
export default function childTools(pi) {
  pi.registerTool({
    name:'kernelcat_child_effect', label:'Read kernel evidence',
    description:'Perform one host-sealed read against the original campaign ledger.',
    parameters:Type.Object({}, {additionalProperties:true}),
    async execute(id,call) {
      const child=globalThis[Symbol.for('kernelcat.child.bridge/v1')]?.getStore();
      if(!child?.rpc||child.mode!=='read_only'||call.id!==id)throw new Error('No read-only child permit');
      const response=await child.rpc.call('child_tool',{permit:child.permit,call});
      if(response.stop)throw new Error(`Child stopped: ${response.reason}`);
      return {content:[{type:'text',text:JSON.stringify(response.outcome)}],details:response,
        isError:response.outcome.status!=='completed'};
    },
  });
}
