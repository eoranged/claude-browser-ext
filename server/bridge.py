#!/usr/bin/env python3
"""Claude Browser Bridge — server + CLI.

Server:
    bridge.py                              Start the bridge server
    bridge.py install                      Auto-start on login
    bridge.py uninstall                    Remove auto-start
    bridge.py stop                         Stop running server

CLI client:
    bridge.py status                       Check server & extension status
    bridge.py tabs                         List enabled tabs
    bridge.py url TAB_ID                   Get tab URL
    bridge.py dom TAB_ID                   Get tab DOM (HTML)
    bridge.py eval TAB_ID CODE             Execute JavaScript in tab
    bridge.py screenshot TAB_ID [-o FILE]  Capture tab screenshot
    bridge.py events [options]             Query console/network events
"""

import argparse
import asyncio
import base64
import json
import os
import platform
import re
import shutil
import signal
import sys
import uuid
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

DEFAULT_PORT = 18321
DEFAULT_HOST = "127.0.0.1"
REQUEST_TIMEOUT = 30
DEBUG = False


def log(msg: str) -> None:
    print(msg, flush=True)


def debug(msg: str) -> None:
    if DEBUG:
        print(f"  [debug] {msg}", flush=True)


# --- Config ---

def config_dir() -> Path:
    if platform.system() == "Windows":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    d = base / "claude-browser-bridge"
    d.mkdir(parents=True, exist_ok=True)
    return d


def pid_file() -> Path:
    return config_dir() / "server.pid"


def load_token() -> str | None:
    p = config_dir() / "token"
    if p.exists():
        return p.read_text().strip()
    return None


def save_token(token: str) -> None:
    p = config_dir() / "token"
    p.write_text(token + "\n")
    try:
        p.chmod(0o600)
    except OSError:
        pass


def write_pid() -> None:
    pid_file().write_text(str(os.getpid()))


