'use strict';
const express = require('express');
const Database = require('better-sqlite3');
const bcrypt = require('bcryptjs');
const jwt = require('jsonwebtoken');
const multer = require('multer');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { Readable } = require('node:stream');
const { pipeline } = require('node:stream/promises');

const PORT = Number(process.env.PORT || 3000);
const SECRET = process.env.JWT_SECRET;
if (!SECRET || SECRET.length < 32) throw new Error('Set a private JWT_SECRET with at least 32 characters');
const DB_PATH = process.env.DB_PATH || path.join(__dirname, 'data.db');
const UPLOAD_DIR = process.env.UPLOAD_DIR || path.join(__dirname, 'uploads');
const PARSER = process.env.PARSER_URL || 'http://parser:8765';
const PARSER_TOKEN = process.env.PARSER_API_TOKEN;
fs.mkdirSync(path.dirname(DB_PATH), { recursive: true });
fs.mkdirSync(UPLOAD_DIR, { recursive: true });
const db = new Database(DB_PATH);
db.pragma('journal_mode = WAL');
db.pragma('foreign_keys = ON');
db.exec(`
CREATE TABLE IF NOT EXISTS users (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
 company TEXT DEFAULT '', plan TEXT DEFAULT 'Pro', role TEXT DEFAULT 'user', usage INTEGER DEFAULT 0,
 quota INTEGER DEFAULT 40, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS templates (
 id TEXT PRIMARY KEY, owner_id TEXT REFERENCES users(id) ON DELETE CASCADE, name TEXT NOT NULL,
 slides INTEGER DEFAULT 0, size INTEGER DEFAULT 0, file_path TEXT DEFAULT '', created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS decks (
 id TEXT PRIMARY KEY, owner_id TEXT REFERENCES users(id) ON DELETE CASCADE, title TEXT NOT NULL,
 slides INTEGER DEFAULT 0, status TEXT DEFAULT 'pending', template_name TEXT DEFAULT '', created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS design_systems (
 id TEXT PRIMARY KEY, owner_id TEXT REFERENCES users(id) ON DELETE CASCADE, data TEXT NOT NULL, updated_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_templates_owner ON templates(owner_id);
CREATE INDEX IF NOT EXISTS idx_decks_owner ON decks(owner_id);
CREATE TABLE IF NOT EXISTS audit_log (
 id INTEGER PRIMARY KEY, actor TEXT, action TEXT NOT NULL, target TEXT, created_at INTEGER NOT NULL);
`);
const columns = new Set(db.prepare('PRAGMA table_info(templates)').all().map(x => x.name));
for (const [name, type] of [['parser_job_id', 'TEXT'], ['parser_status', "TEXT DEFAULT 'uploaded'"], ['parser_version', 'TEXT'], ['parser_sha256', 'TEXT']]) {
  if (!columns.has(name)) db.exec(`ALTER TABLE templates ADD COLUMN ${name} ${type}`);
}
// Existing mock decks must never be served as real PPTX files.
db.prepare("UPDATE decks SET status='generator_unavailable' WHERE status='done'").run();
const now = Date.now;
const uid = crypto.randomUUID;
const audit = (actor, action, target) => db.prepare('INSERT INTO audit_log(actor,action,target,created_at) VALUES(?,?,?,?)').run(actor, action, target, now());
const pubUser = u => ({ id:u.id, name:u.name, email:u.email, company:u.company, plan:u.plan, role:u.role, usage:u.usage, limit:u.quota, createdAt:u.created_at });
const pubTemplate = t => ({ id:t.id, name:t.name, slides:t.slides, size:t.size, createdAt:t.created_at, status:t.parser_status, parserVersion:t.parser_version });
const pubDeck = d => ({ id:d.id, title:d.title, slides:d.slides, status:d.status, templateName:d.template_name, createdAt:d.created_at });
const sign = u => jwt.sign({ id:u.id }, SECRET, { expiresIn:'24h', algorithm:'HS256' });
const fail = (status, message) => Object.assign(new Error(message), { status });
const wrap = fn => (req, res, next) => Promise.resolve().then(() => fn(req, res)).catch(next);
const emailOf = value => typeof value === 'string' ? value.trim().toLowerCase() : '';
const text = (value, max) => typeof value === 'string' ? value.trim().slice(0, max) : '';
const validEmail = value => /^\S+@\S+\.\S+$/.test(value) && value.length <= 254;

