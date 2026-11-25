"""
Windows system mode - full administrator / SYSTEM access, like AnyDesk.

Architecture
------------
  RemoteDesk service  (runs as LocalSystem, starts with Windows, before login)
        │  watches which session is on the physical console
        ▼
  RemoteDesk.exe --agent   (SYSTEM process launched INTO the console session)
        * follows the "input desktop": normal desktop, login screen,
          lock screen and UAC prompts - so everything can be seen and controlled
        * asks the service for a real Ctrl+Alt+Del (SendSAS)
        * shows a visible "being controlled" banner with a Disconnect button

The GUI (RemoteDesk.exe without arguments) runs elevated, installs/removes the
service, edits the password and shows the agent's status.
"""
import ctypes
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

IS_WIN = sys.platform == "win32"
SERVICE_NAME = "RemoteDesk"
SERVICE_DISPLAY = "RemoteDesk - שליטה מרחוק"
SAS_EVENT = "Global\\RemoteDeskSAS"
NO_WINDOW = 0x08000000
INSTALL_DIR = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "RemoteDesk"
INSTALLED_EXE = INSTALL_DIR / "RemoteDesk.exe"


def log(msg):
    try:
        import core
        core._secure_dir()
        with open(core.CONFIG_DIR / "service.log", "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except Exception:
        pass


# --------------------------------------------------------------------------
# Elevation
# --------------------------------------------------------------------------
def is_admin():
    if not IS_WIN:
        return os.geteuid() == 0
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin():
    """Restart this program with a UAC prompt. Returns True if launched."""
    if getattr(sys, "frozen", False):
        exe, params = sys.executable, sys.argv[1:]
    else:
        exe = sys.executable.replace("python.exe", "pythonw.exe")
        params = [os.path.abspath(sys.argv[0])] + sys.argv[1:]
    r = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe,
                                            subprocess.list2cmdline(params), None, 1)
    return r > 32


# --------------------------------------------------------------------------
# Follow the input desktop (Default / Winlogon / UAC secure desktop)
# --------------------------------------------------------------------------
_tls = threading.local()
if IS_WIN:
    from ctypes import wintypes
    _u32 = ctypes.WinDLL("user32", use_last_error=True)
    _u32.OpenInputDesktop.restype = wintypes.HANDLE
    _u32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _u32.SetThreadDesktop.restype = wintypes.BOOL
    _u32.SetThreadDesktop.argtypes = [wintypes.HANDLE]
    _u32.CloseDesktop.restype = wintypes.BOOL
    _u32.CloseDesktop.argtypes = [wintypes.HANDLE]
    _u32.GetUserObjectInformationW.restype = wintypes.BOOL
    _u32.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                               wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.OpenEventW.restype = wintypes.HANDLE
    _k32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    _k32.SetEvent.argtypes = [wintypes.HANDLE]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]


def follow_input_desktop():
    """Attach the calling thread to whatever desktop currently receives input.
    Returns True when the thread switched desktop (caller must rebuild any
    screen-capture context)."""
    if not IS_WIN:
        return False
    h = _u32.OpenInputDesktop(0, False, 0x10000000)   # GENERIC_ALL
    if not h:
        return False
    buf = ctypes.create_unicode_buffer(256)
    needed = wintypes.DWORD()
    _u32.GetUserObjectInformationW(h, 2, buf, ctypes.sizeof(buf), ctypes.byref(needed))
    name = buf.value
    if getattr(_tls, "name", None) == name:
        _u32.CloseDesktop(h)
        return False
    if _u32.SetThreadDesktop(h):
        old = getattr(_tls, "handle", None)
        if old:
            _u32.CloseDesktop(old)
        _tls.handle, _tls.name = h, name
        return True
    _u32.CloseDesktop(h)
    return False


def request_sas():
    """Ask the service to send a real Ctrl+Alt+Del. True if the service got it."""
    if not IS_WIN:
        return False
    h = _k32.OpenEventW(0x0002, False, SAS_EVENT)      # EVENT_MODIFY_STATE
    if not h:
        return False
    _k32.SetEvent(h)
    _k32.CloseHandle(h)
    return True


