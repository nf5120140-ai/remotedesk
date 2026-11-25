"""
RemoteDesk core: networking, authentication, encryption, screen capture,
input injection and file transfer. No GUI code here (see app.py).

Security model
--------------
* Both sides connect out to the relay; the relay only matches IDs.
* The host never stores the password itself, only a PBKDF2 key derived from
  it. Both sides prove knowledge of that key to EACH OTHER with HMAC
  challenge/response (mutual auth - a fake host cannot pretend to be yours).
* After auth, every message is encrypted end-to-end with AES-256-GCM using a
  per-session key, so the relay operator cannot see the screen or keystrokes.
"""
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import struct
import subprocess
import sys
import threading
import time
import zlib
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect as ws_connect

DEFAULT_SERVER = "wss://remotedesk-relay-tdfv.onrender.com"
OLD_DEFAULT_SERVERS = ("ws://localhost:8765",)
IS_WIN = sys.platform == "win32"
if IS_WIN:
    CONFIG_DIR = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "RemoteDesk"
else:
    CONFIG_DIR = Path.home() / ".remotedesk"
CONFIG_FILE = CONFIG_DIR / "config.json"
STATUS_FILE = CONFIG_DIR / "status.json"
KICK_FILE = CONFIG_DIR / "kick.flag"
PBKDF2_ROUNDS = 200_000
FILE_CHUNK = 256 * 1024
MAX_FILE = 4 * 1024 * 1024 * 1024   # 4 GB

MSG_JSON = 0
MSG_FRAME = 1
MSG_FILE = 2

ERRORS = {
    "offline": "המחשב המרוחק כבוי, לא מחובר לאינטרנט, או שהמספר שגוי",
    "busy": "המחשב המרוחק כבר נמצא בחיבור אחר",
    "rate": "יותר מדי ניסיונות. נסה שוב בעוד דקה",
    "wrong_password": "סיסמה שגויה",
    "locked": "המחשב המרוחק ננעל ל-5 דקות בגלל יותר מדי סיסמאות שגויות",
    "impostor": "המחשב המרוחק לא עבר בדיקת זהות (ייתכן ניסיון התחזות)",
    "protocol": "תשובה לא צפויה מהמחשב המרוחק",
    "refused": "החיבור נדחה",
}


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
def new_id():
    return str(secrets.randbelow(900_000_000) + 100_000_000)


def _secure_dir():
    """Create the config dir. On Windows restrict it to SYSTEM + Administrators."""
    new = not CONFIG_DIR.exists()
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if IS_WIN and new:
        try:
            subprocess.run(["icacls", str(CONFIG_DIR), "/inheritance:r",
                            "/grant:r", "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F"],
                           capture_output=True, creationflags=0x08000000)
        except Exception:
            pass


def load_config():
    cfg = {}
    try:
        cfg = json.loads(CONFIG_FILE.read_text("utf-8"))
    except (OSError, ValueError):
        pass
    changed = False
    defaults = {
        "server": DEFAULT_SERVER,
        "id": new_id(),
        "secret": secrets.token_hex(32),
        "key": "",
        "key_id": "",
        "quality": 60,
        "max_width": 1600,
        "fps": 15,
        "monitor": 1,
        "recent": [],
    }
    for k, v in defaults.items():
        if k not in cfg:
            cfg[k] = v
            changed = True
    if cfg.get("server") in OLD_DEFAULT_SERVERS:   # upgrade old default
        cfg["server"] = DEFAULT_SERVER
        changed = True
    if cfg.get("password"):              # migrate old plaintext password
        set_password(cfg, cfg.pop("password"), save=False)
        changed = True
    if changed:
        save_config(cfg)
    return cfg


def save_config(cfg):
    _secure_dir()
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), "utf-8")
    os.replace(tmp, CONFIG_FILE)
    if not IS_WIN:
        os.chmod(CONFIG_FILE, 0o600)


def set_password(cfg, password, save=True):
    cfg["key"] = derive_key(password, cfg["id"]).hex()
    cfg["key_id"] = cfg["id"]
    if save:
        save_config(cfg)


def has_password(cfg):
    return bool(cfg.get("key")) and cfg.get("key_id") == cfg.get("id")


def format_id(i):
    i = str(i)
    return f"{i[0:3]} {i[3:6]} {i[6:9]}"


