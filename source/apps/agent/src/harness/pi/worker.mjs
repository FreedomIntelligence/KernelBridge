import { RpcClient, DEFAULT_FRAME_BYTES } from '../../clients/protocol.mjs';
import { runGovernedPi as runAgent } from '../../loopx/pi-host.ts';
export {runGovernedPi as runAgent} from '../../loopx/pi-host.ts';
import { pathToFileURL } from 'node:url';
if(process.argv[1]&&import.meta.url===pathToFileURL(process.argv[1]).href){
  const limit=Number(process.env.KERNELCAT_MAX_FRAME_BYTES??DEFAULT_FRAME_BYTES);
  try{await runAgent(new RpcClient(process.stdin,process.stdout,limit));process.exitCode=0;}
  catch(error){process.stderr.write(String(error)+'\n');process.exitCode=1;}
  // Plugin shutdown was awaited; do not keep provider/watch timers alive in the disposable process.
  process.exit(process.exitCode);
}
