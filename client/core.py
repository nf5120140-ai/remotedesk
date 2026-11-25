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
import platform
import re
import secrets
import struct
import subprocess
import sys
import threading
import time
import zlib
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect as ws_connect

DEFAULT_SERVER = "wss://remote.naftalikempe.com"
# Other addresses of the SAME relay service. If the main address is blocked
# (e.g. by an internet filter) or unreachable, these are tried automatically.
RELAY_ALIASES = ("wss://remote.naftalikempe.com", "wss://remotedesk-relay-tdfv.onrender.com")
OLD_DEFAULT_SERVERS = ("ws://localhost:8765", "wss://remotedesk-relay-tdfv.onrender.com")
_last_good = {}


def relay_candidates(server):
    """The configured address first (or the one that worked last time), then its aliases."""
    if server not in RELAY_ALIASES:
        return [server]
    order = [_last_good.get("url", server), server] + list(RELAY_ALIASES)
    seen, out = set(), []
    for u in order:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def connect_relay(server, **kw):
    """Open a websocket to the relay, falling back to its other addresses."""
    last = None
    for url in relay_candidates(server):
        try:
            ws = ws_connect(url, **kw)
            _last_good["url"] = url
            return ws
        except Exception as e:          # blocked / DNS / down -> try the next address
            last = e
    raise last
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
    "not_trusted": "המחשב הזה כבר לא מורשה להיכנס למחשב המרוחק. יש להתחבר שוב עם סיסמה או אישור",
    "declined": "המשתמש במחשב המרוחק דחה את הבקשה",
    "no_approver": "אין מי שיאשר במחשב המרוחק (אף אחד לא מחובר אליו). התחבר עם סיסמה",
    "approval_timeout": "לא התקבל אישור מהמחשב המרוחק בזמן",
    "too_many_requests": "נשלחו יותר מדי בקשות אישור. נסה שוב בעוד כמה דקות",
}

APPROVAL_TIMEOUT = 45


def explain_connection_error(e):
    """Human (Hebrew) explanation for a failed connection to the relay."""
    status = getattr(getattr(e, "response", None), "status_code", None)
    name = e.__class__.__name__
    if status is not None:
        if status in (403, 418, 451):
            return f"הגישה לשרת נחסמה (קוד {status}) - כנראה סינון אינטרנט. יש לבקש לאשר את הכתובת של השרת"
        if status in (502, 503, 504, 520, 521, 522, 523, 524):
            return f"השרת מתעורר או מתעדכן (קוד {status}) - מנסה שוב..."
        if status == 200:
            return "חיבור חי (WebSocket) לשרת נחסם בדרך - כנראה סינון אינטרנט"
        return f"השרת דחה את החיבור (קוד {status})"
    if isinstance(e, TimeoutError) or "Timeout" in name:
        return "אין תשובה מהשרת - בדוק את החיבור לאינטרנט"
    if isinstance(e, OSError):
        return "אין חיבור לאינטרנט או לשרת"
    return f"לא מחובר לשרת ({name})"


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
        "device_id": secrets.token_hex(16),
        "device_name": platform.node() or "Computer",
        "trusted": {},      # viewers allowed into THIS computer: device_id -> {key, name, added}
        "saved": {},        # computers THIS computer can enter: host_id -> {name, trust_key}
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


def ecdh_keypair():
    priv = ec.generate_private_key(ec.SECP256R1())
    pub = priv.public_key().public_bytes(serialization.Encoding.X962,
                                         serialization.PublicFormat.UncompressedPoint)
    return priv, pub


def ecdh_key(priv, peer_pub_hex, hnonce: bytes, vnonce: bytes):
    peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), bytes.fromhex(peer_pub_hex))
    shared = priv.exchange(ec.ECDH(), peer)
    return hashlib.sha256(b"remotedesk-ecdh|" + shared + b"|" + hnonce + b"|" + vnonce).digest()


def verification_code(key):
    """6-digit code shown on both screens - must match (detects a man in the middle)."""
    n = int.from_bytes(hashlib.sha256(b"code|" + key).digest()[:4], "big") % 1_000_000
    return f"{n:06d}"[:3] + " " + f"{n:06d}"[3:]