// Bootstrap only from server-side secrets; public registration NEVER grants admin.
const adminEmail = emailOf(process.env.ADMIN_EMAIL);
if (adminEmail && process.env.ADMIN_PASSWORD_HASH && !db.prepare('SELECT 1 FROM users WHERE email=?').get(adminEmail)) {
  db.prepare('INSERT INTO users(id,name,email,password_hash,role,created_at) VALUES(?,?,?,?,?,?)')
    .run(uid(), 'Администратор', adminEmail, process.env.ADMIN_PASSWORD_HASH, 'admin', now());
}

const app = express();
app.disable('x-powered-by');
app.use(express.json({ limit:'1mb' }));
app.use((req, res, next) => { res.set('Cache-Control','no-store'); res.set('X-Content-Type-Options','nosniff'); next(); });
const auth = (req, res, next) => {
  try {
    const value = jwt.verify((req.headers.authorization || '').replace(/^Bearer\s+/i, ''), SECRET, { algorithms:['HS256'] });
    req.user = db.prepare('SELECT * FROM users WHERE id=?').get(value.id);
    if (!req.user) throw new Error();
    next();
  } catch { res.status(401).json({ message:'Войдите в аккаунт' }); }
};
const adminOnly = (req, res, next) => req.user.role === 'admin' ? next() : res.status(403).json({ message:'Нет доступа' });
const attempts = new Map();
const authLimit = (req, res, next) => {
  // Email-based limit avoids trusting spoofed X-Forwarded-For through shared proxies.
  const key = emailOf(req.body?.email) || req.socket.remoteAddress;
  const entry = attempts.get(key);
  const record = entry && entry.until > now() ? entry : { count:0, until:now()+600000 };
  if (++record.count > 30) return res.status(429).json({ message:'Слишком много попыток. Повторите через 10 минут.' });
  attempts.set(key, record);
  if (attempts.size > 10000) for (const [k,v] of attempts) if (v.until < now()) attempts.delete(k);
  next();
};
app.get('/healthz', (req,res) => { db.prepare('SELECT 1').get(); res.json({ ok:true }); });
app.get('/api/capabilities', wrap(async (req,res) => {
  let parser = { available:false, vlmConfigured:false };
  try { const r = await fetch(PARSER+'/healthz', { signal:AbortSignal.timeout(3000) }); if(r.ok) parser = {available:true, ...(await r.json())}; } catch {}
  res.json({ parser, generation:false });
}));
app.post('/api/auth/register', authLimit, wrap(async (req,res) => {
  const name = text(req.body.name,120), email = emailOf(req.body.email), password = req.body.password;
  if (!name || !validEmail(email) || typeof password !== 'string' || password.length < 8 || Buffer.byteLength(password) > 72 || !/\d/.test(password) || !/[A-ZА-Я]/.test(password))
    throw fail(400,'Проверьте имя, email и пароль (8+ символов, цифра, заглавная буква; до 72 байт)');
  if (email === adminEmail || db.prepare('SELECT 1 FROM users WHERE email=?').get(email)) throw fail(409,'Этот email уже занят');
  const user = { id:uid(), name, email, password_hash:await bcrypt.hash(password,10), created_at:now() };
  db.prepare('INSERT INTO users(id,name,email,password_hash,created_at) VALUES(@id,@name,@email,@password_hash,@created_at)').run(user);
  const saved = db.prepare('SELECT * FROM users WHERE id=?').get(user.id);
  res.status(201).json({ token:sign(saved), user:pubUser(saved) });
}));
app.post('/api/auth/login', authLimit, wrap(async (req,res) => {
  const u = db.prepare('SELECT * FROM users WHERE email=?').get(emailOf(req.body.email));
  if (!u || typeof req.body.password !== 'string' || !await bcrypt.compare(req.body.password,u.password_hash)) throw fail(401,'Неверный email или пароль');
  res.json({ token:sign(u), user:pubUser(u) });
}));
app.post('/api/auth/reset', (req,res) => res.status(501).json({ message:'Отправка писем пока не подключена. Обратитесь к администратору.' }));
app.get('/api/auth/me', auth, (req,res) => res.json(pubUser(req.user)));
app.patch('/api/profile', auth, wrap(async (req,res) => {
  const name = req.body.name === undefined ? req.user.name : text(req.body.name,120);
  const email = req.body.email === undefined ? req.user.email : emailOf(req.body.email);
  if (!name || !validEmail(email)) throw fail(400,'Проверьте имя и email');
  if (email !== req.user.email && (email === adminEmail || db.prepare('SELECT 1 FROM users WHERE email=?').get(email))) throw fail(409,'Этот email уже занят');
  db.prepare('UPDATE users SET name=?,email=?,company=? WHERE id=?').run(name,email,req.body.company === undefined ? req.user.company : text(req.body.company,200),req.user.id);
  res.json(pubUser(db.prepare('SELECT * FROM users WHERE id=?').get(req.user.id)));
}));

