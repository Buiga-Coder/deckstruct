import { readFileSync } from 'node:fs';
import { Script } from 'node:vm';
const html = readFileSync(new URL('../frontend/index.html', import.meta.url), 'utf8');
const scripts = [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)];
if (!scripts.length) throw new Error('No frontend scripts found');
scripts.forEach((match, i) => new Script(match[1], { filename: `inline-${i}.js` }));
console.log(`Frontend JavaScript syntax OK (${scripts.length} scripts)`);
