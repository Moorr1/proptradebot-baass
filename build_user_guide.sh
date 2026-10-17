#!/bin/sh
# Builds public/downloads/PropTradeBot-User-Guide.pdf from the site's HTML guides:
# getting-started.html, with nt8-guide.html's content added as an appendix in the same design.
# Run after any change to either page, before pushing. The PDF is never edited by hand.
set -e
cd "$(dirname "$0")"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
T=$(mktemp -d)
python3 - "$T/guide.html" <<'PY'
import re, sys
g = open("public/getting-started.html").read()
n = open("public/nt8-guide.html").read()
body = re.search(r'<div class="container">(.*)</div>\s*</body>', n, re.S).group(1)
body = re.sub(r'<a class="back"[^>]*>.*?</a>', "", body, flags=re.S)
body = re.sub(r"<h1>(.*?)</h1>", r'<h2 id="nt8"><span class="n">APPENDIX</span>\1</h2>', body, flags=re.S)
body = re.sub(r"<h2>(.*?)</h2>", r"<h3>\1</h3>", body, flags=re.S)
body = body.replace('class="box warn"', 'class="warn"').replace('class="box"', 'class="note"').replace('class="subtitle"', 'class="lede"')
assert g.count("<footer>") == 1
g = g.replace("<footer>", '<section class="appendix">' + body + "</section>\n  <footer>")
open(sys.argv[1], "w").write(g)
PY
"$CHROME" --headless=new --disable-gpu --no-pdf-header-footer --print-to-pdf=public/downloads/PropTradeBot-User-Guide.pdf "file://$T/guide.html" 2>/dev/null
ls -la public/downloads/PropTradeBot-User-Guide.pdf
mdls -name kMDItemNumberOfPages public/downloads/PropTradeBot-User-Guide.pdf
