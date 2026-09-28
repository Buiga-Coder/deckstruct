import { readFileSync } from 'node:fs';
import { Script } from 'node:vm';
for (const file of ['index.html', 'admin.html']) {
const html = readFileSync(new URL('../frontend/'+file, import.meta.url), 'utf8');
const scripts = [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)];
if (!scripts.length) throw new Error('No frontend scripts found');
scripts.forEach((match, i) => new Script(match[1], { filename: `inline-${i}.js` }));
console.log(`Frontend JavaScript syntax OK (${scripts.length} scripts)`);
}
new Script(readFileSync(new URL('../frontend/integration.js', import.meta.url),'utf8'));