const owned = (req) => {
  const t = db.prepare('SELECT * FROM templates WHERE id=? AND owner_id=?').get(req.params.id,req.user.id);
  if (!t) throw fail(404,'Шаблон не найден');
  return t;
};
async function parserFetch(route, opts={}) {
  let response;
  try { response = await fetch(PARSER+route, { ...opts, headers:{ Authorization:'Bearer '+PARSER_TOKEN, ...opts.headers }, signal:AbortSignal.timeout(60000) }); }
  catch { throw fail(503,'Парсер временно недоступен. Шаблон сохранён, повторите анализ.'); }
  if (!response.ok) {
    const statuses = {400:400, 404:404, 413:413, 429:429};
    throw fail(statuses[response.status] || 502, response.status === 429 ? 'Очередь парсера заполнена или недостаточно места. Повторите позже.' : response.status === 400 ? 'Парсер отклонил файл или состояние задачи. Проверьте PPTX и настройки API.' : 'Результат парсера пока недоступен');
  }
  return response;
}
async function submitTemplate(t) {
  const size = fs.statSync(t.file_path).size;
  const response = await parserFetch('/jobs', { method:'POST', body:fs.createReadStream(t.file_path), duplex:'half', headers:{'Content-Type':'application/octet-stream','Content-Length':String(size)} });
  const job = await response.json();
  db.prepare("UPDATE templates SET parser_job_id=?,parser_status='queued' WHERE id=?").run(job.job_id,t.id);
  return job;
}
async function syncTemplate(t) {
  if (!t.parser_job_id) return { status:t.parser_status, version:null };
  const raw = await (await parserFetch('/jobs/'+t.parser_job_id)).json();
  db.prepare('UPDATE templates SET parser_status=?,parser_version=?,parser_sha256=?,slides=COALESCE(?,slides) WHERE id=?')
    .run(raw.status,raw.version || null,raw.archive_sha256 || null,raw.slides ?? null,t.id);
  // Explicit allowlist: upstream may include private filesystem paths in state.
  return { status:raw.status, version:raw.version, slides:raw.slides, progress:raw.progress, counts:raw.counts, error:raw.error, archiveSha256:raw.archive_sha256 };
}
const upload = multer({ dest:UPLOAD_DIR, limits:{ fileSize:50*1024*1024, files:1, fields:0 }, fileFilter:(req,file,cb) => cb(file.originalname.toLowerCase().endsWith('.pptx') ? null : fail(400,'Нужен файл .pptx'),true) });
const uploadQuota = (req,res,next) => {
  const usage = db.prepare('SELECT COUNT(*) n, COALESCE(SUM(size),0) bytes FROM templates WHERE owner_id=?').get(req.user.id);
  const total = db.prepare('SELECT COALESCE(SUM(size),0) bytes FROM templates').get().bytes;
  if (usage.n >= 30 || usage.bytes >= 300*1024*1024 || total >= 1024*1024*1024) return res.status(429).json({message:'Достигнут лимит хранения шаблонов. Удалите ненужные файлы.'});
  next();
};
app.get('/api/templates', auth, (req,res) => res.json(db.prepare('SELECT * FROM templates WHERE owner_id=? ORDER BY created_at DESC').all(req.user.id).map(pubTemplate)));
app.post('/api/templates', auth, uploadQuota, upload.single('file'), wrap(async (req,res) => {
  if (!req.file) throw fail(400,'Файл не получен');
  const id = uid();
  db.prepare('INSERT INTO templates(id,owner_id,name,size,file_path,created_at) VALUES(?,?,?,?,?,?)').run(id,req.user.id,path.parse(req.file.originalname).name.slice(0,120),req.file.size,req.file.path,now());
  const t = db.prepare('SELECT * FROM templates WHERE id=?').get(id);
  try { await submitTemplate(t); }
  catch (e) {
    if ([400,413].includes(e.status)) { db.prepare('DELETE FROM templates WHERE id=?').run(id); fs.unlinkSync(t.file_path); throw e; }
    db.prepare("UPDATE templates SET parser_status='submission_failed' WHERE id=?").run(id);
  }
  res.status(202).json(pubTemplate(db.prepare('SELECT * FROM templates WHERE id=?').get(id)));
}));
app.get('/api/templates/:id/analysis', auth, wrap(async (req,res) => {
  const t = owned(req);
  const state = await syncTemplate(t);
  let summary = null, result = null;
  if(t.parser_job_id) {
    try { summary = await (await parserFetch(`/jobs/${t.parser_job_id}/summary`)).json(); } catch(e) { if(e.status !== 404) throw e; }
    if(state.version) result = await (await parserFetch(`/jobs/${t.parser_job_id}/versions/${state.version}/result`)).json();
  }
  if(result) { result.slides.forEach(s => { s.preview_url=`/api/templates/${t.id}/previews/${s.slide_id}.png`; }); result.package_url=`/api/templates/${t.id}/package`; }
  res.json({ template:pubTemplate(db.prepare('SELECT * FROM templates WHERE id=?').get(t.id)), ...state, summary, result });
}));
app.post('/api/templates/:id/analyze', auth, wrap(async (req,res) => {
  const t = owned(req);
  if (!t.parser_job_id) await submitTemplate(t);
  else {
    const state = await syncTemplate(t);
    if (['partial','failed','interrupted','awaiting_configuration'].includes(state.status)) {
      await parserFetch(`/jobs/${t.parser_job_id}/resume`,{method:'POST'});
      db.prepare("UPDATE templates SET parser_status='queued' WHERE id=?").run(t.id);
    }
  }
  res.status(202).json(pubTemplate(db.prepare('SELECT * FROM templates WHERE id=?').get(t.id)));
}));
async function sendParserFile(res, route, mime) {
  const response = await parserFetch(route);
  res.type(mime);
  if(response.headers.get('content-length')) res.set('Content-Length',response.headers.get('content-length'));
  await pipeline(Readable.fromWeb(response.body),res);
}
app.get('/api/templates/:id/previews/:slide', auth, wrap(async (req,res) => {
  const t = owned(req);
  if(!t.parser_job_id || !/^s[1-9][0-9]*\.png$/.test(req.params.slide)) throw fail(404,'Превью не найдено');
  await sendParserFile(res,`/jobs/${t.parser_job_id}/previews/${req.params.slide}`,'image/png');
}));
app.get('/api/templates/:id/package', auth, wrap(async (req,res) => {
  const t = owned(req), state = await syncTemplate(t);
  if(!state.version) throw fail(409,'Пакет анализа ещё не готов');
  res.set('Content-Disposition','attachment; filename="template-analysis.zip"');
  await sendParserFile(res,`/jobs/${t.parser_job_id}/versions/${state.version}/package`,'application/zip');
}));
function deleteTemplate(t) {
  db.transaction(() => { db.prepare('DELETE FROM design_systems WHERE id=? AND owner_id=?').run(t.id,t.owner_id); db.prepare('DELETE FROM templates WHERE id=?').run(t.id); })();
  if(t.file_path && path.dirname(path.resolve(t.file_path)) === path.resolve(UPLOAD_DIR)) { try {fs.unlinkSync(t.file_path);} catch {} }
  // Parser snapshots remain server-private for recovery; see documented retention policy.
}
app.delete('/api/templates/:id', auth, wrap(async (req,res) => { deleteTemplate(owned(req)); res.json({ok:true}); }));

