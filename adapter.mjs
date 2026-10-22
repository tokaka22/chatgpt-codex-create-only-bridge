import {createServer} from 'node:http';
import {AsyncLocalStorage} from 'node:async_hooks';
import {timingSafeEqual} from 'node:crypto';
import {pathToFileURL,fileURLToPath} from 'node:url';
import path from 'node:path';
import {createRequire} from 'node:module';

const DEPENDENCIES=process.env.MCP_ADAPTER_DEPENDENCIES || path.dirname(fileURLToPath(import.meta.url));
const require=createRequire(path.join(DEPENDENCIES,'package.json'));
const {z}=require('zod');
const {McpServer,createMcpHandler,ProtocolError,fromJsonSchema}=await import(pathToFileURL(path.join(DEPENDENCIES,'node_modules/@modelcontextprotocol/server/dist/index.mjs')));
const {toNodeHandler}=await import(pathToFileURL(path.join(DEPENDENCIES,'node_modules/@modelcontextprotocol/node/dist/index.mjs')));
const {Client}=await import(pathToFileURL(path.join(DEPENDENCIES,'node_modules/@modelcontextprotocol/sdk/dist/esm/client/index.js')));
const {StreamableHTTPClientTransport}=await import(pathToFileURL(path.join(DEPENDENCIES,'node_modules/@modelcontextprotocol/sdk/dist/esm/client/streamableHttp.js')));
const storage=new AsyncLocalStorage();
const METHODS={
 'tools/list':'tools','tools/call':'tools',
 'resources/list':'resources','resources/templates/list':'resources','resources/read':'resources',
 'prompts/list':'prompts','prompts/get':'prompts',
 'completion/complete':'completions','logging/setLevel':'logging'
};
const DROP_HEADERS=new Set(['host','connection','content-length','transfer-encoding','keep-alive','upgrade','te','trailer','proxy-connection','accept','content-type','mcp-protocol-version','mcp-session-id','mcp-method','mcp-name','mcp-uri']);
const ENVELOPE_KEYS=new Set(['io.modelcontextprotocol/protocolVersion','io.modelcontextprotocol/clientInfo','io.modelcontextprotocol/clientCapabilities','io.modelcontextprotocol/logLevel']);
const plain=x=>x!==null&&typeof x==='object'&&!Array.isArray(x);
function sameToken(a,b){const x=Buffer.from(a),y=Buffer.from(b);return x.length===y.length&&timingSafeEqual(x,y)}
export function legacyHeaders(headers){
 const out={};for(const [k,v]of headers){const key=k.toLowerCase();if(!DROP_HEADERS.has(key)&&(key==='authorization'||key.startsWith('openai-')||key.startsWith('x-openai-')||['x-request-id','x-correlation-id','traceparent','tracestate','baggage'].includes(key)))out[key]=v}
 return out;
}
function capabilitiesFromBackend(c={}){
 const out={};
 for(const key of ['tools','resources','prompts','completions','logging'])if(c[key]){
  out[key]={...c[key]};
  delete out[key].listChanged;delete out[key].subscribe;
 }
 return out;
}
function decodedRoutingName(value){
 if(typeof value!=='string'||!value||value.length>8192)throw new ProtocolError(-32602,'Missing or invalid Mcp-Name');
 value=value.replace(/^[ \t]+|[ \t]+$/g,'');
 if(!value.startsWith('=?base64?'))return value;
 if(!value.endsWith('?='))throw new ProtocolError(-32602,'Invalid Mcp-Name encoding');
 const encoded=value.slice(9,-2);
 if(!/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(encoded))throw new ProtocolError(-32602,'Invalid Mcp-Name encoding');
 try{return new TextDecoder('utf-8',{fatal:true}).decode(Buffer.from(encoded,'base64'))}catch{throw new ProtocolError(-32602,'Invalid Mcp-Name encoding')}
}
function sanitizedParams(raw){
 const out=structuredClone(raw||{});
 if(plain(out._meta)){for(const key of ENVELOPE_KEYS)delete out._meta[key];if(!Object.keys(out._meta).length)delete out._meta}
 return out;
}
function normalizeModernRouting(raw,headers){
 const wire=structuredClone(raw);
 for(const message of(Array.isArray(wire)?wire:[wire])){
  if(!plain(message)||message.id===undefined)continue;
  if(message.params?._meta?.['io.modelcontextprotocol/protocolVersion']!=='2026-07-28')continue;
  const field=message.method==='resources/read'?'uri':['tools/call','prompts/get'].includes(message.method)?'name':undefined;
  if(field&&plain(message.params)&&message.params[field]===undefined&&typeof headers['mcp-name']==='string')message.params[field]=decodedRoutingName(headers['mcp-name']);
 }
 return wire;
}
function rejectNewSemantics(raw){
 for(const message of(Array.isArray(raw)?raw:[raw])){
  if(!plain(message))continue;const p=message.params;
  if(plain(p)&&('inputResponses'in p||'requestState'in p||'task'in p))throw new ProtocolError(-32602,'This adapter does not support multi-round-trip input or protocol tasks');
 }
}
function jsonError(res,status,code,message){res.writeHead(status,{'content-type':'application/json','cache-control':'no-store'});res.end(JSON.stringify({jsonrpc:'2.0',id:null,error:{code,message}}))}
export async function createAdapter({
 backendUrl='http://127.0.0.1:8876/mcp',expectedBearer,allowUnauthenticatedLoopback=false,
 maxRequestBytes=1024*1024,requestTimeoutMs=180000,responseTimeoutMs=requestTimeoutMs+1000,responseMode='auto',maxConcurrent=32,
 fetchImpl=globalThis.fetch
}={}){
 if(typeof allowUnauthenticatedLoopback!=='boolean')throw new Error('Loopback no-auth flag must be boolean');
 const backend=new URL(backendUrl);
 if(!['http:'].includes(backend.protocol)||backend.hostname!=='127.0.0.1'||backend.username||backend.password||backend.pathname!=='/mcp'||backend.search||backend.hash)throw new Error('Backend must be an uncredentialed loopback HTTP /mcp URL');
 if((typeof expectedBearer!=='string'||!expectedBearer)&&allowUnauthenticatedLoopback!==true)throw new Error('Operator must supply the existing backend bearer token or explicitly retain loopback no-auth');
 if(!Number.isInteger(maxRequestBytes)||maxRequestBytes<1||maxRequestBytes>4*1024*1024)throw new Error('Invalid request size bound');
 if(!Number.isInteger(requestTimeoutMs)||requestTimeoutMs<50||requestTimeoutMs>600000)throw new Error('Invalid timeout bound');
 if(!Number.isInteger(responseTimeoutMs)||responseTimeoutMs<50||responseTimeoutMs>601000)throw new Error('Invalid response deadline');
 if(!['auto','sse','json'].includes(responseMode))throw new Error('Invalid response mode');
 if(!Number.isInteger(maxConcurrent)||maxConcurrent<1||maxConcurrent>128)throw new Error('Invalid concurrency bound');
 let live=0,stopping=false;const sessions=new Set();
 const handler=createMcpHandler(async factory=>{
  const incoming=storage.getStore();if(!incoming)throw new ProtocolError(-32603,'Missing operator request context');
  const forwarded=legacyHeaders(factory.requestInfo.headers);
  let client,server,front,inflight=0,closing=false,closed=false,setupDone=false;const progressContexts=new Map();
  const setupDeadline=Date.now()+Math.min(requestTimeoutMs,20000);
  const setupTimeout=()=>{const remaining=setupDeadline-Date.now();if(incoming.expired||remaining<=0)throw new Error('Adapter setup deadline');return remaining};
  incoming.abortSetup=()=>{if(!setupDone){closing=true;void client?.close().catch(()=>{})}};
  const dispose=async()=>{if(closed||inflight)return;closed=true;sessions.delete(session);await client?.close().catch(()=>{})};
  const session={close:async()=>{closing=true;await dispose()}};
  sessions.add(session);
  const boundedFetch=async(url,init={})=>{
   // Protocol cancellation is not authority to cancel an independently admitted backend job.
   // Explicit job-cancel tools still pass to the backend as ordinary authorized tools/call.
   let parsed;try{if(typeof init.body==='string')parsed=JSON.parse(init.body)}catch{}
   if(parsed?.method==='notifications/cancelled')return new Response(null,{status:202});
   const response=await fetchImpl(url,init);
   const size=Number(response.headers.get('content-length')||0);
   if(size>16*1024*1024)throw new Error('Backend response exceeds adapter bound');
   if(!response.body)return response;
   let total=0;
   const stream=response.body.pipeThrough(new TransformStream({transform(chunk,controller){total+=chunk.byteLength;if(total>16*1024*1024)controller.error(new Error('Backend response exceeds adapter bound'));else controller.enqueue(chunk)}}));
   return new Response(stream,{status:response.status,statusText:response.statusText,headers:response.headers});
  };
  try{
   client=new Client({name:'codex-protocol-adapter',version:'1.0.0'},{capabilities:{}});
   const transport=new StreamableHTTPClientTransport(backend,{
    requestInit:{headers:forwarded},fetch:boundedFetch,
    reconnectionOptions:{maxRetries:0}
   });
   await client.connect(transport,{timeout:setupTimeout()});
   const info=client.getServerVersion()||{name:'codex-bridge',version:'unknown'};
   const caps=capabilitiesFromBackend(client.getServerCapabilities());
   front=new McpServer(info,{capabilities:caps,instructions:client.getInstructions?.(),inputRequired:{legacyShim:false}});
   server=front.server;
   // Populate the SDK's official registry for modern x-mcp-header validation.
   // The wire list/call handlers below remain exact backend passthrough.
   if(caps.tools){
    let cursor,toolCount=0,pages=0;
    do{
     if(++pages>64)throw new Error('Backend tool pagination exceeds adapter bound');
     const listed=await client.request({method:'tools/list',params:{...(cursor?{cursor}:{}),...(sanitizedParams(incoming.messages.find(m=>m?.params)?.params)._meta?{_meta:sanitizedParams(incoming.messages.find(m=>m?.params)?.params)._meta}:{})}},z.looseObject({}),{timeout:setupTimeout()});
     for(const tool of listed.tools||[]){
      if(tool.execution?.taskSupport&&tool.execution.taskSupport!=='forbidden')throw new Error('Backend protocol task execution is not supported by this adapter');
      if(++toolCount>4096)throw new Error('Backend tool count exceeds adapter bound');
      front.registerTool(tool.name,{
       ...(tool.title?{title:tool.title}:{}),...(tool.description?{description:tool.description}:{}),
       inputSchema:fromJsonSchema(tool.inputSchema),
       ...(tool.outputSchema?{outputSchema:fromJsonSchema(tool.outputSchema)}:{}),
       annotations:tool.annotations,_meta:tool._meta
      },async()=>{throw new Error('Passthrough handler not installed')});
     }
     cursor=listed.nextCursor;
    }while(cursor);
   }
   const noSubscriptions={};
   for(const key of ['tools','resources','prompts'])if(caps[key])noSubscriptions[key]={listChanged:false,...(key==='resources'?{subscribe:false}:{})};
   server.registerCapabilities(noSubscriptions);
   client.setNotificationHandler(z.looseObject({method:z.literal('notifications/progress')}),async notification=>{
    const ctx=progressContexts.get(notification.params?.progressToken);if(ctx)await ctx.mcpReq.notify(notification).catch(()=>{});
   });
   server.onclose=()=>{closing=true;void dispose()};
   for(const[method,cap]of Object.entries(METHODS)){
    if(!caps[cap])continue;
    server.setRequestHandler(method,{params:z.looseObject({}),result:z.looseObject({})},async(params,ctx)=>{
     const original=incoming.messages.find(m=>m?.id===ctx.mcpReq.id&&m?.method===method);
     if(!original)throw new ProtocolError(-32603,'Original request correlation missing');
     const outgoing=sanitizedParams(original.params);
     const routingField=method==='resources/read'?'uri':['tools/call','prompts/get'].includes(method)?'name':undefined;
     if(routingField&&outgoing[routingField]===undefined){
      outgoing[routingField]=typeof params[routingField]==='string'?params[routingField]:decodedRoutingName(factory.requestInfo.headers.get('mcp-name'));
     }
     inflight++;const progressToken=outgoing._meta?.progressToken;if(progressToken!==undefined)progressContexts.set(progressToken,ctx);
     try{
      // Deliberately no ctx abort signal: a disconnected HTTP observer must not cancel core work.
      return await client.request({method,params:outgoing},z.looseObject({}),{timeout:requestTimeoutMs});
     }catch(error){
      if(typeof error?.code==='number')throw new ProtocolError(error.code,error.message,error.data);
      throw new ProtocolError(-32603,'Legacy backend request failed');
     }finally{if(progressToken!==undefined)progressContexts.delete(progressToken);inflight--;if(closing)await dispose()}
    });
   }
   setupDone=true;incoming.abortSetup=undefined;return front;
  }catch(error){closing=true;await dispose();throw error}
 },{legacy:'stateless',responseMode,maxRequestBodySize:maxRequestBytes,maxSubscriptions:1,onerror:()=>{}});
 const nodeHandler=toNodeHandler(handler,{maxRequestBodySize:maxRequestBytes,onerror:()=>{}});
 const http=createServer(async(req,res)=>{
  const host=req.headers.host||'';
  if(!/^(127\.0\.0\.1|localhost)(?::([0-9]{1,5}))?$/i.test(host)){jsonError(res,403,-32000,'Host not allowed');return}
  if(req.headers.origin){let origin;try{origin=new URL(req.headers.origin)}catch{jsonError(res,403,-32000,'Origin not allowed');return}
   if(!['http:','https:'].includes(origin.protocol)||!['localhost','127.0.0.1'].includes(origin.hostname)||origin.username||origin.password||origin.pathname!=='/'||origin.search||origin.hash){jsonError(res,403,-32000,'Origin not allowed');return}
  }
  if(stopping){jsonError(res,503,-32603,'Adapter is stopping');return}
  if(req.url!=='/mcp'){jsonError(res,404,-32601,'Not found');return}
  if(allowUnauthenticatedLoopback!==true&&!sameToken(req.headers.authorization||'','Bearer '+expectedBearer)){jsonError(res,401,-32000,'Unauthorized');return}
  if(live>=maxConcurrent){jsonError(res,503,-32603,'Adapter concurrency limit');return}
  live++;let timer;res.on('error',()=>{});
  try{
   const length=Number(req.headers['content-length']||0);
   if(length>maxRequestBytes){jsonError(res,413,-32600,'Request too large');req.resume();return}
   const chunks=[];let size=0;
   for await(const chunk of req){size+=chunk.length;if(size>maxRequestBytes){jsonError(res,413,-32600,'Request too large');return}chunks.push(chunk)}
   let body;
   if(chunks.length){try{body=JSON.parse(Buffer.concat(chunks).toString('utf8'))}catch{jsonError(res,400,-32700,'Invalid JSON');return}}
   if(Array.isArray(body)){jsonError(res,400,-32600,'Batch requests are not supported');return}
   rejectNewSemantics(body);
   const messages=structuredClone(Array.isArray(body)?body:[body]);
   const wireBody=normalizeModernRouting(body,req.headers);
   // Admission timeout affects only this response. No backend cancellation RPC is emitted.
   const admission={messages,expired:false};
   timer=setTimeout(()=>{
    admission.expired=true;admission.abortSetup?.();
    if(res.writableEnded||res.destroyed)return;
    if(!res.headersSent)jsonError(res,504,-32603,'Adapter request timeout');else res.end();
   },responseTimeoutMs);timer.unref();
   await storage.run(admission,()=>nodeHandler(req,res,wireBody));
  }catch(error){
   if(!res.headersSent&&!res.writableEnded)jsonError(res,error instanceof ProtocolError?400:500,typeof error?.code==='number'?error.code:-32603,error instanceof ProtocolError?error.message:'Adapter request failed');
  }finally{clearTimeout(timer);live--}
 });
 http.requestTimeout=Math.min(requestTimeoutMs+5000,605000);http.headersTimeout=15000;http.keepAliveTimeout=5000;
 return{
  server:http,
  async listen(port=18876){if(!Number.isInteger(port)||port<0||port>65535)throw new Error('Invalid loopback port');await new Promise((ok,bad)=>{http.once('error',bad);http.listen(port,'127.0.0.1',ok)});return http.address()},
  async close(){stopping=true;http.closeIdleConnections();await handler.close();await Promise.allSettled([...sessions].map(s=>s.close()));await new Promise(ok=>http.close(ok))},
  limits:{maxRequestBytes,requestTimeoutMs,responseTimeoutMs,maxConcurrent}
 };
}
if(process.argv[1]&&pathToFileURL(path.resolve(process.argv[1])).href===import.meta.url){
 const expectedBearer=process.env.MCP_ADAPTER_BEARER_TOKEN||process.env.CODEX_MCP_BRIDGE_TOKEN||process.env.CODEX_GPT_BRIDGE_TOKEN;
 const allowUnauthenticatedLoopback=process.env.MCP_ADAPTER_ALLOW_LOOPBACK_NO_AUTH==='1';
 const port=Number(process.env.MCP_ADAPTER_PORT||18876);
 const adapter=await createAdapter({backendUrl:process.env.MCP_ADAPTER_BACKEND_URL||'http://127.0.0.1:8878/mcp',expectedBearer,allowUnauthenticatedLoopback});
 await adapter.listen(port);console.log(JSON.stringify({event:'adapter-listening',host:'127.0.0.1',port}));
 for(const signal of['SIGINT','SIGTERM'])process.once(signal,()=>{void adapter.close().then(()=>process.exit(0))});
}
