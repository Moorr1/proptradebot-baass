// Tell Bing (and through it ChatGPT search, Yandex, Seznam...) that site pages changed.
//   node indexnow.js            # submits every URL in public/sitemap.xml
//   node indexnow.js /a.html    # submits just these paths
// The key is public by design (IndexNow checks it at https://proptradebot.com/<key>.txt);
// it only proves we own the site. Re-run after adding or changing a page and deploying it.
const fs = require('fs');
const path = require('path');
const https = require('https');

const HOST = 'proptradebot.com';
const pub = path.join(__dirname, 'public');
const key = fs.readdirSync(pub).find((f) => /^[0-9a-f]{32}\.txt$/.test(f)).slice(0, -4);
const args = process.argv.slice(2);
const urlList = args.length
  ? args.map((p) => `https://${HOST}${p.startsWith('/') ? p : '/' + p}`)
  : [...fs.readFileSync(path.join(pub, 'sitemap.xml'), 'utf8').matchAll(/<loc>([^<]+)<\/loc>/g)].map((m) => m[1]);

const body = JSON.stringify({ host: HOST, key, keyLocation: `https://${HOST}/${key}.txt`, urlList });
const req = https.request({ host: 'api.indexnow.org', path: '/indexnow', method: 'POST',
  headers: { 'Content-Type': 'application/json; charset=utf-8', 'Content-Length': Buffer.byteLength(body) } },
(res) => { let b = ''; res.on('data', (c) => (b += c)); res.on('end', () => {
  console.log(`IndexNow ${res.statusCode} for ${urlList.length} URLs${b ? ': ' + b : ''}`);
  process.exit(res.statusCode === 200 || res.statusCode === 202 ? 0 : 1);
}); });
req.on('error', (e) => { console.error('IndexNow failed:', e.message); process.exit(1); });
req.end(body);
