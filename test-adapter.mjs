import assert from 'node:assert/strict';
import {createServer,request as rawRequest} from 'node:http';
import {pathToFileURL,fileURLToPath} from 'node:url';
import path from 'node:path';
import {createRequire} from 'node:module';
import {createAdapter} from './adapter.mjs';
const deps=process.env.MCP_ADAPTER_DEPENDENCIES||path.dirname(fileURLToPath(import.meta.url));
const require=createRequire(path.join(deps,'package.json'));const{z}=require('zod');
const{Server}=await import(pathToFileURL(path.join(deps,'node_modules/@modelcontextprotocol/sdk/dist/esm/server/index.js')));
const{StreamableHTTPServerTransport}=await import(pathToFileURL(path.join(deps,'node_modules/@modelcontextprotocol/sdk/dist/esm/server/streamableHttp.js')));
const token='adapter-test-token';const seen=[];let backendHttpRequests=0;let cancellations=0,delayedDone=false,delayedSseDone=false,allowBackendNoAuth=false;
const descriptor={name:'fixture_echo',title:'Fixture echo',description:'No model or file writes',inputSchema:{type:'object',properties:{value:{type:'string'}},required:['value'],additionalProperties:false},execution:{taskSupport:'forbidden'},outputSchema:{type:'object',properties:{answer:{type:'string'}},required:['answer']},annotations:{readOnlyHint:true,destructiveHint:false,openWorldHint:false},_meta:{'openai/outputTemplate':'ui://fixture','fixture/opaque':{proof:'operator-proof-fixture'}}};
const modernDescriptor=structuredClone(descriptor);delete modernDescriptor.execution;
const callResult={content:[{type:'text',text:'fixture result'}],structuredContent:{answer:'ok'},_meta:{'fixture/uiProof':{opaque:'test-proof',revision:9},'openai/widgetAccessible':true}};
const resource={uri:'ui://fixture',name:'fixture',mimeType:'text/html',_meta:{'openai/widgetDescription':'fixture'}};
const prompt={name:'fixture_prompt',description:'fixture',arguments:[{name:'topic',required:true}],_meta:{'fixture/value':7}};
const mock=createServer(async(req,res)=>{
 backendHttpRequests++;
 if(!allowBackendNoAuth&&req.headers.authorization!=='Bearer '+token){res.writeHead(401);res.end();return}
 let chunks=[];for await(const c of req)chunks.push(c);
 let body;try{body=JSON.parse(Buffer.concat(chunks))}catch{res.writeHead(400);res.end();return}
 if(body.method==='notifications/cancelled')cancellations++;
 const server=new Server({name:'fixture-core',version:'1.0.0'},{capabilities:{tools:{listChanged:true},resources:{subscribe:true,listChanged:true},prompts:{listChanged:true},completions:{}}});
 for(const method of['tools/list','tools/call','resources/list','resources/templates/list','resources/read','prompts/list','prompts/get','completion/complete']){
  server.setRequestHandler(z.looseObject({method:z.literal(method)}),async request=>{
   seen.push({method,params:structuredClone(request.params),headers:{...req.headers}});
   if(method==='tools/list')return{tools:[descriptor],_meta:{'fixture/listMeta':{revision:3}}};
   if(method==='tools/call'){if(request.params.name==='delayed-sse'){await server.notification({method:'notifications/progress',params:{progressToken:request.params._meta.progressToken,progress:0}},{relatedRequestId:request.id});await new Promise(r=>setTimeout(r,800));delayedSseDone=true}if(request.params.name==='delayed'){await new Promise(r=>setTimeout(r,180));delayedDone=true}return callResult}
   if(method==='resources/list')return{resources:[resource]};
   if(method==='resources/templates/list')return{resourceTemplates:[{uriTemplate:'fixture://{id}',name:'template',_meta:{fixture:1}}]};
   if(method==='resources/read')return{contents:[{uri:'ui://fixture',mimeType:'text/html',text:'<p>fixture</p>',_meta:{'fixture/contents':true}}],_meta:{'fixture/read':3}};
   if(method==='prompts/list')return{prompts:[prompt]};
   if(method==='prompts/get')return{description:'fixture',messages:[{role:'user',content:{type:'text',text:'fixture'}}],_meta:{'fixture/prompt':2}};
   return{completion:{values:['fixture'],total:1,hasMore:false},_meta:{'fixture/completion':1}};
  });
 }
 const transport=new StreamableHTTPServerTransport({sessionIdGenerator:undefined});
 try{await server.connect(transport);await transport.handleRequest(req,res,body)}finally{await transport.close();await server.close()}
});
await new Promise(r=>mock.listen(0,'127.0.0.1',r));const backendPort=mock.address().port;
const adapter=await createAdapter({backendUrl:'http://127.0.0.1:'+backendPort+'/mcp',expectedBearer:token,maxRequestBytes:4096,requestTimeoutMs:3000});
await adapter.listen(0);const frontPort=adapter.server.address().port;
let id=1;const tests=[];
function parseResponse(text){const data=text.split(/\r?\n/).filter(l=>l.startsWith('data:')).map(l=>l.slice(5).trim()).filter(Boolean);return data.length?JSON.parse(data.at(-1)):text?JSON.parse(text):undefined}
async function rpc(method,params={},modern=true,options={}){
 const rid=id++;const meta=modern?{'io.modelcontextprotocol/protocolVersion':'2026-07-28','io.modelcontextprotocol/clientCapabilities':{},'io.modelcontextprotocol/clientInfo':{name:'adapter-tests',version:'1'}}:{};
 const bodyParams={...params,_meta:{...meta,...params._meta}};if(options.headerOnly){delete bodyParams.name;delete bodyParams.uri}
 const body={jsonrpc:'2.0',id:rid,method,params:bodyParams};
 const headers={'authorization':'Bearer '+token,'accept':'application/json, text/event-stream','content-type':'application/json',...(modern?{'mcp-protocol-version':'2026-07-28','mcp-method':method}:{'mcp-protocol-version':'2025-11-25'}),...options.headers};
 if(modern&&params.name)headers['mcp-name']=params.name;
 if(modern&&params.uri)headers['mcp-name']=params.uri;
 const response=await fetch('http://127.0.0.1:'+frontPort+'/mcp',{method:'POST',headers,body:JSON.stringify(body),signal:options.signal});
 const text=await response.text();return{status:response.status,body:parseResponse(text)};
}
try{
 assert.equal(adapter.server.address().address,'127.0.0.1');tests.push('loopback-only');
 let r=await rpc('server/discover');assert.equal(r.status,200,JSON.stringify(r.body));assert.ok(r.body.result.supportedVersions.includes('2026-07-28'));assert.notEqual(r.body.result.capabilities.tools.listChanged,true);assert.notEqual(r.body.result.capabilities.resources.subscribe,true);tests.push('modern-native-discovery-no-false-subscriptions');
 r=await rpc('tools/list');assert.equal(r.status,200,JSON.stringify(r.body));assert.deepEqual(r.body.result.tools,[modernDescriptor]);assert.deepEqual(r.body.result._meta['fixture/listMeta'],{revision:3});assert.equal(r.body.result.resultType,'complete');assert.equal(r.body.result.ttlMs,0);assert.equal(r.body.result.cacheScope,'private');tests.push('modern-descriptor-schema-annotations-meta-exact');
 const meta={'openai/session':'trusted-session-fixture','openai/conversation':'conversation-fixture','fixture/proof':{value:'opaque'}};
 const headers={'openai-conversation-id':'header-conversation-fixture','x-openai-session-id':'header-session-fixture','x-request-id':'request-fixture'};
 r=await rpc('tools/call',{name:'fixture_echo',arguments:{value:'abc'},_meta:meta},true,{headers});assert.equal(r.status,200,JSON.stringify(r.body));assert.deepEqual(r.body.result.structuredContent,callResult.structuredContent);for(const[k,v]of Object.entries(callResult._meta))assert.deepEqual(r.body.result._meta[k],v);assert.deepEqual(r.body.result.content,callResult.content);
 let last=seen.filter(x=>x.method==='tools/call').at(-1);assert.deepEqual(last.params,{name:'fixture_echo',arguments:{value:'abc'},_meta:meta});for(const[k,v]of Object.entries(headers))assert.equal(last.headers[k],v);assert.equal(last.headers['mcp-protocol-version'],'2025-11-25');tests.push('modern-input-result-trusted-metadata-and-headers-preserved');
 r=await rpc('tools/call',{name:'fixture_echo',arguments:{value:'header-only'},_meta:meta},true,{headerOnly:true,headers});assert.equal(r.status,200,JSON.stringify(r.body));assert.ok(r.body.result,JSON.stringify(r.body));assert.equal(r.body.result.isError,undefined,JSON.stringify(r.body));last=seen.filter(x=>x.method==='tools/call').at(-1);assert.deepEqual(last.params,{name:'fixture_echo',arguments:{value:'header-only'},_meta:meta});tests.push('modern-header-only-tool-name-normalized-then-validated-by-sdk');
 r=await rpc('resources/read',{uri:'ui://fixture',_meta:meta},true,{headerOnly:true});assert.equal(r.status,200,JSON.stringify(r.body));last=seen.filter(x=>x.method==='resources/read').at(-1);assert.deepEqual(last.params,{uri:'ui://fixture',_meta:meta});tests.push('modern-header-only-resource-uri-normalized-then-validated-by-sdk');
 r=await rpc('initialize',{protocolVersion:'2025-11-25',capabilities:{},clientInfo:{name:'legacy-test',version:'1'}},false);assert.equal(r.status,200,JSON.stringify(r.body));assert.equal(r.body.result.protocolVersion,'2025-11-25');tests.push('legacy-native-initialize');
 r=await rpc('tools/list',{},false);assert.equal(r.status,200,JSON.stringify(r.body));assert.deepEqual(r.body.result.tools,[descriptor]);assert.equal(r.body.result.resultType,undefined);tests.push('legacy-descriptor-exact');
 for(const[method,params,field]of[
  ['resources/list',{},'resources'],['resources/templates/list',{},'resourceTemplates'],['resources/read',{uri:'ui://fixture'},'contents'],
  ['prompts/list',{},'prompts'],['prompts/get',{name:'fixture_prompt',arguments:{topic:'x'}},'messages'],
  ['completion/complete',{ref:{type:'ref/prompt',name:'fixture_prompt'},argument:{name:'topic',value:'f'}},'completion']
 ]){r=await rpc(method,params);assert.equal(r.status,200,method+': '+JSON.stringify(r.body));assert.ok(r.body.result[field],method);tests.push('passthrough-'+method)}
 r=await rpc('tools/call',{name:'fixture_echo',arguments:{value:'x'},inputResponses:{foo:{}}});assert.equal(r.status,400);assert.equal(r.body.error.code,-32602);tests.push('unsupported-modern-input-semantics-rejected');
 const beforeBatch=backendHttpRequests;
 const duplicateBatch=[{jsonrpc:'2.0',id:777,method:'tools/call',params:{name:'fixture_echo',arguments:{value:'batch-a'},_meta:{'openai/session':'fixture-batch-a'}}},{jsonrpc:'2.0',id:777,method:'tools/call',params:{name:'fixture_echo',arguments:{value:'batch-b'},_meta:{'openai/session':'fixture-batch-b'}}}];
 const batchResponse=await fetch('http://127.0.0.1:'+frontPort+'/mcp',{method:'POST',headers:{authorization:'Bearer '+token,accept:'application/json, text/event-stream','content-type':'application/json','mcp-protocol-version':'2025-11-25'},body:JSON.stringify(duplicateBatch)});
 assert.equal(batchResponse.status,400);assert.equal((await batchResponse.json()).error.code,-32600);assert.equal(backendHttpRequests,beforeBatch);tests.push('duplicate-id-batch-rejected-before-any-backend-http');
 const beforeHost=seen.length;
 r=await rpc('tools/list',{},true,{headers:{origin:'https://outside.invalid'}});assert.equal(r.status,403);assert.equal(seen.length,beforeHost);tests.push('external-origin-rejected-before-backend');
 const badHostStatus=await new Promise((resolve,reject)=>{
  const req=rawRequest({host:'127.0.0.1',port:frontPort,path:'/mcp',method:'POST',headers:{host:'outside.invalid',authorization:'Bearer '+token,'content-type':'application/json'}},res=>{res.resume();res.on('end',()=>resolve(res.statusCode))});req.on('error',reject);req.end('{}');
 });assert.equal(badHostStatus,403);assert.equal(seen.length,beforeHost);tests.push('nonloopback-host-rejected-before-backend');
 r=await rpc('tools/list',{},true,{headers:{origin:'null'}});assert.equal(r.status,403);tests.push('opaque-origin-rejected');
 const before=seen.length;r=await rpc('tools/list',{},true,{headers:{authorization:'Bearer invalid-test-token'}});assert.equal(r.status,401);assert.equal(seen.length,before);tests.push('authentication-before-backend');
 r=await rpc('tools/call',{name:'fixture_echo',arguments:{value:'x'.repeat(5000)}});assert.equal(r.status,413);tests.push('request-size-bound');
 const controller=new AbortController();const pending=rpc('tools/call',{name:'delayed',arguments:{}},true,{signal:controller.signal});setTimeout(()=>controller.abort(),60);await pending.catch(()=>{});await new Promise(r=>setTimeout(r,260));assert.equal(delayedDone,true);assert.equal(cancellations,0);tests.push('disconnect-does-not-send-backend-cancel');
 for(const invalid of ['false','true',0,1,null,{}])await assert.rejects(createAdapter({backendUrl:'http://127.0.0.1:'+backendPort+'/mcp',allowUnauthenticatedLoopback:invalid}));tests.push('nonboolean-noauth-flags-rejected');
 const streaming=await createAdapter({backendUrl:'http://127.0.0.1:'+backendPort+'/mcp',expectedBearer:token,requestTimeoutMs:3000,responseTimeoutMs:250,responseMode:'sse'});await streaming.listen(0);
 try{
  const port=streaming.server.address().port;const wire={jsonrpc:'2.0',id:881,method:'tools/call',params:{name:'delayed-sse',arguments:{},_meta:{progressToken:'sse-timeout-fixture','io.modelcontextprotocol/protocolVersion':'2026-07-28','io.modelcontextprotocol/clientCapabilities':{}}}};
  const response=await fetch('http://127.0.0.1:'+port+'/mcp',{method:'POST',headers:{authorization:'Bearer '+token,accept:'application/json, text/event-stream','content-type':'application/json','mcp-protocol-version':'2026-07-28','mcp-method':'tools/call','mcp-name':'delayed-sse'},body:JSON.stringify(wire)});
  const partial=await response.text();assert.equal(response.status,200);assert.ok(response.headers.get('content-type').includes('text/event-stream'));assert.ok(partial.includes('notifications/progress'));await new Promise(r=>setTimeout(r,900));assert.equal(delayedSseDone,true);assert.equal(cancellations,0);
  const alive=await fetch('http://127.0.0.1:'+port+'/mcp',{method:'POST',headers:{authorization:'Bearer '+token,accept:'application/json, text/event-stream','content-type':'application/json','mcp-protocol-version':'2026-07-28','mcp-method':'tools/list'},body:JSON.stringify({jsonrpc:'2.0',id:882,method:'tools/list',params:{_meta:{'io.modelcontextprotocol/protocolVersion':'2026-07-28','io.modelcontextprotocol/clientCapabilities':{}}}})});assert.equal(alive.status,200);await alive.text();tests.push('sse-timeout-keeps-process-alive-and-core-work-running');
 }finally{await streaming.close()}
 await assert.rejects(createAdapter({backendUrl:'http://127.0.0.1:'+backendPort+'/mcp'}));tests.push('noauth-requires-explicit-operator-optin');
 allowBackendNoAuth=true;
 const noauth=await createAdapter({backendUrl:'http://127.0.0.1:'+backendPort+'/mcp',allowUnauthenticatedLoopback:true});await noauth.listen(0);
 try{
  const response=await fetch('http://127.0.0.1:'+noauth.server.address().port+'/mcp',{method:'POST',headers:{accept:'application/json, text/event-stream','content-type':'application/json','mcp-protocol-version':'2026-07-28','mcp-method':'tools/list'},body:JSON.stringify({jsonrpc:'2.0',id:991,method:'tools/list',params:{_meta:{'io.modelcontextprotocol/protocolVersion':'2026-07-28','io.modelcontextprotocol/clientCapabilities':{}}}})});
  assert.equal(response.status,200);const data=parseResponse(await response.text());assert.deepEqual(data.result.tools,[modernDescriptor]);tests.push('explicit-loopback-noauth-preserves-existing-noauth-core');
 }finally{await noauth.close()}
 console.log(JSON.stringify({status:'passed',no_llm:true,tests,backend_observed_calls:seen.length,production_service_changed:false},null,2));
}finally{await adapter.close();mock.closeAllConnections();await new Promise(r=>mock.close(r))}