def default_download_dir():
    return Path.home() / "Desktop" / "RemoteDesk"


def public_download_dir():
    """Shared desktop (visible to every user) - used by the SYSTEM agent."""
    return Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / "Desktop" / "RemoteDesk"


# --------------------------------------------------------------------------
# Crypto helpers
# --------------------------------------------------------------------------
def derive_key(password, host_id):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                               b"remotedesk:" + host_id.encode(), PBKDF2_ROUNDS)


def proof(key, label, *parts):
    return hmac.new(key, label.encode() + b"|" + b"|".join(parts), hashlib.sha256).hexdigest()


class SecureChannel:
    """AES-GCM over a websocket. Direction byte + counter = unique nonce,
    and the counter must strictly increase (replay protection)."""

    def __init__(self, ws, session_key, i_am_host):
        self.ws = ws
        self.aes = AESGCM(session_key)
        self.send_dir = 1 if i_am_host else 2
        self.recv_dir = 2 if i_am_host else 1
        self.send_ctr = 0
        self.recv_ctr = -1
        self.lock = threading.Lock()

    def send(self, kind, payload: bytes):
        with self.lock:
            self.send_ctr += 1
            nonce = struct.pack(">BxxxQ", self.send_dir, self.send_ctr)
            ct = self.aes.encrypt(nonce, bytes([kind]) + payload, None)
            self.ws.send(nonce + ct)

    def send_json(self, **msg):
        self.send(MSG_JSON, json.dumps(msg).encode())

    def decrypt(self, data: bytes):
        if not isinstance(data, (bytes, bytearray)) or len(data) < 29:
            raise ValueError("bad packet")
        nonce, ct = bytes(data[:12]), bytes(data[12:])
        d, ctr = struct.unpack(">BxxxQ", nonce)
        if d != self.recv_dir or ctr <= self.recv_ctr:
            raise ValueError("replay or wrong direction")
        pt = self.aes.decrypt(nonce, ct, None)
        self.recv_ctr = ctr
        kind, body = pt[0], pt[1:]
        if kind == MSG_JSON:
            return kind, json.loads(body)
        return kind, body


# --------------------------------------------------------------------------
# Windows DPI awareness (so screen pixels == mouse pixels)
# --------------------------------------------------------------------------
def set_dpi_aware():
    if IS_WIN:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass


