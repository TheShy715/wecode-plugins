import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {spawn} from 'node:child_process';
import {once} from 'node:events';
import {fileURLToPath} from 'node:url';
import {profile} from '../plugins/community.text-profile/service.mjs';

test('text counts have explicit Unicode, CRLF, empty and trailing-line semantics',()=>{
 assert.deepEqual(profile('A🙂\r\n\nz'),{utf8_bytes:9,code_points:6,lines:3,blank_lines:1,longest_line_code_points:2,lf_count:2,crlf_count:1});
 assert.deepEqual(profile(''),{utf8_bytes:0,code_points:0,lines:0,blank_lines:0,longest_line_code_points:0,lf_count:0,crlf_count:0});
 assert.equal(profile('a\r').longest_line_code_points,2);
 assert.equal(profile('\r').blank_lines,0);
 assert.equal(profile('a\r\nb\r').longest_line_code_points,2);
 assert.equal(profile('a\n').lines,1);assert.equal(profile('\n').blank_lines,1);assert.equal(profile(' ').blank_lines,0);
 assert.equal(profile('🙂'.repeat(65536)).utf8_bytes,262144);
 for(const value of [null,42,'a'.repeat(65537),'\ud800'])assert.throws(()=>profile(value));
});
test('actual child speaks only the bounded service and honours channel identity/cancellation',async()=>{
 const script=fileURLToPath(new URL('../plugins/community.text-profile/service.mjs',import.meta.url));
 const child=spawn(process.execPath,[script],{stdio:['pipe','pipe','pipe']}),closed=once(child,'close');
 let bytes=Buffer.alloc(0),frames=[],waiting=[],diagnostic='';child.stderr.on('data',part=>diagnostic+=part);
 child.stdout.on('data',part=>{bytes=Buffer.concat([bytes,part]);while(bytes.length>=4&&bytes.length>=bytes.readUInt32BE()+4){const size=bytes.readUInt32BE();const frame=JSON.parse(bytes.subarray(4,size+4));bytes=bytes.subarray(size+4);if(waiting.length)waiting.shift()(frame);else frames.push(frame);}});
 const next=()=>frames.length?Promise.resolve(frames.shift()):new Promise(resolve=>waiting.push(resolve));
 const send=body=>{const bytes=Buffer.from(JSON.stringify(body)),head=Buffer.alloc(4);head.writeUInt32BE(bytes.length);child.stdin.write(Buffer.concat([head,bytes]));};
 const invocation=id=>({kind:'Invoke',event_window:1,invocation:{service:'text',method:'profile',input:{text:'A🙂\r\n\nz'},timeout_ms:null,
  scope:{root_operation_id:id,call_id:id,parent_call_id:null,assembly_id:'test-assembly',caller_provider:'test-peer',admission:{kind:'Active',grant:{grant_id:'test-only',root_operation_id:id,call_id:id,parent_call_id:null,assembly_id:'test-assembly',adopted_revision:'1',closure_digest:'a'.repeat(64),activation_attempt_generation:'1',host_instance:'test-host',caller_instance:'test-caller',target_binding_id:'test-binding',target_provider_instance:'test-provider'}}}}});
 const timer=setTimeout(()=>child.kill('SIGTERM'),10000); // Test observation only.
 try {
  send({kind:'Initialize',protocol:{major:1,minor:113},instance_id:'test-provider',configuration:null});
  const ready=await next(),manifest=JSON.parse(await readFile(new URL('../plugins/community.text-profile/plugin.json',import.meta.url),'utf8'));
  assert.equal(ready.kind,'Ready');assert.deepEqual(ready.description.services,manifest.services);
  send(invocation('business'));const result=await next();assert.equal(result.envelope.outcome.kind,'Result');assert.deepEqual(result.envelope.outcome.result,profile('A🙂\r\n\nz'));
  const bad=invocation('wrong');bad.invocation.scope.admission.grant.target_provider_instance='someone-else';send(bad);assert.equal((await next()).envelope.outcome.error.code,'protocol_violation');
  send(invocation('cancel'));send({kind:'Cancel',call_id:'cancel'});assert.equal((await next()).envelope.outcome.error.code,'cancelled');
  send({kind:'Ping',nonce:'still-open'});assert.deepEqual(await next(),{kind:'Pong',nonce:'still-open'});
  send({kind:'Shutdown'});child.stdin.end();assert.deepEqual(await closed,[0,null],diagnostic);
 }finally{clearTimeout(timer);if(child.exitCode===null&&child.signalCode===null)child.kill('SIGTERM');await closed;}
});
