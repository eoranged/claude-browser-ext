# Claude Browser Bridge

Expose browser tabs to CLI tools via a local HTTP API. A browser extension connects to a local Python server over WebSocket; CLI tools query the server over HTTP.

## Setup

### Server

Requires Python 3.10+ and the `websockets` package.

```bash
cd server
pip install -r requirements.txt
python bridge.py
# First run prompts for the auth token from the extension
```

The server listens on `http://127.0.0.1:18321`.

Options:
```
python bridge.py --port 18322    # custom port
python bridge.py --token TOKEN   # skip interactive prompt
python bridge.py --debug         # verbose logging
python bridge.py stop            # stop running server
python bridge.py install         # auto-start on login (launchd/systemd)
python bridge.py uninstall       # remove auto-start
```

### Extension

Load the extension from `extension/dist/`:
- **Chrome**: `chrome://extensions` → Load unpacked → select `dist/chrome-mv3`
- **Firefox**: `about:debugging` → Load Temporary Add-on → select any file in `dist/firefox-mv2`

Click the extension icon to open the popup. Copy the auth token and pass it to the server. Click the power button on any tab to enable it for API access.

### Building the extension

```bash
cd extension
npm install
npm run build           # Chrome
npm run build:firefox   # Firefox
```

## API

All endpoints require `Authorization: Bearer <token>` header.

| Endpoint | Method | Description |
|---|---|---|
| `/status` | GET | `{"ok": true, "connected": bool}` |
| `/tabs` | GET | List enabled tabs |
| `/tabs/{id}/url` | GET | Get tab URL |
| `/tabs/{id}/dom` | GET | Get tab HTML |
| `/tabs/{id}/eval` | POST | Execute JS (`{"code": "..."}`) |
| `/tabs/{id}/screenshot` | GET | Capture tab as base64 PNG |
| `/events` | GET | Query console logs and network requests |

### Events query parameters

| Param | Description |
|---|---|
| `tab_id` | Filter by tab ID |
| `from_id` | Start from this event ID (inclusive) |
| `page_size` | Entries per page (default: 20) |
| `type` | `log` or `request` |
| `level` | `log`, `warn`, `error`, `info`, `debug` |
| `method` | `GET`, `POST`, etc. |
| `url_pattern` | Substring match on request URL |

### Examples

```bash
TOKEN=<your-token>

curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:18321/status
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:18321/tabs
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:18321/tabs/1/dom
curl -H "Authorization: Bearer $TOKEN" -X POST -d '{"code":"document.title"}' http://127.0.0.1:18321/tabs/1/eval
curl -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:18321/events?type=log&level=error"
```