# --------------------------------------------------------------------------
# Service (LocalSystem)
# --------------------------------------------------------------------------
def _launch_agent_in_session(session_id):
    import win32api
    import win32con
    import win32process
    import win32profile
    import win32security
    tok = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_ALL_ACCESS)
    dup = win32security.DuplicateTokenEx(tok, win32security.SecurityImpersonation,
                                         win32con.TOKEN_ALL_ACCESS, win32security.TokenPrimary)
    win32security.SetTokenInformation(dup, win32security.TokenSessionId, session_id)
    si = win32process.STARTUPINFO()
    si.lpDesktop = "winsta0\\default"
    env = win32profile.CreateEnvironmentBlock(dup, False)
    cmd = subprocess.list2cmdline([sys.executable, "--agent"])
    hp, ht, pid, tid = win32process.CreateProcessAsUser(
        dup, None, cmd, None, None, False,
        NO_WINDOW | 0x00000400,          # CREATE_UNICODE_ENVIRONMENT
        env, str(Path(sys.executable).parent), si)
    ht.Close()
    log(f"agent started pid={pid} session={session_id}")
    return hp


def _supervise(stop_event):
    import win32api
    import win32event
    import win32security
    import win32ts
    # SAS event: only SYSTEM (the agent) may signal it
    sa = win32security.SECURITY_ATTRIBUTES()
    sd = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
        "D:(A;;GA;;;SY)(A;;GA;;;BA)", win32security.SDDL_REVISION_1)
    sa.SECURITY_DESCRIPTOR = sd
    sas_event = win32event.CreateEvent(sa, False, False, SAS_EVENT)
    proc, sess = None, None
    log("service started")
    while True:
        r = win32event.WaitForMultipleObjects([stop_event, sas_event], False, 2000)
        if r == win32event.WAIT_OBJECT_0:
            break
        if r == win32event.WAIT_OBJECT_0 + 1:
            try:
                ctypes.WinDLL("sas.dll").SendSAS(False)
                log("SendSAS")
            except Exception as e:
                log(f"SendSAS failed: {e}")
            continue
        cur = win32ts.WTSGetActiveConsoleSessionId()
        if cur == 0xFFFFFFFF:
            continue
        alive = proc is not None and win32event.WaitForSingleObject(proc, 0) == win32event.WAIT_TIMEOUT
        if cur != sess or not alive:
            if alive:
                win32api.TerminateProcess(proc, 0)
            try:
                proc, sess = _launch_agent_in_session(cur), cur
            except Exception as e:
                log(f"agent launch failed: {e}")
                proc = None
                time.sleep(5)
    if proc is not None:
        try:
            win32api.TerminateProcess(proc, 0)
        except Exception:
            pass
    log("service stopped")


def run_service():
    import servicemanager
    import win32event
    import win32service
    import win32serviceutil

    class RemoteDeskService(win32serviceutil.ServiceFramework):
        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = SERVICE_DISPLAY

        def __init__(self, args):
            super().__init__(args)
            self.stop_event = win32event.CreateEvent(None, True, False, None)

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            win32event.SetEvent(self.stop_event)

        def SvcDoRun(self):
            _supervise(self.stop_event)

    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(RemoteDeskService)
    servicemanager.StartServiceCtrlDispatcher()


# --------------------------------------------------------------------------
# Install / uninstall / status (called from the elevated GUI)
# --------------------------------------------------------------------------
def _sc(*args):
    return subprocess.run(["sc.exe", *args], capture_output=True, text=True,
                          creationflags=NO_WINDOW)


def service_state():
    """'running' / 'stopped' / 'missing'"""
    if not IS_WIN:
        return "missing"
    r = _sc("query", SERVICE_NAME)
    if r.returncode != 0:
        return "missing"
    return "running" if "RUNNING" in r.stdout else "stopped"


def install_service():
    if not getattr(sys, "frozen", False):
        raise RuntimeError("התקנה כשירות מערכת אפשרית רק מקובץ RemoteDesk.exe (הרץ את build_windows.bat)")
    if service_state() != "missing":
        uninstall_service()
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    if Path(sys.executable).resolve() != INSTALLED_EXE.resolve():
        shutil.copy2(sys.executable, INSTALLED_EXE)
    # allow services to generate Ctrl+Alt+Del (SendSAS)
    subprocess.run(["reg", "add", r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System",
                    "/v", "SoftwareSASGeneration", "/t", "REG_DWORD", "/d", "3", "/f"],
                   capture_output=True, creationflags=NO_WINDOW)
    # firewall: outbound is allowed by default, nothing to open
    bin_path = f'"{INSTALLED_EXE}" --service'
    r = _sc("create", SERVICE_NAME, "binPath=", bin_path, "start=", "auto",
            "obj=", "LocalSystem", "DisplayName=", SERVICE_DISPLAY)
    if r.returncode != 0:
        raise RuntimeError(r.stdout + r.stderr)
    _sc("description", SERVICE_NAME, "RemoteDesk remote control - full system access")
    _sc("failure", SERVICE_NAME, "reset=", "60", "actions=", "restart/3000/restart/3000/restart/10000")
    # Start-menu / desktop shortcut for the control panel
    _make_shortcut()
    r = _sc("start", SERVICE_NAME)
    if r.returncode != 0 and "1056" not in r.stdout:
        raise RuntimeError(r.stdout + r.stderr)