# --------------------------------------------------------------------------
# Input injection (host side)
# --------------------------------------------------------------------------
class InputInjector:
    def __init__(self, cad_hook=None):
        from pynput.keyboard import Controller as KC, Key, KeyCode
        from pynput.mouse import Button, Controller as MC
        self.Key, self.KeyCode, self.Button = Key, KeyCode, Button
        self.kb, self.mouse = KC(), MC()
        self.cad_hook = cad_hook
        self.pressed_keys = {}
        self.pressed_buttons = set()
        K = Key
        self.special = {
            "Return": K.enter, "KP_Enter": K.enter, "BackSpace": K.backspace,
            "Tab": K.tab, "ISO_Left_Tab": K.tab, "Escape": K.esc, "Delete": K.delete,
            "Insert": getattr(K, "insert", None), "Home": K.home, "End": K.end,
            "Prior": K.page_up, "Next": K.page_down, "Left": K.left, "Right": K.right,
            "Up": K.up, "Down": K.down, "space": K.space,
            "Shift_L": K.shift_l, "Shift_R": K.shift_r,
            "Control_L": K.ctrl_l, "Control_R": K.ctrl_r,
            "Alt_L": K.alt_l, "Alt_R": K.alt_r, "Meta_L": K.cmd, "Meta_R": K.cmd,
            "Super_L": K.cmd, "Super_R": K.cmd, "Win_L": K.cmd, "Win_R": K.cmd,
            "Caps_Lock": K.caps_lock, "Menu": getattr(K, "menu", None),
            "Print": getattr(K, "print_screen", None),
            "Num_Lock": getattr(K, "num_lock", None),
            "Pause": getattr(K, "pause", None),
            "Scroll_Lock": getattr(K, "scroll_lock", None),
        }
        for n in range(1, 21):
            if hasattr(K, f"f{n}"):
                self.special[f"F{n}"] = getattr(K, f"f{n}")
        self.buttons = {"left": Button.left, "right": Button.right, "middle": Button.middle}

    def _key(self, name, char):
        k = self.special.get(name)
        if k is not None:
            return k
        if char and len(char) == 1 and ord(char) >= 32:
            return self.KeyCode.from_char(char)
        if name and len(name) == 1:
            return self.KeyCode.from_char(name)
        return None

    def handle(self, msg, mon):
        t = msg.get("type")
        if t == "move":
            x = mon["left"] + max(0.0, min(1.0, float(msg["x"]))) * (mon["width"] - 1)
            y = mon["top"] + max(0.0, min(1.0, float(msg["y"]))) * (mon["height"] - 1)
            self.mouse.position = (int(x), int(y))
        elif t == "button":
            b = self.buttons.get(msg.get("b"))
            if b is None:
                return
            if msg.get("down"):
                self.mouse.press(b)
                self.pressed_buttons.add(b)
            else:
                self.mouse.release(b)
                self.pressed_buttons.discard(b)
        elif t == "scroll":
            self.mouse.scroll(0, int(msg.get("dy", 0)))
        elif t == "key":
            name, char = msg.get("name", ""), msg.get("char", "")
            if msg.get("down"):
                k = self._key(name, char)
                if k is not None:
                    self.kb.press(k)
                    self.pressed_keys[name] = k
            else:
                k = self.pressed_keys.pop(name, None) or self._key(name, char)
                if k is not None:
                    try:
                        self.kb.release(k)
                    except Exception:
                        pass
        elif t == "type_text":
            self.kb.type(str(msg.get("text", ""))[:10000])
        elif t == "cad":
            # Real Ctrl+Alt+Del only works through the system service (SendSAS).
            if self.cad_hook and self.cad_hook():
                return
            with self.kb.pressed(self.Key.ctrl, self.Key.shift):
                self.kb.press(self.Key.esc)
                self.kb.release(self.Key.esc)

    def release_all(self):
        for k in list(self.pressed_keys.values()):
            try:
                self.kb.release(k)
            except Exception:
                pass
        self.pressed_keys.clear()
        for b in list(self.pressed_buttons):
            try:
                self.mouse.release(b)
            except Exception:
                pass
        self.pressed_buttons.clear()


def safe_filename(name):
    name = os.path.basename(str(name).replace("\\", "/")).strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)[:200]
    return name or "file"


class FileReceiver:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.f = None
        self.path = None
        self.remaining = 0

    def begin(self, name, size):
        self.abort()
        size = int(size)
        if size < 0 or size > MAX_FILE:
            raise ValueError("file too big")
        self.folder.mkdir(parents=True, exist_ok=True)
        base = safe_filename(name)
        path = self.folder / base
        stem, suf, n = path.stem, path.suffix, 1
        while path.exists():
            path = self.folder / f"{stem} ({n}){suf}"
            n += 1
        self.path, self.remaining = path, size
        self.f = open(path, "wb")

    def chunk(self, data):
        if self.f is None:
            return
        data = data[: self.remaining]
        self.f.write(data)
        self.remaining -= len(data)

    def end(self):
        if self.f is None:
            return None
        self.f.close()
        self.f = None
        return self.path

    def abort(self):
        if self.f is not None:
            self.f.close()
            try:
                self.path.unlink()
            except OSError:
                pass
            self.f = None


