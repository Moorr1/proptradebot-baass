#!/bin/bash
# Build PropTradeBot as a standalone Mac .app
# Run this on a Mac to create the distributable

set -e

echo "🤖 Building PropTradeBot Mac App..."

# Check dependencies
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 not found. Install from python.org"
    exit 1
fi

# Create build directory
BUILD_DIR="$(pwd)/build"
DIST_DIR="$(pwd)/dist"
mkdir -p "$BUILD_DIR"

# Install dependencies
echo "📦 Installing dependencies..."
pip3 install py2app requests websockets 2>/dev/null || pip install py2app requests websockets

# Create setup.py for py2app
cat > "$BUILD_DIR/setup.py" << 'EOF'
from setuptools import setup

APP = ['gui_app.py']
DATA_FILES = [
    'server_projectx_v2.py',
    'cloud_client.py',
    'config_loader.py',
    'alert_normalizer.py',
    'config.json'
]
OPTIONS = {
    'argv_emulation': True,
    'packages': ['requests', 'websockets'],
    'includes': ['tkinter', 'json', 'subprocess', 'threading', 'datetime'],
    'iconfile': 'icon.icns' if os.path.exists('icon.icns') else None,
    'plist': {
        'CFBundleName': 'PropTradeBot',
        'CFBundleShortVersionString': '1.0.0',
        'CFBundleVersion': '1.0.0',
        'CFBundleIdentifier': 'com.proptradebot.app',
        'LSMinimumSystemVersion': '10.13',
        'NSHighResolutionCapable': True,
    }
}

import os
setup(
    app=APP,
    data_files=DATA_FILES,
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
)
EOF

# Copy files to build directory
cp gui_app.py "$BUILD_DIR/"
cp server_projectx_v2.py "$BUILD_DIR/" 2>/dev/null || echo "⚠️  server_projectx_v2.py not found"
cp cloud_client.py "$BUILD_DIR/" 2>/dev/null || echo "⚠️  cloud_client.py not found"
cp config_loader.py "$BUILD_DIR/" 2>/dev/null || echo "⚠️  config_loader.py not found"
cp alert_normalizer.py "$BUILD_DIR/" 2>/dev/null || echo "⚠️  alert_normalizer.py not found"

# Create default config if not exists
if [ ! -f "config.json" ]; then
    cat > "$BUILD_DIR/config.json" << 'EOF'
{
  "accounts": [],
  "strategy": {
    "contract": "MNQ",
    "contracts_per_entry": 5,
    "t1_target": 20,
    "t1_contracts": 3,
    "t2_target": 40,
    "t2_contracts": 1,
    "runner_target": 60,
    "runner_contracts": 1,
    "stop_loss": 35
  },
  "cloud": {
    "enabled": true,
    "api_url": "https://proptradebot.com"
  }
}
EOF
else
    cp config.json "$BUILD_DIR/"
fi

cd "$BUILD_DIR"

# Build the app
echo "🔨 Building .app bundle..."
python3 setup.py py2app 2>&1 | tail -20

# Check if build succeeded
if [ -d "dist/PropTradeBot.app" ]; then
    echo "✅ Build successful!"
    
    # Create dmg for distribution
    echo "📀 Creating DMG..."
    hdiutil create -volname "PropTradeBot" -srcfolder "dist/PropTradeBot.app" -ov -format UDZO "../PropTradeBot-v1.0.dmg" 2>/dev/null || echo "⚠️  DMG creation skipped (run manually if needed)"
    
    # Also create zip
    echo "📦 Creating ZIP..."
    cd dist
    zip -r "../../PropTradeBot-v1.0-mac.zip" "PropTradeBot.app"
    
    echo ""
    echo "🎉 Done! Distribution files:"
    echo "   - PropTradeBot-v1.0-mac.zip"
    ls -lh "../../PropTradeBot-v1.0-mac.zip" 2>/dev/null || true
else
    echo "❌ Build failed. Check errors above."
    exit 1
fi
