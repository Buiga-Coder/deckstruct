// Isolated contract checks: temporary SQLite + fake parser, no paid API requests.
const assert=require('node:assert/strict'), fs=require('node:fs'), os=require('node:os'), path=require('node:path'), http=require('node:http');
const {spawn}=require('node:child_process'),{createRequire}=require('node:module');
const apiDir=process.env.API_DIR||path.resolve(__dirname,'../services/api');
const bcrypt=createRequire(path.join(apiDir,'server.js'))('bcryptjs');
const dir=fs.mkdtempSync(path.join(os.tmpdir(),'deckstruct-check-'));
const token='parser-test-token-'.repeat(3),jwt='test-jwt-secret-'.repeat(3);
let child,base;
const fake=http.createServer((req,res)=>{
 res.setHeader('Content-Type','application/json');
 if(req.url==='/healthz')return res.end(JSON.stringify({ok:true,vlmConfigured:true}));
 assert.equal(req.headers.authorization,'Bearer '+token);
 if(req.url==='/jobs'&&req.method==='POST'){
  assert.equal(req.headers['content-type'],'application/octet-stream');assert.ok(Number(req.headers['content-length'])>0);
  let data='';req.on('data',x=>data+=x);req.on('end',()=>{res.statusCode=data==='bad'?400:202;res.end(JSON.stringify({job_id:'a'.repeat(32)}));});return;
 }
 if(req.url.endsWith('/summary'))return res.end(JSON.stringify({slides:2,palette:[],fonts:[]}));
 if(req.url.endsWith('/result'))return res.end(JSON.stringify({slides:[{slide_id:'s1',components:[],preview_url:'/private'}],package_url:'/private'}));
 if(req.url.endsWith('/package')){res.setHeader('Content-Type','application/zip');return res.end('zip fixture');}
 if(req.url.endsWith('.png')){res.setHeader('Content-Type','image/png');return res.end('png fixture');}
 res.end(JSON.stringify({status:'completed',version:'b'.repeat(32),slides:2,exported:'/private/filesystem',archive_sha256:'c'.repeat(64)}));
});
const wait=ms=>new Promise(r=>setTimeout(r,ms));
async function request(route,method='GET',body,auth){
 const r=await fetch(base+route,{method,headers:{...(auth?{Authorization:'Bearer '+auth}:{}),...(body&&!(body instanceof FormData)?{'Content-Type':'application/json'}:{})},body:body instanceof FormData?body:body?JSON.stringify(body):undefined});
 return {status:r.status,data:await r.json().catch(()=>null)};
}
async function start(){
 const s=http.createServer();await new Promise(r=>s.listen(0,'127.0.0.1',r));const port=s.address().port;await new Promise(r=>s.close(r));base='http://127.0.0.1:'+port;
 child=spawn(process.execPath,[path.join(apiDir,'server.js')],{env:{...process.env,PORT:String(port),JWT_SECRET:jwt,DB_PATH:path.join(dir,'db.sqlite'),UPLOAD_DIR:path.join(dir,'uploads'),PARSER_URL:'http://127.0.0.1:'+fake.address().port,PARSER_API_TOKEN:token,ADMIN_EMAIL:'admin@example.test',ADMIN_PASSWORD_HASH:bcrypt.hashSync('TestAdmin123',4)},stdio:['ignore','pipe','pipe']});
 child.stderr.on('data',x=>process.stderr.write(x));
 for(let i=0;i<100;i++){try{if((await request('/healthz')).status===200)return;}catch{}await wait(100);}throw Error('Test API did not start');
}
async function stop(){if(child&&child.exitCode===null){const exited=new Promise(r=>child.once('exit',r));child.kill('SIGTERM');await exited;}}
(async()=>{
 await new Promise(r=>fake.listen(0,'127.0.0.1',r));await start();
 const a=await request('/api/auth/register','POST',{name:'Alice',email:'Alice@example.test',password:'AliceTest123'});assert.equal(a.status,201);assert.equal(a.data.user.role,'user');const at=a.data.token;
 const b=await request('/api/auth/register','POST',{name:'Bob',email:'bob@example.test',password:'BobTest123'}),bt=b.data.token;
 assert.equal((await request('/api/admin/stats','GET',null,at)).status,403);assert.equal((await request('/api/templates')).status,401);
 const admin=await request('/api/auth/login','POST',{email:'admin@example.test',password:'TestAdmin123'});assert.equal(admin.status,200);assert.equal((await request('/api/admin/stats','GET',null,admin.data.token)).status,200);
 const form=new FormData();form.append('file',new Blob(['fixture']),'<unsafe>.pptx');const upload=await request('/api/templates','POST',form,at);assert.equal(upload.status,202);const id=upload.data.id;
 for(const [route,method,body] of [[`/templates/${id}/analysis`,'GET'],[`/templates/${id}/package`,'GET'],[`/templates/${id}/previews/s1.png`,'GET'],[`/templates/${id}/analyze`,'POST'],[`/templates/${id}`,'DELETE'],[`/design-systems/${id}`,'PATCH',{x:1}]])assert.equal((await request('/api'+route,method,body,bt)).status,404);
 const analysis=await request(`/api/templates/${id}/analysis`,'GET',null,at);assert.equal(analysis.data.summary.slides,2);assert.equal(analysis.data.exported,undefined);assert.ok(!JSON.stringify(analysis.data).includes('/private'));
 assert.equal((await request('/api/templates','GET',null,at)).data[0].slides,2);
 assert.equal((await request(`/api/design-systems/${id}`,'PATCH',{palette:['#123456']},at)).status,200);assert.deepEqual((await request(`/api/design-systems/${id}`,'GET',null,at)).data,{palette:['#123456']});
 assert.equal((await request('/api/decks','POST',{title:'Should not generate'},at)).status,503);assert.equal((await request('/api/auth/me','GET',null,at)).data.usage,0);
 const bad=new FormData();bad.append('file',new Blob(['bad']),'invalid.pptx');assert.equal((await request('/api/templates','POST',bad,at)).status,400);assert.equal((await request('/api/templates','GET',null,at)).data.length,1);
 await stop();await start();assert.equal((await request('/api/templates','GET',null,at)).data[0].id,id);
 assert.equal((await request(`/api/templates/${id}`,'DELETE',null,at)).status,200);assert.equal((await request(`/api/design-systems/${id}`,'GET',null,at)).status,404);
 console.log('PASS: auth/admin, ownership, parser contract, persistence, invalid upload, overrides, generation disabled');
})().catch(e=>{console.error(e);process.exitCode=1;}).finally(async()=>{await stop();fake.close();fs.rmSync(dir,{recursive:true,force:true});});
