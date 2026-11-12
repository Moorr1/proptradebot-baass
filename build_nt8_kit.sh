#!/bin/sh
# Builds public/downloads/nt8/PropTradeBot-NT8-Setup-Guide.pdf (from nt8-guide.html, light print style, ONE disclosure
# footer) and public/downloads/nt8/PTBBridge-Setup-Kit.zip = the UNCHANGED PTBBridge.zip (NinjaTrader import archive;
# never add files to it) + ptbbridge.cfg.example + the guide PDF + START-HERE.txt. Run after changing nt8-guide.html.
set -e
cd "$(dirname "$0")"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
T=$(mktemp -d); D=public/downloads/nt8
python3 - "$T/nt8.html" <<'PY'
import sys
t = open("public/nt8-guide.html").read()
# Inline the shared stylesheet: the PDF must not depend on the live site (or a relative /guide.css under file://).
t = t.replace('<link rel="stylesheet" href="/guide.css">', "<style>" + open("public/guide.css").read() + "</style>")
css = ("<style>@media print{*{color:#111 !important;background:transparent !important;box-shadow:none !important;border-color:#bbb !important}"
       "a{color:#1d4ed8 !important}pre,code{background:#f1f5f9 !important}.nt-badge img{filter:none}nav,.back{display:none !important}}</style>")
assert t.count("</head>") == 1
open(sys.argv[1], "w").write(t.replace("</head>", css + "</head>").replace('href="/', 'href="https://proptradebot.com/').replace('src="/', 'src="file://' + __import__("os").path.abspath("public") + '/'))
PY
"$CHROME" --headless=new --disable-gpu --no-pdf-header-footer --print-to-pdf="$D/PropTradeBot-NT8-Setup-Guide.pdf" "file://$T/nt8.html" 2>/dev/null
cat > "$T/START-HERE.txt" <<'TXT'
PropTradeBot - NinjaTrader 8 setup kit

1. Read PropTradeBot-NT8-Setup-Guide.pdf. Pick option A (PropTradeBot on the same Windows PC as NinjaTrader)
   or option B (PropTradeBot on a Mac, NinjaTrader on Windows).
2. Make your settings file from ptbbridge.cfg.example, as the guide explains.
3. Install the add-on: in NinjaTrader, Tools -> Import -> NinjaScript Add-On..., and choose PTBBridge.zip.
   Do NOT unzip PTBBridge.zip - NinjaTrader imports it as it is.

The latest version of this guide: https://proptradebot.com/nt8-guide.html
Before you start, confirm that your prop firm allows automated trading through NinjaTrader 8 on your accounts.
Futures trading involves substantial risk. Risk Disclosure: https://proptradebot.com/disclosures.html
TXT
cp "$D/PTBBridge.zip" "$D/ptbbridge.cfg.example" "$D/PropTradeBot-NT8-Setup-Guide.pdf" "$T/"
rm -f "$D/PTBBridge-Setup-Kit.zip"
(cd "$T" && zip -q -X "PTBBridge-Setup-Kit.zip" START-HERE.txt PropTradeBot-NT8-Setup-Guide.pdf PTBBridge.zip ptbbridge.cfg.example)
cp "$T/PTBBridge-Setup-Kit.zip" "$D/"
unzip -l "$D/PTBBridge-Setup-Kit.zip"