# --------------------------------------------------------------------------
# Host: waits for incoming sessions, streams the screen, applies input
# --------------------------------------------------------------------------
class HostService(threading.Thread):
    """Callbacks (called from background threads):
        on_status(text, online: bool)
        on_session(active: bool, ip: str)
    Hooks (system mode on Windows):
        desktop_hook() -> bool   attach calling thread to the current input
                                 desktop (login screen / UAC). True = changed.
        cad_hook() -> bool       send a real Ctrl+Alt+Del.
    """

    def __init__(self, cfg, on_status=None, on_session=None, desktop_hook=None,
                 cad_hook=None, download_dir=None):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.on_status = on_status or (lambda *a: None)
        self.on_session = on_session or (lambda *a: None)
        self.desktop_hook = desktop_hook or (lambda: False)
        self.cad_hook = cad_hook
        self.files = FileReceiver(download_dir or default_download_dir())
        self.injector = None
        self.stop_flag = threading.Event()
        self.ws = None
        self.chan = None
        self.session_active = threading.Event()
        self.streamer = None
        self.failures = []            # timestamps of failed logins

    # ---- lifecycle
    def stop(self):
        self.stop_flag.set()
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass

    def kick(self):
        """Local user pressed 'Disconnect' - drop the current session."""
        if self.session_active.is_set() and self.chan:
            try:
                self.chan.send_json(type="bye", reason="החיבור נותק על ידי המשתמש במחשב המרוחק")
            except Exception:
                pass
        self._end_session()
        try:
            self.ws.close()
        except Exception:
            pass

    def run(self):
        backoff = 1
        while not self.stop_flag.is_set():
            if not has_password(self.cfg):
                self.on_status("יש להגדיר סיסמה כדי לאפשר גישה מרחוק", False)
                self.stop_flag.wait(1)
                continue
            try:
                self.on_status("מתחבר לשרת...", False)
                with ws_connect(self.cfg["server"], max_size=16 * 1024 * 1024,
                                open_timeout=10, ping_interval=20, ping_timeout=40) as ws:
                    self.ws = ws
                    ws.send(json.dumps({"type": "register", "id": self.cfg["id"],
                                        "secret": self.cfg["secret"]}))
                    reply = json.loads(ws.recv(timeout=15))
                    if reply.get("type") != "registered":
                        if reply.get("code") == "id_taken":
                            self.cfg["id"] = new_id()
                            save_config(self.cfg)
                            continue
                        raise RuntimeError(reply.get("message", "registration failed"))
                    backoff = 1
                    self.on_status("מוכן - ממתין לחיבורים", True)
                    self._loop(ws)
            except Exception as e:
                if self.stop_flag.is_set():
                    break
                self.on_status(f"לא מחובר לשרת ({e.__class__.__name__}) - מנסה שוב...", False)
            finally:
                self._end_session()
                self.ws = None
            self.stop_flag.wait(backoff)
            backoff = min(backoff * 2, 30)

    # ---- protocol
    def _loop(self, ws):
        pending = None
        peer_ip = "?"
        key = bytes.fromhex(self.cfg["key"])
        while not self.stop_flag.is_set():
            msg = ws.recv()
            if self.chan is not None and isinstance(msg, (bytes, bytearray)):
                try:
                    kind, body = self.chan.decrypt(msg)
                except Exception:
                    continue
                if kind == MSG_JSON:
                    self._on_secure(body)
                elif kind == MSG_FILE and self.session_active.is_set():
                    try:
                        self.files.chunk(body)
                    except OSError:
                        self.files.abort()
                continue
            if isinstance(msg, (bytes, bytearray)):
                continue
            try:
                m = json.loads(msg)
            except ValueError:
                continue
            t = m.get("type")
            if t == "peer_joined":
                self._end_session()
                peer_ip = m.get("ip", "?")
                pending = secrets.token_hex(16)
                ws.send(json.dumps({"type": "challenge", "nonce": pending, "version": 2}))
            elif t == "auth" and pending:
                now = time.time()
                self.failures = [f for f in self.failures if now - f < 300]
                locked = len(self.failures) >= 5
                vnonce = str(m.get("vnonce", ""))[:64]
                expected = proof(key, "viewer", pending.encode(), vnonce.encode())
                if not locked and hmac.compare_digest(expected, str(m.get("proof", ""))):
                    sess_key = hmac.new(key, b"session|" + pending.encode() + b"|" + vnonce.encode(),
                                        hashlib.sha256).digest()
                    ws.send(json.dumps({"type": "auth_ok",
                                        "proof": proof(key, "host", pending.encode(), vnonce.encode())}))
                    self.chan = SecureChannel(ws, sess_key, i_am_host=True)
                    pending = None
                    self._start_session(peer_ip)
                else:
                    if not locked:
                        self.failures.append(now)
                    ws.send(json.dumps({"type": "auth_fail",
                                        "code": "locked" if locked else "wrong_password"}))
                    pending = None
            elif t == "peer_left":
                pending = None
                self._end_session()

    def _start_session(self, ip):
        if self.injector is None:
            try:
                self.injector = InputInjector(self.cad_hook)
            except Exception as e:
                self.on_status(f"שליטה בעכבר ובמקלדת לא זמינה: {e}", True)
        self.session_active.set()
        self.on_session(True, ip)
        self.streamer = ScreenStreamer(self)
        self.streamer.start()

    def _end_session(self):
        was = self.session_active.is_set()
        self.session_active.clear()
        if self.streamer:
            self.streamer.stop()
            self.streamer = None
        if self.injector:
            self.injector.release_all()
        self.files.abort()
        self.chan = None
        if was:
            self.on_session(False, "")

    def _on_secure(self, m):
        if not self.session_active.is_set():
            return
        t = m.get("type")
        if t == "ack":
            if self.streamer:
                self.streamer.ack()
        elif t == "settings":
            if self.streamer:
                self.streamer.update_settings(m)
        elif t == "file_begin":
            try:
                self.files.begin(m.get("name", "file"), m.get("size", 0))
            except Exception as e:
                self.chan.send_json(type="file_error", message=str(e))
        elif t == "file_end":
            path = self.files.end()
            if path:
                self.chan.send_json(type="file_done", path=str(path))
        elif self.injector is not None and self.streamer is not None:
            try:
                self.desktop_hook()          # follow login screen / UAC desktop
                self.injector.handle(m, self.streamer.monitor)
            except Exception:
                pass