app.get('/api/design-systems/:id', auth, wrap(async (req,res) => {
  const t=owned(req);
  const row=db.prepare('SELECT data FROM design_systems WHERE id=? AND owner_id=?').get(t.id,req.user.id);
  res.json(row ? JSON.parse(row.data) : {});
}));
app.patch('/api/design-systems/:id', auth, wrap(async (req,res) => {
  const t=owned(req);
  if (!req.body || Array.isArray(req.body) || typeof req.body !== 'object') throw fail(400,'Ожидается JSON-объект');
  // User overrides are separate from immutable parser facts consumed by the generator.
  const data=JSON.stringify(req.body);
  db.prepare('INSERT INTO design_systems(id,owner_id,data,updated_at) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data,updated_at=excluded.updated_at').run(t.id,req.user.id,data,now());
  res.json(req.body);
}));
app.get('/api/decks', auth, (req,res) => res.json(db.prepare('SELECT * FROM decks WHERE owner_id=? ORDER BY created_at DESC').all(req.user.id).map(pubDeck)));
app.post('/api/decks', auth, (req,res) => res.status(503).json({code:'GENERATOR_NOT_CONNECTED',message:'Генератор презентаций ещё не подключён. Анализ шаблонов доступен.'}));
app.get('/api/decks/:id/download', auth, (req,res) => res.status(503).json({code:'GENERATOR_NOT_CONNECTED',message:'Готового PPTX пока нет: генератор ещё не подключён.'}));
app.delete('/api/decks/:id', auth, (req,res) => {db.prepare('DELETE FROM decks WHERE id=? AND owner_id=?').run(req.params.id,req.user.id);res.json({ok:true});});

