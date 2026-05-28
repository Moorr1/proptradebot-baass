# PropTradeBot

Automated futures trading bot for prop firm accounts.

## Requirements

- Python 3.9+
- macOS (tested on MacBook Pro)
- Chrome Extension (for Discord signal scraping)

## Setup

1. Install dependencies:
   ```bash
   pip install requests websockets
   ```

2. Set your API key:
   ```bash
   export PTB_API_KEY=your_api_key_from_dashboard
   ```

3. Configure your accounts in `config.json`

4. Run the bot:
   ```bash
   python server_projectx_v2.py
   ```

## Chrome Extension

1. Download `proptradebot-connector-v4.zip`
2. Unzip it
3. Open Chrome → Extensions → Developer Mode ON
4. Click "Load unpacked" → select the unzipped folder
5. Configure the extension with your bot URL (default: http://localhost:5555)

## Support

Email: support@proptradebot.com
