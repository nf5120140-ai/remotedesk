"""CI test on a real Windows machine: install the SYSTEM service from the built
exe, wait for it to come online, connect as a viewer, grab a frame, inject a
mouse move, then uninstall."""
import json, os, subprocess, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core  # noqa: E402

EXE = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2]).resolve()
OUT.mkdir(parents=True, exist_ok=True)
PASSWORD = "ci-test-password-1"


def dump_logs():
    for f in ("service.log", "status.json"):
        p = core.CONFIG_DIR / f
        if p.exists():
            print(f"----- {f} -----\n{p.read_text('utf-8', 'replace')}")


cfg = core.load_config()
cfg["server"] = "ws://127.0.0.1:8765"
core.set_password(cfg, PASSWORD)
print("host id:", cfg["id"])

r = subprocess.run([str(EXE), "--install-service"], timeout=120)
print("install exit code:", r.returncode)
print(subprocess.run(["sc.exe", "query", "RemoteDesk"], capture_output=True, text=True).stdout)

ok = False
for _ in range(60):
    try:
        st = json.loads(core.STATUS_FILE.read_text("utf-8"))
        if st.get("online"):
            ok = True
            print("agent status:", st)
            break
    except (OSError, ValueError):
        pass
    time.sleep(1)
if not ok:
    dump_logs()
    sys.exit("FAIL: agent never came online")

frames, msgs = [], []
v = core.ViewerSession(cfg["server"], cfg["id"], PASSWORD, None, None, None)
v.on_frame = lambda d: (frames.append(d), v.ack())
v.on_message = msgs.append
v.on_closed = lambda r: print("closed:", r)
v.handshake()
v.start()
for _ in range(30):
    if frames:
        break
    time.sleep(0.5)
print("screen msgs:", [m for m in msgs if m.get("type") == "screen"])
if not frames:
    dump_logs()
    sys.exit("FAIL: no frames from the SYSTEM agent")
(OUT / "remote_screen.jpg").write_bytes(frames[-1])
print("frame bytes:", len(frames[-1]))

# mouse injection from the SYSTEM agent
import ctypes  # noqa: E402
class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
v.send(type="move", x=0.25, y=0.5)
time.sleep(1.5)
p = POINT(); ctypes.windll.user32.GetCursorPos(ctypes.byref(p))
print("cursor after remote move:", p.x, p.y)

# wrong password must be rejected (session is busy, so use a 2nd host? just check busy)
v2 = core.ViewerSession(cfg["server"], cfg["id"], "wrong", None, None, None)
try:
    v2.handshake()
    sys.exit("FAIL: second viewer accepted")
except core.AuthError as e:
    print("second viewer rejected:", e)
v.close()
time.sleep(2)

r = subprocess.run([str(EXE), "--uninstall-service"], timeout=120)
print("uninstall exit code:", r.returncode)
dump_logs()
log = (core.CONFIG_DIR / "service.log").read_text("utf-8", "replace")
assert "service started (version 1.0." in log, "version not stamped into exe"
# ---- installer: fresh install, then upgrade OVER a running copy, then uninstall
setup = EXE.parent / "RemoteDesk-Setup.exe"
if setup.exists():
    def run_setup(label):
        r = subprocess.run([str(setup), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/TASKS=service"],
                           timeout=300)
        st = subprocess.run(["sc.exe", "query", "RemoteDesk"], capture_output=True, text=True).stdout
        print(f"{label}: setup exit={r.returncode} service running={'RUNNING' in st}")
        assert r.returncode == 0 and "RUNNING" in st, f"{label} failed"
    run_setup("fresh install")
    time.sleep(8)                       # agent is now running from Program Files
    subprocess.Popen([r"C:\Program Files\RemoteDesk\RemoteDesk.exe"])   # and the window is open
    time.sleep(5)
    run_setup("upgrade over running copy")
    unins = Path(r"C:\Program Files\RemoteDesk\unins000.exe")
    r = subprocess.run([str(unins), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], timeout=300)
    time.sleep(3)
    st = subprocess.run(["sc.exe", "query", "RemoteDesk"], capture_output=True, text=True)
    print("uninstall: exit", r.returncode, "service removed:", st.returncode != 0)
    assert st.returncode != 0, "service still installed after uninstall"
print("PASS")