app.get('/api/admin/stats', auth, adminOnly, (req,res) => {
  const count=t=>db.prepare(`SELECT COUNT(*) n FROM ${t}`).get().n;
  res.json({users:count('users'),decks:count('decks'),templates:count('templates'),generations:db.prepare('SELECT COALESCE(SUM(usage),0) n FROM users').get().n});
});
const TABLES={users:'users',templates:'templates',decks:'decks'};
app.get('/api/admin/:kind', auth, adminOnly, (req,res) => {
  const t=TABLES[req.params.kind]; if(!t)return res.sendStatus(404);
  const cols={users:'id,name,email,role,usage,quota,created_at',templates:'id,owner_id,name,slides,size,created_at,parser_status',decks:'id,owner_id,title,slides,status,template_name,created_at'};
  res.json(db.prepare(`SELECT ${cols[t]} FROM ${t} ORDER BY created_at DESC`).all());
});
app.patch('/api/admin/users/:id', auth, adminOnly, (req,res) => {
  if(!['user','admin'].includes(req.body.role) || req.params.id===req.user.id)return res.status(400).json({message:'Нельзя изменить эту роль'});
  const updated=db.prepare('UPDATE users SET role=? WHERE id=?').run(req.body.role,req.params.id);
  if(!updated.changes)return res.status(404).json({message:'Пользователь не найден'});
  audit(req.user.id,'role:'+req.body.role,req.params.id);res.json({ok:true});
});
app.delete('/api/admin/:kind/:id', auth, adminOnly, wrap(async (req,res) => {
  const t=TABLES[req.params.kind];if(!t)throw fail(404,'Не найдено');
  if(t==='users' && req.params.id===req.user.id)throw fail(400,'Нельзя удалить себя');
  if(t==='templates') {const row=db.prepare('SELECT * FROM templates WHERE id=?').get(req.params.id);if(row)deleteTemplate(row);}
  else {if(t==='users')db.prepare('SELECT * FROM templates WHERE owner_id=?').all(req.params.id).forEach(deleteTemplate);db.prepare(`DELETE FROM ${t} WHERE id=?`).run(req.params.id);}
  audit(req.user.id,'delete:'+t,req.params.id);res.json({ok:true});
}));
app.use((req,res)=>res.status(404).json({message:'Маршрут не найден'}));
app.use((err,req,res,next)=>{
  if(res.headersSent)return next(err);
  const status=err.code==='LIMIT_FILE_SIZE'?413:err instanceof multer.MulterError?400:err.status || 500;
  if(status>=500)console.error('Request failed:',req.method,req.path,err.name);
  res.status(status).json({message:status===413?'Файл больше 50 МБ':status===500?'Ошибка сервера':err.message});
});
const server=app.listen(PORT,'0.0.0.0',()=>console.log('Deckstruct API listening on '+PORT));
process.on('SIGTERM',()=>server.close(()=>{db.close();process.exit(0);}));