def session_key(key, hnonce: bytes, vnonce: bytes):
    return hmac.new(key, b"session|" + hnonce + b"|" + vnonce, hashlib.sha256).digest()


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
# One host per computer (the service agent and a window must never both host)
# --------------------------------------------------------------------------
HOST_LOCK_NAME = "Global\\RemoteDeskHostLock"


def acquire_host_lock():
    """Returns a handle if this process may host, None if another copy already does."""
    if not IS_WIN:
        return True
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateMutexW.restype = wintypes.HANDLE
    k.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    h = k.CreateMutexW(None, False, HOST_LOCK_NAME)
    err = ctypes.get_last_error()
    if not h:                       # access denied -> exists, owned by SYSTEM (the service)
        return None
    if err == 183:                  # ERROR_ALREADY_EXISTS
        k.CloseHandle(h)
        return None
    return h


def release_host_lock(h):
    if IS_WIN and h and h is not True:
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32")
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.CloseHandle(h)


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
                 cad_hook=None, download_dir=None, on_request=None):
        """on_request(info) -> (accept, always): called (blocking, from this
        thread) when a remote computer asks for access without a password.
        info = {name, ip, code}. None = nobody here to approve."""
        super().__init__(daemon=True)
        self.on_request = on_request
        self.requests = []
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
        lock = None
        while not self.stop_flag.is_set():
            lock = acquire_host_lock()
            if lock:
                break
            self.on_status("RemoteDesk כבר מקבל חיבורים במחשב הזה (בשירות המערכת)", True)
            self.stop_flag.wait(5)
        try:
            self._run()
        finally:
            release_host_lock(lock)

    def _run(self):
        backoff = 1
        while not self.stop_flag.is_set():
            if not has_password(self.cfg) and not self.cfg.get("trusted") and self.on_request is None:
                self.on_status("יש להגדיר סיסמה כדי לאפשר גישה מרחוק", False)
                self.stop_flag.wait(1)
                continue
            try:
                self.on_status("מתחבר לשרת...", False)
                with connect_relay(self.cfg["server"], max_size=16 * 1024 * 1024,
                                open_timeout=10, ping_interval=20, ping_timeout=40) as ws:
                    self.ws = ws
                    ws.send(json.dumps({"type": "register", "id": self.cfg["id"],
                                        "secret": self.cfg["secret"]}))
                    reply = json.loads(ws.recv(timeout=15))
                    if reply.get("type") != "registered":
                        if reply.get("code") == "duplicate":
                            self.on_status("RemoteDesk פועל פעמיים במחשב הזה. סגור עותקים ישנים או הפעל מחדש את המחשב", False)
                            self.stop_flag.wait(60)
                            continue
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
                self.on_status(explain_connection_error(e) + " · מנסה שוב...", False)
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
                ws.send(json.dumps({"type": "challenge", "nonce": pending, "version": 3,
                                    "name": self.cfg.get("device_name", ""),
                                    "methods": ["password", "trust", "request"]}))
            elif t == "auth" and pending:
                hn, vn = pending.encode(), str(m.get("vnonce", ""))[:64].encode()
                pending = None
                self._authenticate(ws, m, hn, vn, key, peer_ip)
            elif t == "peer_left":
                pending = None
                self._end_session()

    def _authenticate(self, ws, m, hn, vn, pw_key, peer_ip):
        now = time.time()
        self.failures = [f for f in self.failures if now - f < 300]
        locked = len(self.failures) >= 5
        method = m.get("method", "password")
        device = str(m.get("device", ""))[:64]
        dev_name = str(m.get("device_name", "") or "מחשב לא ידוע")[:60]

        def fail(code, count=True):
            if count and not locked:
                self.failures.append(now)
            ws.send(json.dumps({"type": "auth_fail", "code": code}))

        grant = False
        if method == "password":
            if locked:
                return fail("locked")
            if not hmac.compare_digest(proof(pw_key, "viewer", hn, vn), str(m.get("proof", ""))):
                return fail("wrong_password")
            key = pw_key
            grant = bool(m.get("remember")) and bool(device)
        elif method == "trust":
            if locked:
                return fail("locked")
            entry = load_config().get("trusted", {}).get(device)
            if not entry:
                return fail("not_trusted", count=False)
            key = bytes.fromhex(entry["key"])
            if not hmac.compare_digest(proof(key, "viewer", hn, vn), str(m.get("proof", ""))):
                return fail("not_trusted")
        elif method == "request":
            self.requests = [r for r in self.requests if now - r < 120]
            if self.on_request is None:
                return fail("no_approver", count=False)
            if len(self.requests) >= 3:
                return fail("too_many_requests", count=False)
            self.requests.append(now)
            try:
                priv, pub = ecdh_keypair()
                key = ecdh_key(priv, str(m.get("pub", "")), hn, vn)
            except Exception:
                return fail("protocol", count=False)
            ws.send(json.dumps({"type": "pending", "pub": pub.hex()}))
            try:
                accept, always = self.on_request({"name": dev_name, "ip": peer_ip,
                                                  "code": verification_code(key)})
            except Exception:
                accept, always = False, False
            if accept is None:
                return fail("approval_timeout", count=False)
            if not accept:
                return fail("declined", count=False)
            grant = bool(always) and bool(device)
        else:
            return fail("protocol", count=False)

        ws.send(json.dumps({"type": "auth_ok", "proof": proof(key, "host", hn, vn)}))
        self.chan = SecureChannel(ws, session_key(key, hn, vn), i_am_host=True)
        if grant:
            tk = secrets.token_bytes(32)
            cfg = load_config()
            cfg.setdefault("trusted", {})[device] = {"key": tk.hex(), "name": dev_name,
                                                     "added": int(now)}
            save_config(cfg)
            self.chan.send_json(type="trust_grant", key=tk.hex(),
                                host_name=self.cfg.get("device_name", ""))
        self._start_session(peer_ip)

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
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def _err(reply, default="refused"):
    code = reply.get("code") or default
    return AuthError(ERRORS.get(code, reply.get("message") or ERRORS[default]), code)


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
        self.remote_name = ""
        self.file_lock = threading.Lock()

    def handshake(self, method="password", trust_key=None, remember=False,
                  device_id="", device_name="", on_code=None):
        """Blocking: connect + mutual auth. Raises AuthError on failure.
        method: "password" | "trust" (saved computer) | "request" (ask the
        other side to approve). on_code(code) is called while waiting for
        approval so the user can compare the verification code."""
        self.ws = connect_relay(self.server, max_size=16 * 1024 * 1024, open_timeout=10,
                             ping_interval=20, ping_timeout=40)
        ws = self.ws
        ws.send(json.dumps({"type": "connect", "id": self.host_id}))
        r = json.loads(ws.recv(timeout=15))
        if r.get("type") != "connected":
            raise _err(r)
        r = json.loads(ws.recv(timeout=15))
        if r.get("type") != "challenge":
            raise _err(r, "protocol")
        self.remote_name = r.get("name", "")
        hn, vn = r["nonce"].encode(), secrets.token_hex(16)
        msg = {"type": "auth", "method": method, "vnonce": vn,
               "device": device_id, "device_name": device_name}
        if method == "password":
            key = derive_key(self.password, self.host_id)
            msg.update(proof=proof(key, "viewer", hn, vn.encode()), remember=bool(remember))
        elif method == "trust":
            key = bytes.fromhex(trust_key)
            msg["proof"] = proof(key, "viewer", hn, vn.encode())
        else:
            priv, pub = ecdh_keypair()
            msg["pub"] = pub.hex()
        ws.send(json.dumps(msg))
        r = json.loads(ws.recv(timeout=15))
        if method == "request":
            if r.get("type") != "pending":
                raise _err(r, "protocol")
            key = ecdh_key(priv, r["pub"], hn, vn.encode())
            if on_code:
                on_code(verification_code(key))
            r = json.loads(ws.recv(timeout=APPROVAL_TIMEOUT + 15))
        if r.get("type") != "auth_ok":
            raise _err(r, "wrong_password")
        if not hmac.compare_digest(r.get("proof", ""), proof(key, "host", hn, vn.encode())):
            raise AuthError(ERRORS["impostor"], "impostor")
        self.chan = SecureChannel(ws, session_key(key, hn, vn.encode()), i_am_host=False)

    def cancel(self):
        self.close()

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