class ScreenStreamer(threading.Thread):
    MAX_IN_FLIGHT = 2

    def __init__(self, host: HostService):
        super().__init__(daemon=True)
        self.host = host
        self.cfg = host.cfg
        self.quality = int(self.cfg.get("quality", 60))
        self.max_width = int(self.cfg.get("max_width", 1600))
        self.fps = max(1, int(self.cfg.get("fps", 15)))
        self.monitor_index = int(self.cfg.get("monitor", 1))
        self.monitor = {"left": 0, "top": 0, "width": 1, "height": 1}
        self.in_flight = 0
        self.cv = threading.Condition()
        self.stop_flag = threading.Event()
        self.force = True

    def stop(self):
        self.stop_flag.set()
        with self.cv:
            self.cv.notify_all()

    def ack(self):
        with self.cv:
            self.in_flight = max(0, self.in_flight - 1)
            self.cv.notify_all()

    def update_settings(self, m):
        if "quality" in m:
            self.quality = max(10, min(95, int(m["quality"])))
        if "max_width" in m:
            self.max_width = max(320, min(7680, int(m["max_width"])))
        if "monitor" in m:
            self.monitor_index = int(m["monitor"])
        self.force = True

    def run(self):
        import mss
        from PIL import Image
        last_crc = None
        last_send = 0.0
        sct = None
        try:
            while not self.stop_flag.is_set():
                with self.cv:
                    if self.in_flight >= self.MAX_IN_FLIGHT and not self.stop_flag.is_set():
                        self.cv.wait(1.0)
                        if self.in_flight >= self.MAX_IN_FLIGHT:
                            self.in_flight = 0      # lost acks - don't stall forever
                if self.stop_flag.is_set():
                    break
                wait = 1.0 / self.fps - (time.time() - last_send)
                if wait > 0:
                    time.sleep(wait)
                # Desktop switched (login screen / UAC / lock)? -> new capture context
                if self.host.desktop_hook() or sct is None:
                    if sct is not None:
                        sct.close()
                    sct = mss.mss()
                    self.force = True
                try:
                    mons = sct.monitors
                    idx = self.monitor_index if 0 < self.monitor_index < len(mons) else 1
                    if idx >= len(mons):
                        idx = 0
                    mon = mons[idx]
                    if mon != self.monitor:
                        self.monitor = dict(mon)
                        self.force = True
                        self._send_json(type="screen", width=mon["width"], height=mon["height"],
                                        monitors=len(mons) - 1, monitor=idx)
                    shot = sct.grab(mon)
                except Exception:
                    # capture failed (e.g. during a desktop switch) - rebuild and retry
                    try:
                        sct.close()
                    except Exception:
                        pass
                    sct = None
                    time.sleep(0.2)
                    continue
                crc = zlib.crc32(shot.bgra)
                if crc == last_crc and not self.force:
                    last_send = time.time()
                    continue
                last_crc, self.force = crc, False
                img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
                if img.width > self.max_width:
                    h = int(img.height * self.max_width / img.width)
                    img = img.resize((self.max_width, h), Image.BILINEAR)
                buf = io.BytesIO()
                img.save(buf, "JPEG", quality=self.quality)
                try:
                    with self.cv:
                        self.in_flight += 1
                    self.host.chan.send(MSG_FRAME, buf.getvalue())
                except Exception:
                    break
                last_send = time.time()
        finally:
            if sct is not None:
                sct.close()

    def _send_json(self, **m):
        try:
            self.host.chan.send_json(**m)
        except Exception:
            pass


