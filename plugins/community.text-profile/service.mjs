// Original, dependency-free implementation of this one text profiling service.
// This is not a general SDK. The native host owns admission and schema checks.
import {readFile} from 'node:fs/promises';
import {pathToFileURL} from 'node:url';
import {setImmediate as turn} from 'node:timers/promises';

export function profile(text) {
  if(typeof text!=='string' || !text.isWellFormed()) throw Error('Expected well-formed Unicode text.');
  const codePoints=Array.from(text).length;
  if(codePoints>65536) throw Error('Text exceeds 65536 Unicode code points.');
  const lfCount=text.split('\n').length-1;
  let rows=text===''?[]:text.split('\n');
  if(text.endsWith('\n')) rows.pop();
  rows=rows.map((row,index)=>index<lfCount&&row.endsWith('\r')?row.slice(0,-1):row);
  return {utf8_bytes:Buffer.byteLength(text,'utf8'),code_points:codePoints,lines:rows.length,
    blank_lines:rows.filter(row=>row.length===0).length,
    longest_line_code_points:rows.reduce((maximum,row)=>Math.max(maximum,Array.from(row).length),0),
    lf_count:lfCount,crlf_count:text.split('\r\n').length-1};
}

async function serve() {
  const manifest=JSON.parse(await readFile(new URL('./plugin.json',import.meta.url),'utf8'));
  const protocol={major:manifest.protocol.major,minor:manifest.protocol.max_minor};
  const description={identity:manifest.identity,services:manifest.services,service_dependencies:[],
    service_contracts:manifest.service_contracts,max_concurrency:1};
  const decoder=new TextDecoder('utf-8',{fatal:true});
  let instance, pending, output=Promise.resolve(),queued=0,buffer=Buffer.alloc(0),ended=false;
  const fault=(code,message)=>({code,message,retryable:false});
  const send=message=>{
    if(++queued>16) throw Error('Too many pending response frames.');
    const body=Buffer.from(JSON.stringify(message),'utf8');
    if(body.length>1024*1024) throw Error('Response exceeds frame capacity.');
    const header=Buffer.alloc(4);header.writeUInt32BE(body.length);
    const written=output.then(()=>new Promise((resolve,reject)=>process.stdout.write(Buffer.concat([header,body]),error=>error?reject(error):resolve())));
    output=written.finally(()=>queued--);return output;
  };
  const terminal=(id,outcome)=>send({kind:'Stream',envelope:{kind:'Terminal',request_id:id,stream_id:id,sequence:'0',outcome}});
  const positive=value=>typeof value==='string' && /^[1-9][0-9]{0,19}$/.test(value) && BigInt(value)<=18446744073709551615n;
  const active=scope=>{
    const grant=scope?.admission?.kind==='Active'?scope.admission.grant:null;
    return grant && grant.target_provider_instance===instance && grant.call_id===scope.call_id &&
      typeof scope.root_operation_id==='string' && scope.root_operation_id.length>0 && grant.root_operation_id===scope.root_operation_id &&
      grant.parent_call_id===scope.parent_call_id && typeof scope.assembly_id==='string' && scope.assembly_id.length>0 &&
      grant.assembly_id===scope.assembly_id && positive(grant.adopted_revision) && positive(grant.activation_attempt_generation);
  };
  async function invoke(invocation) {
    const id=invocation?.scope?.call_id;
    if(typeof id!=='string' || !id || id.length>256) throw Error('Invocation requires a bounded call identity.');
    if(pending?.id===id) throw Error('Duplicate active call identity.');
    if(pending) return terminal(id,{kind:'Error',error:fault('busy','This service handles one call at a time.')});
    if(!active(invocation.scope)) return terminal(id,{kind:'Error',error:fault('protocol_violation','Invocation does not match this activated provider instance.')});
    const current={id,cancelled:false,task:null};pending=current;
    current.task=(async()=>{
      let outcome;
      try {
        await turn();
        if(current.cancelled) throw fault('cancelled','Text profiling was cancelled.');
        if(invocation.service!=='text' || invocation.method!=='profile') throw fault('invalid_input','Choose text/profile.');
        const input=invocation.input;
        if(!input || typeof input!=='object' || Array.isArray(input) || Object.keys(input).length!==1 || !Object.hasOwn(input,'text'))
          throw fault('invalid_input','Input must contain exactly one text field.');
        if(invocation.timeout_ms!==null && (!Number.isSafeInteger(invocation.timeout_ms) || invocation.timeout_ms<=0))
          throw fault('invalid_input','Unsupported or expired invocation budget.');
        const result=profile(input.text);
        await turn();
        if(current.cancelled) throw fault('cancelled','Text profiling was cancelled.');
        outcome={kind:'Result',result};
      } catch(error) {
        outcome={kind:'Error',error:error.code?error:fault('invalid_input',error.message)};
      }
      await terminal(id,outcome);
    })().finally(()=>{if(pending===current)pending=null;});
    current.task.catch(()=>{ended=true;process.stdin.destroy();});
  }
  async function receive(message) {
    if(message.kind==='Initialize') {
      if(instance || typeof message.instance_id!=='string' || !message.instance_id) throw Error('Initialize must occur once with an instance identity.');
      if(message.configuration!==null && (typeof message.configuration!=='object' || Array.isArray(message.configuration) || Object.keys(message.configuration).length)) {
        await send({kind:'InitializationFailed',protocol,error:fault('invalid_input','This service has no configuration fields.')});return false;
      }
      instance=message.instance_id;await send({kind:'Ready',protocol,description});return true;
    }
    if(!instance) throw Error('Initialize is required before other messages.');
    switch(message.kind) {
      case 'Invoke': await invoke(message.invocation);return true;
      case 'Cancel': if(pending?.id===message.call_id)pending.cancelled=true;return true;
      case 'Credits': return true; // No Data events exist; Terminal needs no credit.
      case 'Ping': if(typeof message.nonce!=='string')throw Error('Ping requires a nonce.');await send({kind:'Pong',nonce:message.nonce});return true;
      case 'Shutdown': if(pending) {pending.cancelled=true;await pending.task;}await output;return false;
      default: throw Error('Unsupported message for this dependency-free service.');
    }
  }
  try {
    for await(const chunk of process.stdin) {
      buffer=Buffer.concat([buffer,chunk]);
      while(buffer.length>=4) {
        const size=buffer.readUInt32BE();
        if(!size || size>1024*1024)throw Error('Input frame exceeds capacity.');
        if(buffer.length<size+4)break;
        const message=JSON.parse(decoder.decode(buffer.subarray(4,size+4)));buffer=buffer.subarray(size+4);
        if(!await receive(message)){ended=true;break;}
      }
      if(ended)break;
    }
    if(!ended && buffer.length)throw Error('Incomplete input frame.');
  } finally {
    if(pending){pending.cancelled=true;await pending.task;}
    await output;
  }
}
if(process.argv[1] && pathToFileURL(process.argv[1]).href===import.meta.url) {
  serve().catch(()=>{process.stderr.write('Text service stopped after an invalid or incomplete channel exchange.\n');process.exitCode=1;});
}