def kill_existing() -> bool:
    """Kill existing server process if running. Returns True if killed."""
    pf = pid_file()
    if not pf.exists():
        return False
    try:
        pid = int(pf.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        log(f"Stopped existing server (PID {pid})")
        pf.unlink(missing_ok=True)
        import time
        time.sleep(0.5)
        return True
    except (ProcessLookupError, ValueError):
        pf.unlink(missing_ok=True)
        return False
    except PermissionError:
        log(f"Cannot stop existing server (PID {pid}): permission denied")
        return False


# --- HTTP helpers ---

def parse_http_request(raw: bytes) -> dict | None:
    try:
        header_end = raw.index(b"\r\n\r\n")
    except ValueError:
        return None
    header_bytes = raw[:header_end]
    body = raw[header_end + 4:]
    lines = header_bytes.decode("utf-8", errors="replace").split("\r\n")
    request_line = lines[0].split(" ", 2)
    if len(request_line) < 2:
        return None
    method, raw_path = request_line[0], request_line[1]
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    content_length = int(headers.get("content-length", 0))
    if len(body) < content_length:
        return None
    parsed = urlparse(raw_path)
    return {
        "method": method,
        "path": parsed.path,
        "query": parse_qs(parsed.query),
        "headers": headers,
        "body": body[:content_length],
    }


def http_response(status: int, body: dict | str, content_type: str = "application/json") -> bytes:
    status_texts = {200: "OK", 400: "Bad Request", 401: "Unauthorized",
                    404: "Not Found", 502: "Bad Gateway", 504: "Gateway Timeout"}
    text = status_texts.get(status, "Error")
    if isinstance(body, dict):
        body_bytes = json.dumps(body).encode()
    else:
        body_bytes = body.encode()
    return (
        f"HTTP/1.1 {status} {text}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body_bytes)}\r\n"
        f"Access-Control-Allow-Origin: *\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    ).encode() + body_bytes


# --- Server ---

try:
    from websockets.server import ServerProtocol
    from websockets.http11 import Request as WSRequest
    from websockets.frames import Frame, Opcode
    _HAS_WEBSOCKETS = True
except ImportError:
    _HAS_WEBSOCKETS = False

TAB_ROUTE = re.compile(r"^/tabs/(\d+)/(url|dom|eval|screenshot)$")

class BridgeServer:
    def __init__(self, token: str, port: int = DEFAULT_PORT):
        self.token = token
        self.port = port
        self._ws_protocol: "ServerProtocol | None" = None
        self._ws_writer: asyncio.StreamWriter | None = None
        self.ext_authenticated = False
        self.pending: dict[str, asyncio.Future] = {}

    async def start(self):
        server = await asyncio.start_server(self._handle_connection, "127.0.0.1", self.port)
        log(f"Bridge server listening on http://127.0.0.1:{self.port}")
        async with server:
            await server.serve_forever()

    async def _handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            buf = bytearray()
            while b"\r\n\r\n" not in buf:
                chunk = await asyncio.wait_for(reader.read(65536), timeout=10)
                if not chunk:
                    writer.close()
                    return
                buf.extend(chunk)
            raw = bytes(buf)
            req = parse_http_request(raw)
            if not req:
                writer.close()
                return
            if req["headers"].get("upgrade", "").lower() == "websocket":
                debug("WebSocket upgrade request")
                await self._handle_ws_upgrade(raw, reader, writer)
            else:
                debug(f"HTTP {req['method']} {req['path']}")
                await self._handle_http(req, writer)
        except Exception as e:
            debug(f"Connection error: {e}")
            try:
                writer.write(http_response(500, {"error": str(e)}))
                await writer.drain()
            except Exception:
                pass
            writer.close()

    # --- WebSocket via sans-I/O protocol ---

    def _ws_flush(self, writer: asyncio.StreamWriter) -> None:
        """Write all pending protocol output to the transport."""
        for data in self._ws_protocol.data_to_send():
            if data:
                writer.write(data)

    async def _ws_recv(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                       timeout: float | None = None) -> str | None:
        """Read one text message from the WebSocket. Returns None on close/EOF."""
        while True:
            for event in self._ws_protocol.events_received():
                if isinstance(event, Frame):
                    if event.opcode == Opcode.TEXT:
                        return event.data.decode() if isinstance(event.data, (bytes, bytearray)) else event.data
                    elif event.opcode == Opcode.CLOSE:
                        return None
                    elif event.opcode == Opcode.PING:
                        self._ws_protocol.send_pong(event.data)
                        self._ws_flush(writer)
                        await writer.drain()
            chunk = await asyncio.wait_for(reader.read(65536), timeout=timeout or 300)
            if not chunk:
                return None
            self._ws_protocol.receive_data(chunk)

    def _ws_send(self, text: str, writer: asyncio.StreamWriter) -> None:
        """Queue a text message for sending."""
        self._ws_protocol.send_text(text.encode())
        self._ws_flush(writer)

    async def _handle_ws_upgrade(self, raw_request: bytes, reader: asyncio.StreamReader,
                                 writer: asyncio.StreamWriter):
        """Handle WebSocket upgrade using websockets sans-I/O protocol."""
        self._ws_protocol = ServerProtocol()

        # Feed the HTTP request to the protocol
        self._ws_protocol.receive_data(raw_request)

        # The protocol should have parsed the Request — accept it
        for event in self._ws_protocol.events_received():
            if isinstance(event, WSRequest):
                resp = self._ws_protocol.accept(event)
                self._ws_protocol.send_response(resp)

        # Send the 101 handshake response
        self._ws_flush(writer)
        await writer.drain()

        log("Extension connected, awaiting auth...")

        try:
            # Wait for auth
            try:
                auth_msg = await self._ws_recv(reader, writer, timeout=10)
            except asyncio.TimeoutError:
                log("Extension auth timed out")
                return

            if not auth_msg:
                debug("Connection closed before auth")
                return

            debug(f"Auth message: {auth_msg[:200]}")
            data = json.loads(auth_msg)
            if data.get("type") != "auth" or data.get("token") != self.token:
                log("Extension auth failed (token mismatch)")
                self._ws_send(json.dumps({"type": "auth_result", "ok": False}), writer)
                await writer.drain()
                return

            self._ws_send(json.dumps({"type": "auth_result", "ok": True}), writer)
            await writer.drain()
            log("Extension authenticated")

            self._ws_writer = writer
            self.ext_authenticated = True

            # Read responses until disconnect
            while True:
                msg = await self._ws_recv(reader, writer)
                if msg is None:
                    break
                debug(f"WS response: {msg[:200]}")
                self._handle_ws_response(msg)

        except (asyncio.CancelledError, ConnectionError):
            pass
        except Exception as e:
            debug(f"WS error: {e}")
        finally:
            log("Extension disconnected")
            self._ws_writer = None
            self._ws_protocol = None
            self.ext_authenticated = False
            for fut in self.pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("Extension disconnected"))
            self.pending.clear()
            writer.close()

    def _handle_ws_response(self, raw: str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return
        req_id = data.get("id")
        if req_id and req_id in self.pending:
            fut = self.pending.pop(req_id)
            if not fut.done():
                fut.set_result(data)

    async def _send_command(self, command: str, params: dict | None = None) -> dict:
        if not self._ws_writer or not self.ext_authenticated:
            raise ConnectionError("Extension not connected")
        req_id = str(uuid.uuid4())
        msg: dict = {"id": req_id, "command": command}
        if params:
            msg["params"] = params
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self.pending[req_id] = fut
        try:
            self._ws_send(json.dumps(msg), self._ws_writer)
            await self._ws_writer.drain()
            debug(f"Sent command: {command}")
            return await asyncio.wait_for(fut, timeout=REQUEST_TIMEOUT)
        except asyncio.TimeoutError:
            self.pending.pop(req_id, None)
            raise TimeoutError("Extension did not respond in time")

    # --- HTTP routing ---

    async def _handle_http(self, req: dict, writer: asyncio.StreamWriter):
        if req["method"] == "OPTIONS":
            resp = (
                "HTTP/1.1 204 No Content\r\n"
                "Access-Control-Allow-Origin: *\r\n"
                "Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n"
                "Access-Control-Allow-Headers: Authorization, Content-Type\r\n"
                "Content-Length: 0\r\n"
                "Connection: close\r\n"
                "\r\n"
            )
            writer.write(resp.encode())
            await writer.drain()
            writer.close()
            return

        auth = req["headers"].get("authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] != self.token:
            writer.write(http_response(401, {"error": "Unauthorized"}))
            await writer.drain()
            writer.close()
            return

        path = req["path"]
        method = req["method"]
        query = req["query"]

        try:
            if path == "/status" and method == "GET":
                result = {"ok": True, "connected": self.ext_authenticated}

            elif path == "/tabs" and method == "GET":
                resp = await self._send_command("list_tabs")
                if resp.get("error"):
                    raise RuntimeError(resp["error"])
                result = {"tabs": resp["result"]}

            elif path == "/events" and method == "GET":
                params: dict = {}
                if "tab_id" in query:
                    params["tabId"] = int(query["tab_id"][0])
                if "from_id" in query:
                    params["fromId"] = int(query["from_id"][0])
                if "page_size" in query:
                    params["pageSize"] = int(query["page_size"][0])
                if "type" in query:
                    params["type"] = query["type"][0]
                if "level" in query:
                    params["level"] = query["level"][0]
                if "method" in query:
                    params["method"] = query["method"][0]
                if "url_pattern" in query:
                    params["urlPattern"] = query["url_pattern"][0]

                resp = await self._send_command("get_events", params)
                if resp.get("error"):
                    raise RuntimeError(resp["error"])
                result = resp["result"]

            else:
                m = TAB_ROUTE.match(path)
                if not m:
                    writer.write(http_response(404, {"error": "Not found"}))
                    await writer.drain()
                    writer.close()
                    return

                tab_id = int(m.group(1))
                action = m.group(2)

                if action == "url" and method == "GET":
                    resp = await self._send_command("get_url", {"tabId": tab_id})
                    if resp.get("error"):
                        raise RuntimeError(resp["error"])
                    result = {"url": resp["result"]}

                elif action == "dom" and method == "GET":
                    resp = await self._send_command("get_dom", {"tabId": tab_id})
                    if resp.get("error"):
                        raise RuntimeError(resp["error"])
                    result = {"dom": resp["result"]}

                elif action == "eval" and method == "POST":
                    body = json.loads(req["body"].decode()) if req["body"] else {}
                    code = body.get("code", "")
                    if not code:
                        writer.write(http_response(400, {"error": "Missing 'code' in body"}))
                        await writer.drain()
                        writer.close()
                        return
                    resp = await self._send_command("execute", {"tabId": tab_id, "code": code})
                    if resp.get("error"):
                        raise RuntimeError(resp["error"])
                    result = {"result": resp["result"]}

                elif action == "screenshot" and method == "GET":
                    resp = await self._send_command("screenshot", {"tabId": tab_id})
                    if resp.get("error"):
                        raise RuntimeError(resp["error"])
                    result = {"screenshot": resp["result"]}

                else:
                    writer.write(http_response(400, {"error": "Bad request"}))
                    await writer.drain()
                    writer.close()
                    return

            writer.write(http_response(200, result))
        except ConnectionError:
            writer.write(http_response(502, {"error": "Extension not connected"}))
        except TimeoutError:
            writer.write(http_response(504, {"error": "Extension timeout"}))
        except Exception as e:
            writer.write(http_response(502, {"error": str(e)}))

        await writer.drain()
        writer.close()


# --- Install ---

def get_install_dir() -> Path:
    system = platform.system()
    if system == "Windows":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "claude-browser-bridge"
    return Path.home() / ".local" / "bin"


def install():
    system = platform.system()
    script = Path(__file__).resolve()
    install_dir = get_install_dir()
    install_dir.mkdir(parents=True, exist_ok=True)

    if system == "Windows":
        dest = install_dir / "bridge.py"
        shutil.copy2(script, dest)
        vbs = install_dir / "bridge.vbs"
        vbs.write_text(
            f'Set WshShell = CreateObject("WScript.Shell")\n'
            f'WshShell.Run "pythonw ""{dest}""", 0, False\n'
        )
        startup = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
        startup_link = startup / "claude-browser-bridge.vbs"
        shutil.copy2(vbs, startup_link)
        print(f"Installed to {dest}")
        print(f"Autostart: {startup_link}")
    elif system == "Darwin":
        dest = install_dir / "claude-browser-bridge"
        py_dest = install_dir / "claude-browser-bridge.py"
        shutil.copy2(script, py_dest)
        dest.write_text(f"#!/bin/sh\nexec python3 '{py_dest}' \"$@\"\n")
        dest.chmod(0o755)
        plist_dir = Path.home() / "Library" / "LaunchAgents"
        plist_dir.mkdir(parents=True, exist_ok=True)
        plist = plist_dir / "com.claude-browser-bridge.plist"
        plist.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0">\n<dict>\n'
            '  <key>Label</key><string>com.claude-browser-bridge</string>\n'
            f'  <key>ProgramArguments</key><array><string>python3</string><string>{py_dest}</string></array>\n'
            '  <key>RunAtLoad</key><true/>\n'
            '  <key>KeepAlive</key><true/>\n'
            '  <key>StandardOutPath</key><string>/tmp/claude-browser-bridge.log</string>\n'
            '  <key>StandardErrorPath</key><string>/tmp/claude-browser-bridge.log</string>\n'
            '</dict>\n</plist>\n'
        )
        os.system(f"launchctl load '{plist}'")
        print(f"Installed to {dest}")
        print(f"LaunchAgent: {plist}")
    else:
        dest = install_dir / "claude-browser-bridge"
        py_dest = install_dir / "claude-browser-bridge.py"
        shutil.copy2(script, py_dest)
        dest.write_text(f"#!/bin/sh\nexec python3 '{py_dest}' \"$@\"\n")
        dest.chmod(0o755)
        service_dir = Path.home() / ".config" / "systemd" / "user"
        service_dir.mkdir(parents=True, exist_ok=True)
        service = service_dir / "claude-browser-bridge.service"
        service.write_text(
            "[Unit]\nDescription=Claude Browser Bridge\n\n"
            f"[Service]\nExecStart=python3 {py_dest}\nRestart=on-failure\nRestartSec=5\n\n"
            "[Install]\nWantedBy=default.target\n"
        )
        os.system("systemctl --user daemon-reload")
        os.system("systemctl --user enable --now claude-browser-bridge.service")
        print(f"Installed to {dest}")
        print(f"Service: {service}")

    print("Done.")


def uninstall():
    system = platform.system()
    install_dir = get_install_dir()

    if system == "Windows":
        startup = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
        for f in ["bridge.py", "bridge.vbs"]:
            (install_dir / f).unlink(missing_ok=True)
        (startup / "claude-browser-bridge.vbs").unlink(missing_ok=True)
    elif system == "Darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / "com.claude-browser-bridge.plist"
        os.system(f"launchctl unload '{plist}' 2>/dev/null")
        plist.unlink(missing_ok=True)
        (install_dir / "claude-browser-bridge").unlink(missing_ok=True)
        (install_dir / "claude-browser-bridge.py").unlink(missing_ok=True)
    else:
        os.system("systemctl --user disable --now claude-browser-bridge.service 2>/dev/null")
        service = Path.home() / ".config" / "systemd" / "user" / "claude-browser-bridge.service"
        service.unlink(missing_ok=True)
        (install_dir / "claude-browser-bridge").unlink(missing_ok=True)
        (install_dir / "claude-browser-bridge.py").unlink(missing_ok=True)

    print("Uninstalled.")


# --- CLI client ---

def api_request(method: str, path: str, token: str, host: str, port: int,
                body: dict | None = None) -> dict:
    """Make an HTTP request to the bridge server and return parsed JSON."""
    url = f"http://{host}:{port}{path}"
    data = json.dumps(body).encode() if body else None
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    req = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=35) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        try:
            err = json.loads(e.read().decode())
            print(f"Error {e.code}: {err.get('error', e.reason)}", file=sys.stderr)
        except Exception:
            print(f"Error {e.code}: {e.reason}", file=sys.stderr)
        sys.exit(1)
    except URLError as e:
        print(f"Connection failed: {e.reason}", file=sys.stderr)
        print("Is the bridge server running?", file=sys.stderr)
        sys.exit(1)


def cmd_status(args):
    result = api_request("GET", "/status", args.token, args.host, args.port)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        connected = "yes" if result.get("connected") else "no"
        print(f"Server: ok")
        print(f"Extension connected: {connected}")


def cmd_tabs(args):
    result = api_request("GET", "/tabs", args.token, args.host, args.port)
    tabs = result.get("tabs", [])
    if args.json:
        print(json.dumps(tabs, indent=2))
    else:
        if not tabs:
            print("No enabled tabs.")
            return
        for tab in tabs:
            tab_id = tab.get("id", "?")
            title = tab.get("title", "")
            url = tab.get("url", "")
            print(f"  {tab_id}  {title}")
            print(f"       {url}")


def cmd_url(args):
    result = api_request("GET", f"/tabs/{args.tab_id}/url", args.token, args.host, args.port)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(result.get("url", ""))


def cmd_dom(args):
    result = api_request("GET", f"/tabs/{args.tab_id}/dom", args.token, args.host, args.port)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(result.get("dom", ""))


def cmd_eval(args):
    code = args.code
    if code == "-":
        code = sys.stdin.read()
    result = api_request("POST", f"/tabs/{args.tab_id}/eval", args.token, args.host, args.port,
                         body={"code": code})
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        val = result.get("result")
        if val is None:
            pass
        elif isinstance(val, str):
            print(val)
        else:
            print(json.dumps(val, indent=2))


def cmd_screenshot(args):
    result = api_request("GET", f"/tabs/{args.tab_id}/screenshot", args.token, args.host, args.port)
    b64 = result.get("screenshot", "")
    if args.json:
        print(json.dumps(result, indent=2))
        return

    png_data = base64.b64decode(b64)
    if args.output:
        out = Path(args.output)
        out.write_bytes(png_data)
        print(f"Saved to {out} ({len(png_data)} bytes)")
    else:
        sys.stdout.buffer.write(png_data)


def cmd_events(args):
    params = {}
    if args.tab_id is not None:
        params["tab_id"] = args.tab_id
    if args.from_id is not None:
        params["from_id"] = args.from_id
    if args.page_size is not None:
        params["page_size"] = args.page_size
    if args.type is not None:
        params["type"] = args.type
    if args.level is not None:
        params["level"] = args.level
    if args.method is not None:
        params["method"] = args.method
    if args.url_pattern is not None:
        params["url_pattern"] = args.url_pattern

    qs = f"?{urlencode(params)}" if params else ""
    result = api_request("GET", f"/events{qs}", args.token, args.host, args.port)

    if args.json:
        print(json.dumps(result, indent=2))
        return

    events = result.get("events", [])
    if not events:
        print("No events.")
        return

    for ev in events:
        eid = ev.get("id", "?")
        ts = ev.get("timestamp", 0)
        tab = ev.get("tabId", "?")
        etype = ev.get("type", "?")

        if etype == "log":
            level = ev.get("level", "log").upper()
            ev_args = ev.get("args", [])
            parts = [str(a) for a in ev_args]
            print(f"[{eid}] tab:{tab} {level}: {' '.join(parts)}")
        elif etype == "request":
            method = ev.get("method", "?")
            url = ev.get("url", "")
            status = ev.get("status")
            duration = ev.get("duration")
            status_str = str(status) if status else "..."
            dur_str = f" {duration}ms" if duration else ""
            print(f"[{eid}] tab:{tab} {method} {status_str}{dur_str} {url}")
        else:
            print(f"[{eid}] tab:{tab} {etype}: {json.dumps(ev)}")


# --- CLI commands that are also server management ---

def cmd_serve(args):
    """Start the bridge server."""
    if not _HAS_WEBSOCKETS:
        print("Missing dependency: websockets", file=sys.stderr)
        print("Install with: pip install websockets", file=sys.stderr)
        sys.exit(1)

    kill_existing()

    token = args.token or load_token()
    if not token:
        token = input("Enter token from browser extension: ").strip()
        if not token:
            print("Token is required.", file=sys.stderr)
            sys.exit(1)
        save_token(token)
        print(f"Token saved to {config_dir() / 'token'}")

    write_pid()
    server = BridgeServer(token, args.port)
    try:
        asyncio.run(server.start())
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        pid_file().unlink(missing_ok=True)


def cmd_stop(args):
    if kill_existing():
        return
    print("No running server found.")


def cmd_install(args):
    install()


def cmd_uninstall(args):
    uninstall()


# --- Main ---

# CLI subcommands that query the running server
CLI_COMMANDS = {"status", "tabs", "url", "dom", "eval", "screenshot", "events"}

def main():
    global DEBUG

    parser = argparse.ArgumentParser(
        prog="claude-browser-bridge",
        description="Claude Browser Bridge — server and CLI client",
    )
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help=f"Server host (default: {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"Server port (default: {DEFAULT_PORT})")
    parser.add_argument("--token", type=str, default=None,
                        help="Auth token (reads from config if not set)")
    parser.add_argument("--json", action="store_true",
                        help="Output raw JSON responses (CLI commands)")
    parser.add_argument("--debug", action="store_true",
                        help="Enable debug logging (server)")

    sub = parser.add_subparsers(dest="command")

    # --- Server management ---
    sub.add_parser("serve", help="Start the bridge server (default when no command given)")
    sub.add_parser("stop", help="Stop running server")
    sub.add_parser("install", help="Install as auto-start service")
    sub.add_parser("uninstall", help="Remove auto-start service")

    # --- CLI client commands ---
    sub.add_parser("status", help="Check server and extension status")
    sub.add_parser("tabs", help="List enabled browser tabs")

    p = sub.add_parser("url", help="Get tab URL")
    p.add_argument("tab_id", type=int, help="Tab ID")

    p = sub.add_parser("dom", help="Get tab DOM (HTML)")
    p.add_argument("tab_id", type=int, help="Tab ID")

    p = sub.add_parser("eval", help="Execute JavaScript in a tab")
    p.add_argument("tab_id", type=int, help="Tab ID")
    p.add_argument("code", help="JavaScript code to execute (use '-' to read from stdin)")

    p = sub.add_parser("screenshot", help="Capture tab screenshot as PNG")
    p.add_argument("tab_id", type=int, help="Tab ID")
    p.add_argument("-o", "--output", help="Output file (default: stdout)")

    p = sub.add_parser("events", help="Query console logs and network requests")
    p.add_argument("--tab-id", type=int, help="Filter by tab ID")
    p.add_argument("--from-id", type=int, help="Start from event ID")
    p.add_argument("--page-size", type=int, help="Entries per page (default: 20)")
    p.add_argument("--type", choices=["log", "request"], help="Event type filter")
    p.add_argument("--level", choices=["log", "warn", "error", "info", "debug"],
                   help="Log level filter")
    p.add_argument("--method", help="HTTP method filter (GET, POST, etc.)")
    p.add_argument("--url-pattern", help="URL substring filter")

    args = parser.parse_args()
    DEBUG = args.debug

    # Resolve token for CLI commands
    if args.command in CLI_COMMANDS:
        if not args.token:
            args.token = os.environ.get("BRIDGE_TOKEN") or load_token()
        if not args.token:
            print("No token found. Set --token, BRIDGE_TOKEN env var, or run the server first.",
                  file=sys.stderr)
            sys.exit(1)

    dispatch = {
        "serve": cmd_serve,
        "stop": cmd_stop,
        "install": cmd_install,
        "uninstall": cmd_uninstall,
        "status": cmd_status,
        "tabs": cmd_tabs,
        "url": cmd_url,
        "dom": cmd_dom,
        "eval": cmd_eval,
        "screenshot": cmd_screenshot,
        "events": cmd_events,
    }

    if args.command is None:
        cmd_serve(args)
    else:
        dispatch[args.command](args)


if __name__ == "__main__":
    main()