def uninstall_service():
    _sc("stop", SERVICE_NAME)
    for _ in range(20):
        if service_state() != "running":
            break
        time.sleep(0.5)
    _sc("delete", SERVICE_NAME)


def _make_shortcut():
    try:
        common = Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / "Desktop" / "RemoteDesk.lnk"
        ps = (f'$s=(New-Object -ComObject WScript.Shell).CreateShortcut("{common}");'
              f'$s.TargetPath="{INSTALLED_EXE}";$s.Save()')
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True,
                       creationflags=NO_WINDOW)
    except Exception:
        pass


# --------------------------------------------------------------------------
# Agent (SYSTEM process in the console session)
# --------------------------------------------------------------------------
def run_agent():
    import core
    core.set_dpi_aware()
    state = {"online": False, "text": "", "session": False, "ip": ""}
    banner = {}

    def write_status():
        try:
            data = dict(state, id=core.load_config()["id"], time=time.time())
            tmp = core.STATUS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
            os.replace(tmp, core.STATUS_FILE)
        except Exception:
            pass

    def on_status(text, ok):
        state.update(text=text, online=ok)
        write_status()

    def on_session(active, ip):
        state.update(session=active, ip=ip)
        write_status()
        if "root" in banner:
            banner["root"].after(0, show_banner, active, ip)

    def make_host():
        cfg = core.load_config()
        h = core.HostService(cfg, on_status, on_session, desktop_hook=follow_input_desktop,
                             cad_hook=request_sas, download_dir=core.public_download_dir())
        h.start()
        return h

    host = [make_host()]
    mtime = [core.CONFIG_FILE.stat().st_mtime if core.CONFIG_FILE.exists() else 0]

    def housekeeping():
        # Disconnect requested from the GUI
        if core.KICK_FILE.exists():
            try:
                core.KICK_FILE.unlink()
            except OSError:
                pass
            threading.Thread(target=host[0].kick, daemon=True).start()
        # Password / server changed in the GUI -> restart host
        try:
            m = core.CONFIG_FILE.stat().st_mtime
        except OSError:
            m = 0
        if m != mtime[0]:
            mtime[0] = m
            new = core.load_config()
            old = host[0].cfg
            if any(new.get(k) != old.get(k) for k in ("server", "key", "key_id", "id")):
                host[0].stop()
                host[0] = make_host()

    try:
        import tkinter as tk
    except ImportError:
        tk = None

    if tk is None:
        while True:
            housekeeping()
            time.sleep(0.5)

    root = tk.Tk()
    root.withdraw()
    banner["root"] = root
    win = tk.Toplevel(root)
    win.overrideredirect(True)
    win.attributes("-topmost", True)
    win.configure(bg="#c62828")
    label = tk.Label(win, text="", bg="#c62828", fg="white", font=("Segoe UI", 11, "bold"))
    label.pack(side="right", padx=10, pady=4)
    tk.Button(win, text="נתק", command=lambda: threading.Thread(target=host[0].kick, daemon=True).start(),
              bg="white", fg="#c62828", relief="flat", font=("Segoe UI", 10, "bold")
              ).pack(side="left", padx=6, pady=4)
    win.withdraw()

    def show_banner(active, ip):
        if active:
            label.config(text=f"⚠ המחשב נשלט מרחוק ({ip})")
            win.update_idletasks()
            w = win.winfo_reqwidth()
            win.geometry(f"+{(win.winfo_screenwidth() - w) // 2}+0")
            win.deiconify()
            win.lift()
        else:
            win.withdraw()

    def tick():
        housekeeping()
        if state["session"]:
            win.lift()
        root.after(500, tick)

    tick()
    root.mainloop()
