#!/usr/bin/env python3
"""CLI client for Claude Browser Bridge.

Usage:
    cli.py status                          Check server & extension status
    cli.py tabs                            List enabled tabs
    cli.py url TAB_ID                      Get tab URL
    cli.py dom TAB_ID                      Get tab DOM (HTML)
    cli.py eval TAB_ID CODE                Execute JavaScript in tab
    cli.py screenshot TAB_ID [-o FILE]     Capture tab screenshot
    cli.py events [options]                Query console/network events
"""

import argparse
import base64
import json
import os
import platform
import sys
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from urllib.parse import urlencode

DEFAULT_PORT = 18321
DEFAULT_HOST = "127.0.0.1"


def config_dir() -> Path:
    if platform.system() == "Windows":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "claude-browser-bridge"


def load_token() -> str | None:
    p = config_dir() / "token"
    if p.exists():
        return p.read_text().strip()
    return None


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


def main():
    parser = argparse.ArgumentParser(
        prog="claude-browser-cli",
        description="CLI client for Claude Browser Bridge",
    )
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help=f"Server host (default: {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"Server port (default: {DEFAULT_PORT})")
    parser.add_argument("--token", type=str, default=None,
                        help="Auth token (reads from config if not set)")
    parser.add_argument("--json", action="store_true",
                        help="Output raw JSON responses")

    sub = parser.add_subparsers(dest="command", required=True)

    # status
    sub.add_parser("status", help="Check server and extension status")

    # tabs
    sub.add_parser("tabs", help="List enabled browser tabs")

    # url
    p = sub.add_parser("url", help="Get tab URL")
    p.add_argument("tab_id", type=int, help="Tab ID")

    # dom
    p = sub.add_parser("dom", help="Get tab DOM (HTML)")
    p.add_argument("tab_id", type=int, help="Tab ID")

    # eval
    p = sub.add_parser("eval", help="Execute JavaScript in a tab")
    p.add_argument("tab_id", type=int, help="Tab ID")
    p.add_argument("code", help="JavaScript code to execute (use '-' to read from stdin)")

    # screenshot
    p = sub.add_parser("screenshot", help="Capture tab screenshot as PNG")
    p.add_argument("tab_id", type=int, help="Tab ID")
    p.add_argument("-o", "--output", help="Output file (default: stdout)")

    # events
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

    # Resolve token
    if not args.token:
        args.token = os.environ.get("BRIDGE_TOKEN") or load_token()
    if not args.token:
        print("No token found. Set --token, BRIDGE_TOKEN env var, or run the server first.",
              file=sys.stderr)
        sys.exit(1)

    # Dispatch
    commands = {
        "status": cmd_status,
        "tabs": cmd_tabs,
        "url": cmd_url,
        "dom": cmd_dom,
        "eval": cmd_eval,
        "screenshot": cmd_screenshot,
        "events": cmd_events,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