# --------------------------------------------------------------------------
# Viewer: connects to a remote ID
# --------------------------------------------------------------------------
class AuthError(Exception):
    pass


def _err(reply, default="refused"):
    code = reply.get("code") or default
    return AuthError(ERRORS.get(code, reply.get("message") or ERRORS[default]))


class ViewerSession(threading.Thread):
    """Callbacks (background thread):
        on_frame(jpeg_bytes)  - caller MUST call ack() after displaying
        on_message(dict)      - screen info, bye, file_done, ...
        on_closed(reason)
    """

    def __init__(self, server, host_id, password, on_frame, on_message, on_closed):
        super().__init__(daemon=True)
        self.server, self.host_id, self.password = server, host_id.replace(" ", ""), password
        self.on_frame, self.on_message, self.on_closed = on_frame, on_message, on_closed
        self.ws = None
        self.chan = None
        self.closed = False
        self.file_lock = threading.Lock()

    def handshake(self):
        """Blocking: connect + mutual auth. Raises on failure."""
        self.ws = ws_connect(self.server, max_size=16 * 1024 * 1024, open_timeout=10,
                             ping_interval=20, ping_timeout=40)
        ws = self.ws
        ws.send(json.dumps({"type": "connect", "id": self.host_id}))
        r = json.loads(ws.recv(timeout=15))
        if r.get("type") != "connected":
            raise _err(r)
        r = json.loads(ws.recv(timeout=15))
        if r.get("type") != "challenge":
            raise _err(r, "protocol")
        key = derive_key(self.password, self.host_id)
        hnonce = r["nonce"].encode()
        vnonce = secrets.token_hex(16)
        ws.send(json.dumps({"type": "auth", "vnonce": vnonce,
                            "proof": proof(key, "viewer", hnonce, vnonce.encode())}))
        r = json.loads(ws.recv(timeout=15))
        if r.get("type") != "auth_ok":
            raise _err(r, "wrong_password")
        if not hmac.compare_digest(r.get("proof", ""), proof(key, "host", hnonce, vnonce.encode())):
            raise AuthError(ERRORS["impostor"])
        sess_key = hmac.new(key, b"session|" + hnonce + b"|" + vnonce.encode(), hashlib.sha256).digest()
        self.chan = SecureChannel(ws, sess_key, i_am_host=False)

    def run(self):
        reason = "החיבור נסגר"
        try:
            for msg in self.ws:
                if not isinstance(msg, (bytes, bytearray)):
                    continue
                try:
                    kind, body = self.chan.decrypt(msg)
                except Exception:
                    continue
                if kind == MSG_FRAME:
                    self.on_frame(body)
                elif kind == MSG_JSON:
                    if body.get("type") == "bye":
                        reason = body.get("reason", reason)
                    self.on_message(body)
        except ConnectionClosed:
            pass
        except Exception as e:
            reason = str(e)
        finally:
            self.closed = True
            self.on_closed(reason)

    def send(self, **m):
        if self.chan and not self.closed:
            try:
                self.chan.send_json(**m)
            except Exception:
                pass

    def ack(self):
        self.send(type="ack")

    def send_file(self, path, progress=None):
        """Blocking - run in a thread. progress(sent, total)."""
        path = Path(path)
        total = path.stat().st_size
        with self.file_lock:
            self.send(type="file_begin", name=path.name, size=total)
            sent = 0
            with open(path, "rb") as f:
                while not self.closed:
                    data = f.read(FILE_CHUNK)
                    if not data:
                        break
                    self.chan.send(MSG_FILE, data)
                    sent += len(data)
                    if progress:
                        progress(sent, total)
            self.send(type="file_end")

    def close(self):
        self.closed = True
        try:
            self.ws.close()
        except Exception:
            pass
