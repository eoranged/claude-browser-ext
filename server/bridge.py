#!/usr/bin/env python3
"""Claude Browser Bridge — thin relay between CLI tools and browser extension."""

import asyncio
import json
import os
import platform
import re
import shutil
import signal
import sys
import uuid
from pathlib import Path
from urllib.parse import urlparse, parse_qs

try:
    from websockets.server import ServerProtocol
    from websockets.http11 import Request as WSRequest
    from websockets.frames import Frame, Opcode
except ImportError:
    print("Missing dependency: websockets", file=sys.stderr)
    print("Install with: pip install websockets", file=sys.stderr)
    sys.exit(1)

DEFAULT_PORT = 18321
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

TAB_ROUTE = re.compile(r"^/tabs/(\d+)/(url|dom|eval|screenshot)$")

class BridgeServer:
    def __init__(self, token: str, port: int = DEFAULT_PORT):
        self.token = token
        self.port = port
        self._ws_protocol: ServerProtocol | None = None
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


# --- Main ---

def main():
    global DEBUG
    import argparse
    parser = argparse.ArgumentParser(description="Claude Browser Bridge server")
    parser.add_argument("command", nargs="?", choices=["install", "uninstall", "stop"], help="Subcommand")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Server port (default: {DEFAULT_PORT})")
    parser.add_argument("--token", type=str, help="Auth token (skip interactive prompt)")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    DEBUG = args.debug

    if args.command == "install":
        install()
        return
    if args.command == "uninstall":
        uninstall()
        return
    if args.command == "stop":
        if kill_existing():
            return
        print("No running server found.")
        return
    if args.command is None:
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


if __name__ == "__main__":
    main()
