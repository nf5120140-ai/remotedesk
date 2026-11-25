"""
RemoteDesk relay server.

Both computers connect OUT to this server (so no fixed IP / port forwarding
is needed on either side). The server only matches a viewer with a host by
ID and forwards bytes between them. Passwords never reach the server - the
host verifies them itself with a challenge/response.

Run:  python relay.py            (listens on $PORT or 8765)
"""
import asyncio
import hashlib
import json
import logging
import os
import time
from collections import defaultdict

from pathlib import Path

from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Response

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("relay")

MAX_MSG = 8 * 1024 * 1024          # max single message (a JPEG frame)
CONNECT_LIMIT = 20                 # connect attempts per IP per minute


class Host:
    def __init__(self, ws, host_id):
        self.ws = ws
        self.id = host_id
        self.viewer = None          # websocket of the paired viewer


hosts: dict[str, Host] = {}

# ID ownership: id -> sha256(secret). The first computer that registers an ID
# owns it; nobody else can take it over later (prevents ID hijacking).
OWNERS_FILE = os.environ.get("OWNERS_FILE", "owners.json")
try:
    with open(OWNERS_FILE) as f:
        owners: dict[str, str] = json.load(f)
except (OSError, ValueError):
    owners = {}


def save_owners():
    try:
        tmp = OWNERS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(owners, f)
        os.replace(tmp, OWNERS_FILE)
    except OSError as e:
        log.warning("cannot save owners file: %s", e)
attempts: dict[str, list[float]] = defaultdict(list)


def client_ip(ws):
    fwd = ws.request.headers.get("X-Forwarded-For") if ws.request else None
    if fwd:
        return fwd.split(",")[0].strip()
    return ws.remote_address[0] if ws.remote_address else "?"


def rate_limited(ip):
    now = time.time()
    attempts[ip] = [t for t in attempts[ip] if now - t < 60]
    attempts[ip].append(now)
    return len(attempts[ip]) > CONNECT_LIMIT


async def send_json(ws, **msg):
    try:
        await ws.send(json.dumps(msg))
    except ConnectionClosed:
        pass


async def pipe(src, dst):
    """Forward every message from src to dst until src closes."""
    async for msg in src:
        try:
            await dst.send(msg)
        except ConnectionClosed:
            break


async def handle_host(ws, host_id, secret):
    if not (host_id.isdigit() and len(host_id) == 9) or len(secret) < 32:
        await send_json(ws, type="error", message="invalid id")
        return
    digest = hashlib.sha256(secret.encode()).hexdigest()
    if host_id not in owners:
        owners[host_id] = digest
        save_owners()
    elif owners[host_id] != digest:
        await send_json(ws, type="error", code="id_taken", message="id taken")
        return
    old = hosts.get(host_id)
    if old is not None:
        # Same ID re-registering (e.g. reconnect after network drop): replace.
        await old.ws.close()
    host = Host(ws, host_id)
    hosts[host_id] = host
    log.info("host %s online (%s)", host_id, client_ip(ws))
    await send_json(ws, type="registered", id=host_id)
    try:
        async for msg in ws:
            if host.viewer is not None:
                try:
                    await host.viewer.send(msg)
                except ConnectionClosed:
                    pass
            # messages with no viewer attached (keep-alives) are dropped
    finally:
        if hosts.get(host_id) is host:
            del hosts[host_id]
        if host.viewer is not None:
            await host.viewer.close()
        log.info("host %s offline", host_id)


async def handle_viewer(ws, target_id):
    ip = client_ip(ws)
    if rate_limited(ip):
        await send_json(ws, type="error", code="rate", message="too many attempts, wait a minute")
        return
    host = hosts.get(target_id)
    if host is None:
        await send_json(ws, type="error", code="offline", message="remote computer is offline or ID is wrong")
        return
    if host.viewer is not None:
        await send_json(ws, type="error", code="busy", message="remote computer is busy with another session")
        return
    host.viewer = ws
    log.info("viewer %s -> host %s", ip, target_id)
    await send_json(ws, type="connected")
    await send_json(host.ws, type="peer_joined", ip=ip)
    try:
        await pipe(ws, host.ws)
    finally:
        if host.viewer is ws:
            host.viewer = None
            await send_json(host.ws, type="peer_left")
        log.info("viewer %s left host %s", ip, target_id)


async def handler(ws):
    try:
        first = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
        kind = first.get("type")
        if kind == "register":
            await handle_host(ws, str(first.get("id", "")), str(first.get("secret", "")))
        elif kind == "connect":
            await handle_viewer(ws, str(first.get("id", "")))
        else:
            await send_json(ws, type="error", message="bad hello")
    except (asyncio.TimeoutError, ValueError, ConnectionClosed):
        pass


WEB_DIR = Path(__file__).resolve().parent / "web"
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".json": "application/manifest+json", ".png": "image/png",
                 ".svg": "image/svg+xml", ".css": "text/css; charset=utf-8"}


def static_response(status, body, ctype):
    headers = Headers([
        ("Content-Type", ctype),
        ("Content-Length", str(len(body))),
        ("Cache-Control", "no-cache"),
        ("X-Content-Type-Options", "nosniff"),
        ("Referrer-Policy", "no-referrer"),
        ("Content-Security-Policy",
         "default-src 'self'; connect-src 'self' ws: wss:; img-src 'self' blob: data:; "
         "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'"),
    ])
    return Response(status, "OK" if status == 200 else "Not Found", headers, body)


async def http_handler(connection, request):
    """Plain HTTP GET -> serve the mobile web app (and health checks).
    WebSocket upgrades fall through to the relay handler."""
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return None
    path = request.path.split("?", 1)[0]
    if path in ("/", ""):
        path = "/index.html"
    if path == "/health":
        return static_response(200, b"RemoteDesk relay OK\n", "text/plain")
    f = (WEB_DIR / path.lstrip("/")).resolve()
    if WEB_DIR in f.parents and f.is_file():
        return static_response(200, f.read_bytes(),
                               CONTENT_TYPES.get(f.suffix, "application/octet-stream"))
    return static_response(404, b"not found", "text/plain")


async def main():
    port = int(os.environ.get("PORT", 8765))
    async with serve(handler, "0.0.0.0", port, max_size=MAX_MSG,
                     ping_interval=20, ping_timeout=40,
                     process_request=http_handler):
        log.info("relay listening on port %d", port)
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
