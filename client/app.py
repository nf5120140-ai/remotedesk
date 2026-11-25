"""
RemoteDesk - שליטה מרחוק על מחשב (כמו AnyDesk).

אותה תוכנה רצה בשני המחשבים:
  * "המחשב הזה"     - המספר המזהה והסיסמה, שבעזרתם מתחברים אליך.
  * "מחשב מרוחק"    - מקלידים את המספר של המחשב השני כדי לשלוט בו.

מצבי הפעלה:
  RemoteDesk.exe            חלון הניהול (רץ כמנהל מערכת)
  RemoteDesk.exe --service  שירות Windows (SYSTEM) - מופעל אוטומטית
  RemoteDesk.exe --agent    סוכן השליטה בתוך ה-session - מופעל על ידי השירות
"""
import io
import json
import os
import queue
import sys
import threading
from pathlib import Path
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from PIL import Image, ImageTk

import core
import updater
import winsvc

APP_NAME = "RemoteDesk"
FONT = "Segoe UI" if sys.platform == "win32" else "DejaVu Sans"

# Windows draws Hebrew right-to-left by itself. Linux/macOS Tk does not.
if sys.platform == "win32":
    def T(s):
        return s
else:
    try:
        from bidi.algorithm import get_display as T
    except ImportError:
        try:
            from bidi import get_display as T
        except ImportError:
            def T(s):
                return s


def info(msg, parent=None):
    messagebox.showinfo(T(APP_NAME), T(msg), parent=parent)


def warn(msg, parent=None):
    messagebox.showwarning(T(APP_NAME), T(msg), parent=parent)


def error(msg, parent=None):
    messagebox.showerror(T(APP_NAME), T(msg), parent=parent)


# ==========================================================================
# Remote screen window
# ==========================================================================
class ViewerWindow(tk.Toplevel):
    def __init__(self, master, session, title):
        super().__init__(master)
        self.title(title)
        self.geometry("1280x780")
        self.configure(bg="black")
        self.session = session
        self.frames = queue.Queue(maxsize=2)
        self.photo = None
        self.remote_w, self.remote_h = 1, 1
        self.img_box = (0, 0, 1, 1)
        self.monitors = 1
        self.closed = False

        bar = ttk.Frame(self)
        bar.pack(fill="x")
        # right-to-left toolbar
        ttk.Button(bar, text=T("Ctrl+Alt+Del"), command=lambda: self.send(type="cad")).pack(side="right", padx=2, pady=2)
        ttk.Button(bar, text=T("הקלד טקסט מהלוח"), command=self.paste_text).pack(side="right", padx=2)
        ttk.Button(bar, text=T("שלח קובץ"), command=self.send_file).pack(side="right", padx=2)
        ttk.Label(bar, text=T("איכות:")).pack(side="right", padx=(12, 2))
        self.quality = tk.StringVar(value=T("מאוזן"))
        self.presets = {T("מהיר"): (35, 1280), T("מאוזן"): (60, 1600), T("גבוה"): (85, 2560),
                        T("מקסימלי"): (92, 7680)}
        q = ttk.Combobox(bar, textvariable=self.quality, width=8, state="readonly",
                         values=list(self.presets), justify="right")
        q.pack(side="right")
        q.bind("<<ComboboxSelected>>", self.change_quality)
        ttk.Label(bar, text=T("מסך:")).pack(side="right", padx=(12, 2))
        self.monitor_var = tk.StringVar(value="1")
        self.monitor_box = ttk.Combobox(bar, textvariable=self.monitor_var, width=3, state="readonly", values=["1"])
        self.monitor_box.pack(side="right")
        self.monitor_box.bind("<<ComboboxSelected>>",
                              lambda e: self.send(type="settings", monitor=int(self.monitor_var.get())))
        ttk.Button(bar, text=T("ניתוק"), command=self.close).pack(side="left", padx=4)
        self.status = ttk.Label(bar, text=T("מתחבר..."))
        self.status.pack(side="left", padx=6)

        self.canvas = tk.Canvas(self, bg="black", highlightthickness=0, takefocus=1)
        self.canvas.pack(fill="both", expand=True)
        self.image_item = self.canvas.create_image(0, 0, anchor="nw")

        c = self.canvas
        c.bind("<Motion>", self.on_move)
        for n, name in ((1, "left"), (2, "middle"), (3, "right")):
            c.bind(f"<ButtonPress-{n}>", lambda e, b=name: self.on_button(e, b, True))
            c.bind(f"<ButtonRelease-{n}>", lambda e, b=name: self.on_button(e, b, False))
        c.bind("<MouseWheel>", lambda e: self.send(type="scroll", dy=1 if e.delta > 0 else -1))
        c.bind("<Button-4>", lambda e: self.send(type="scroll", dy=1))
        c.bind("<Button-5>", lambda e: self.send(type="scroll", dy=-1))
        c.bind("<KeyPress>", lambda e: self.on_key(e, True))
        c.bind("<KeyRelease>", lambda e: self.on_key(e, False))
        c.bind("<Enter>", lambda e: c.focus_set())
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.after(15, self.pump)

    # ---- called from the network thread
    def push_frame(self, data):
        try:
            self.frames.put_nowait(data)
        except queue.Full:
            self.session.ack()

    def push_message(self, m):
        self.after(0, self.handle_message, m)

    def push_closed(self, reason):
        self.after(0, self.on_closed, reason)

    # ---- UI thread
    def handle_message(self, m):
        t = m.get("type")
        if t == "screen":
            self.remote_w, self.remote_h = m["width"], m["height"]
            n = max(1, int(m.get("monitors", 1)))
            if n != self.monitors:
                self.monitors = n
                self.monitor_box["values"] = [str(i) for i in range(1, n + 1)]
            self.monitor_var.set(str(m.get("monitor", 1)))
            self.status.config(text=T(f"מחובר  {self.remote_w}x{self.remote_h}  🔒 מוצפן"))
        elif t == "file_done":
            info(f"הקובץ התקבל במחשב המרוחק:\n{m.get('path', '')}", self)
        elif t == "file_error":
            error(f"שליחת הקובץ נכשלה: {m.get('message', '')}", self)

    def on_closed(self, reason):
        if self.closed:
            return
        self.closed = True
        info(f"החיבור הסתיים: {reason}", self)
        self.destroy()

    def pump(self):
        if self.closed:
            return
        data = None
        try:
            while True:
                if data is not None:
                    self.session.ack()
                data = self.frames.get_nowait()
        except queue.Empty:
            pass
        if data is not None:
            try:
                self.show(data)
            finally:
                self.session.ack()
        self.after(15, self.pump)

    def show(self, data):
        img = Image.open(io.BytesIO(data))
        cw, ch = max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height())
        scale = min(cw / img.width, ch / img.height)
        w, h = max(1, int(img.width * scale)), max(1, int(img.height * scale))
        if (w, h) != img.size:
            img = img.resize((w, h), Image.BILINEAR)
        x, y = (cw - w) // 2, (ch - h) // 2
        self.img_box = (x, y, w, h)
        self.photo = ImageTk.PhotoImage(img)
        self.canvas.itemconfigure(self.image_item, image=self.photo)
        self.canvas.coords(self.image_item, x, y)

    def norm(self, e):
        x, y, w, h = self.img_box
        return (e.x - x) / w, (e.y - y) / h

    def send(self, **m):
        self.session.send(**m)

    def on_move(self, e):
        nx, ny = self.norm(e)
        if 0 <= nx <= 1 and 0 <= ny <= 1:
            self.send(type="move", x=round(nx, 5), y=round(ny, 5))

    def on_button(self, e, b, down):
        self.canvas.focus_set()
        nx, ny = self.norm(e)
        if 0 <= nx <= 1 and 0 <= ny <= 1:
            self.send(type="move", x=round(nx, 5), y=round(ny, 5))
            self.send(type="button", b=b, down=down)
        elif not down:
            self.send(type="button", b=b, down=False)

    def on_key(self, e, down):
        char = e.char if e.char and ord(e.char[0]) >= 32 else ""
        self.send(type="key", name=e.keysym, char=char, down=down)
        return "break"

    def paste_text(self):
        try:
            text = self.clipboard_get()
        except tk.TclError:
            return
        if text:
            self.send(type="type_text", text=text)

    def send_file(self):
        path = filedialog.askopenfilename(parent=self, title=T("בחר קובץ לשליחה"))
        if not path:
            return

        def progress(sent, total):
            pct = int(sent * 100 / total) if total else 100
            self.after(0, lambda: self.status.config(text=T(f"שולח קובץ... {pct}%")))

        def work():
            try:
                self.session.send_file(path, progress)
            except Exception as e:
                self.after(0, error, f"שליחת הקובץ נכשלה: {e}", self)

        threading.Thread(target=work, daemon=True).start()

    def change_quality(self, _e=None):
        q, w = self.presets[self.quality.get()]
        self.send(type="settings", quality=q, max_width=w)

    def close(self):
        self.closed = True
        self.session.close()
        self.destroy()


# ==========================================================================
# Main window
# ==========================================================================
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.cfg = core.load_config()
        self.title(T(f"{APP_NAME} - שליטה מרחוק"))
        self.geometry("600x520")
        self.resizable(False, False)
        style = ttk.Style(self)
        try:
            style.theme_use("vista" if sys.platform == "win32" else "clam")
        except tk.TclError:
            pass
        style.configure("TLabelframe.Label", font=(FONT, 11, "bold"))

        root = ttk.Frame(self, padding=14)
        root.pack(fill="both", expand=True)

        # ---------------- This computer
        box = ttk.LabelFrame(root, text=T(" המחשב הזה "), padding=10, labelanchor="ne")
        box.pack(fill="x")
        box.columnconfigure(1, weight=1)
        ttk.Label(box, text=T("המספר שלך:"), font=(FONT, 10)).grid(row=0, column=3, sticky="e")
        self.id_label = ttk.Label(box, text=core.format_id(self.cfg["id"]), font=(FONT, 22, "bold"),
                                  foreground="#c62828")
        self.id_label.grid(row=0, column=1, columnspan=2, sticky="e", padx=8)
        ttk.Button(box, text=T("העתק"), command=self.copy_id).grid(row=0, column=0, sticky="w")

        ttk.Label(box, text=T("סיסמה:"), font=(FONT, 10)).grid(row=1, column=3, sticky="e", pady=(10, 0))
        self.pw_var = tk.StringVar()
        self.pw_entry = ttk.Entry(box, textvariable=self.pw_var, show="*", width=22)
        self.pw_entry.grid(row=1, column=2, sticky="e", padx=8, pady=(10, 0))
        self.show_pw = tk.BooleanVar()
        ttk.Checkbutton(box, text=T("הצג"), variable=self.show_pw,
                        command=lambda: self.pw_entry.config(show="" if self.show_pw.get() else "*")
                        ).grid(row=1, column=1, sticky="e", pady=(10, 0))
        ttk.Button(box, text=T("שמור סיסמה"), command=self.save_password).grid(row=1, column=0, sticky="w", pady=(10, 0))

        self.status_label = ttk.Label(box, text=T("מתחיל..."), foreground="gray", font=(FONT, 10))
        self.status_label.grid(row=2, column=0, columnspan=4, sticky="e", pady=(10, 0))
        self.session_frame = ttk.Frame(box)
        self.session_frame.grid(row=3, column=0, columnspan=4, sticky="we", pady=(6, 0))
        self.session_label = ttk.Label(self.session_frame, text="", foreground="#c62828", font=(FONT, 10, "bold"))
        self.session_label.pack(side="right")
        self.kick_btn = ttk.Button(self.session_frame, text=T("נתק את החיבור"), command=self.kick)

        # ---------------- Remote computer
        box2 = ttk.LabelFrame(root, text=T(" שליטה במחשב מרוחק "), padding=10, labelanchor="ne")
        box2.pack(fill="x", pady=(12, 0))
        ttk.Label(box2, text=T("מספר המחשב המרוחק:"), font=(FONT, 10)).pack(side="right")
        self.remote_var = tk.StringVar()
        self.remote_combo = ttk.Combobox(box2, textvariable=self.remote_var, width=14,
                                         font=(FONT, 14), values=self.cfg.get("recent", []))
        self.remote_combo.pack(side="right", padx=8)
        self.remote_combo.bind("<Return>", lambda e: self.connect())
        self.connect_btn = ttk.Button(box2, text=T("התחבר"), command=self.connect)
        self.connect_btn.pack(side="right")

        # ---------------- System mode
        box3 = ttk.LabelFrame(root, text=T(" גישה מלאה (מנהל מערכת) "), padding=10, labelanchor="ne")
        box3.pack(fill="x", pady=(12, 0))
        self.svc_label = ttk.Label(box3, text="", font=(FONT, 10), justify="right", wraplength=540)
        self.svc_label.pack(side="top", anchor="e")
        btns = ttk.Frame(box3)
        btns.pack(side="top", anchor="e", pady=(6, 0))
        self.install_btn = ttk.Button(btns, text=T("התקן כשירות מערכת"), command=self.install)
        self.install_btn.pack(side="right", padx=2)
        self.uninstall_btn = ttk.Button(btns, text=T("הסר שירות"), command=self.uninstall)
        self.uninstall_btn.pack(side="right", padx=2)

        # ---------------- Server
        box4 = ttk.Frame(root)
        box4.pack(fill="x", pady=(12, 0))
        ttk.Label(box4, text=T("כתובת שרת:")).pack(side="right")
        self.server_var = tk.StringVar(value=self.cfg["server"])
        ttk.Entry(box4, textvariable=self.server_var, width=42).pack(side="right", padx=6)
        ttk.Button(box4, text=T("החל"), command=self.save_server).pack(side="right")

        # ---------------- Version / updates
        box5 = ttk.Frame(root)
        box5.pack(fill="x", pady=(10, 0))
        self.version_label = ttk.Label(box5, text=T(f"גרסה {updater.VERSION}"), foreground="gray")
        self.version_label.pack(side="right")
        self.update_btn = ttk.Button(box5, text="", command=self.do_update)
        self.update_info = None
        threading.Thread(target=self.check_update, daemon=True).start()

        self.host = None
        self.service_mode = None
        self.refresh_mode()
        self.after(1000, self.poll_service)
        self.protocol("WM_DELETE_WINDOW", self.on_exit)

    # ---------------------------------------------------------------- modes
    def refresh_mode(self):
        """Service installed & running -> the SYSTEM agent hosts this computer.
        Otherwise this window hosts it itself (works while the window is open)."""
        state = winsvc.service_state()
        service_mode = state == "running"
        if service_mode != self.service_mode:
            self.service_mode = service_mode
            if service_mode:
                if self.host:
                    self.host.stop()
                    self.host = None
            else:
                self.start_host()
        if not winsvc.IS_WIN:
            self.svc_label.config(text=T("שירות מערכת זמין רק ב-Windows."))
            self.install_btn.config(state="disabled")
            self.uninstall_btn.config(state="disabled")
        elif state == "running":
            self.svc_label.config(text=T("✅ השירות פועל: גישה מלאה גם במסך הכניסה, במסך הנעילה ובחלונות UAC, "
                                         "גם לפני שמשתמש נכנס ל-Windows, וגם כשהחלון הזה סגור."),
                                  foreground="#0a7d00")
            self.install_btn.config(text=T("התקן מחדש"))
            self.uninstall_btn.config(state="normal")
        else:
            self.svc_label.config(text=T("השירות לא מותקן: הגישה עובדת רק כשהחלון הזה פתוח, ובלי מסך הכניסה "
                                         "וחלונות UAC. מומלץ להתקין."),
                                  foreground="#b26a00")
            self.install_btn.config(text=T("התקן כשירות מערכת"))
            self.uninstall_btn.config(state="normal" if state == "stopped" else "disabled")

    def poll_service(self):
        if self.service_mode:
            try:
                st = json.loads(core.STATUS_FILE.read_text("utf-8"))
            except (OSError, ValueError):
                st = None
            if st is None or time.time() - st.get("time", 0) > 3600 * 24 * 365:
                self.set_status("ממתין לשירות...", False)
            else:
                self.set_status(st.get("text", ""), st.get("online", False))
                if st.get("id"):
                    self.cfg["id"] = st["id"]
                self.set_session(st.get("session", False), st.get("ip", ""))
        self.after(1000, self.poll_service)

    def start_host(self):
        if self.host:
            self.host.stop()
        self.host = core.HostService(
            self.cfg,
            on_status=lambda t, ok: self.after(0, self.set_status, t, ok),
            on_session=lambda a, ip: self.after(0, self.set_session, a, ip))
        self.host.start()

    def set_status(self, text, ok):
        self.status_label.config(text=T(text + (" ●" if ok else " ○")),
                                 foreground="#0a7d00" if ok else "gray")
        self.id_label.config(text=core.format_id(self.cfg["id"]))

    def set_session(self, active, ip):
        if active:
            text = T(f"⚠ המחשב שלך נשלט כרגע מרחוק ({ip})")
            if self.session_label.cget("text") != text:
                self.session_label.config(text=text)
                self.kick_btn.pack(side="left")
                if not self.service_mode:
                    self.deiconify()
                    self.lift()
                    self.bell()
        else:
            self.session_label.config(text="")
            self.kick_btn.pack_forget()

    def kick(self):
        if self.service_mode:
            try:
                core.KICK_FILE.write_text("1")
            except OSError as e:
                error(str(e))
        elif self.host:
            threading.Thread(target=self.host.kick, daemon=True).start()

    # ---------------------------------------------------------------- updates
    def check_update(self):
        if not updater.enabled():
            return
        try:
            info = updater.check()
        except Exception:
            return
        self.after(0, self.show_update, info)

    def show_update(self, info):
        if info is None:
            self.version_label.config(text=T(f"גרסה {updater.VERSION} · מעודכנת ✓"))
            return
        self.update_info = info
        if self.service_mode:
            # the service installs it by itself within a few hours (or right away on restart)
            self.version_label.config(text=T(f"גרסה {updater.VERSION} · גרסה {info[0]} תותקן אוטומטית"))
        self.update_btn.config(text=T(f"עדכן עכשיו לגרסה {info[0]}"))
        self.update_btn.pack(side="left")

    def do_update(self):
        if not self.update_info:
            return
        self.update_btn.config(state="disabled", text=T("מוריד עדכון..."))
        exe = Path(sys.executable)

        def progress(done, total):
            if total:
                pct = int(done * 100 / total)
                self.after(0, lambda: self.update_btn.config(text=T(f"מוריד עדכון... {pct}%")))

        def work():
            try:
                new = exe.with_suffix(".new")
                updater.download(self.update_info, new, progress)
                if self.service_mode:
                    winsvc.uninstall_service_for_update()
                updater.swap_in(new, exe)
                if self.service_mode:
                    winsvc.start_service()
            except Exception as e:
                self.after(0, error, f"העדכון נכשל: {e}")
                self.after(0, lambda: self.update_btn.config(state="normal",
                                                             text=T(f"עדכן עכשיו לגרסה {self.update_info[0]}")))
                return
            self.after(0, self.restart_after_update)

        threading.Thread(target=work, daemon=True).start()

    def restart_after_update(self):
        import subprocess
        if self.host:
            self.host.stop()
        subprocess.Popen([sys.executable], close_fds=True)
        self.destroy()

    # ---------------------------------------------------------------- actions
    def copy_id(self):
        self.clipboard_clear()
        self.clipboard_append(self.cfg["id"])

    def reload_cfg(self):
        fresh = core.load_config()
        self.cfg.update(fresh)

    def save_password(self):
        pw = self.pw_var.get()
        if len(pw) < 8:
            warn("בחר סיסמה של 8 תווים לפחות.")
            return
        self.reload_cfg()
        core.set_password(self.cfg, pw)
        self.pw_var.set("")
        if not self.service_mode:
            self.start_host()
        info("הסיסמה נשמרה. הגישה מרחוק פעילה.")

    def save_server(self):
        self.reload_cfg()
        self.cfg["server"] = self.server_var.get().strip()
        core.save_config(self.cfg)
        if not self.service_mode:
            self.start_host()

    def install(self):
        if not winsvc.is_admin():
            error("נדרשות הרשאות מנהל מערכת. סגור את התוכנה ופתח אותה שוב, ובחלון UAC לחץ 'כן'.")
            return
        try:
            self.config(cursor="watch")
            self.update()
            winsvc.install_service()
        except Exception as e:
            error(f"ההתקנה נכשלה:\n{e}")
            return
        finally:
            self.config(cursor="")
        self.refresh_mode()
        info("השירות הותקן והופעל.\n\nמעכשיו המחשב נגיש תמיד, גם אחרי הפעלה מחדש, "
             "במסך הכניסה ובחלונות מנהל מערכת.\nהתוכנה הותקנה בתיקייה:\n" + str(winsvc.INSTALL_DIR))

    def uninstall(self):
        if not messagebox.askyesno(T(APP_NAME), T("להסיר את שירות המערכת?")):
            return
        try:
            winsvc.uninstall_service()
        except Exception as e:
            error(str(e))
        self.refresh_mode()

    # ---------------------------------------------------------------- viewer
    def connect(self):
        rid = self.remote_var.get().replace(" ", "")
        if not (rid.isdigit() and len(rid) == 9):
            warn("הקלד מספר מחשב בן 9 ספרות.")
            return
        if rid == self.cfg["id"]:
            warn("זה המספר של המחשב הזה עצמו.")
            return
        pw = simpledialog.askstring(T(APP_NAME), T(f"סיסמה עבור {core.format_id(rid)}:"), show="*", parent=self)
        if not pw:
            return
        self.connect_btn.config(state="disabled", text=T("מתחבר..."))
        threading.Thread(target=self._do_connect, args=(rid, pw), daemon=True).start()

    def _do_connect(self, rid, pw):
        holder = {}
        sess = core.ViewerSession(
            self.cfg["server"], rid, pw,
            on_frame=lambda d: holder["w"].push_frame(d) if "w" in holder else sess.ack(),
            on_message=lambda m: holder["w"].push_message(m) if "w" in holder else None,
            on_closed=lambda r: holder["w"].push_closed(r) if "w" in holder else None)
        try:
            sess.handshake()
        except Exception as e:
            sess.close()
            msg = str(e) or e.__class__.__name__
            if isinstance(e, (OSError, TimeoutError)) and not isinstance(e, core.AuthError):
                msg = f"אין חיבור לשרת ({msg}). בדוק את כתובת השרת ואת החיבור לאינטרנט."
            self.after(0, self._connect_failed, msg)
            return
        self.after(0, self._open_viewer, rid, sess, holder)

    def _connect_failed(self, err):
        self.connect_btn.config(state="normal", text=T("התחבר"))
        error(f"החיבור נכשל:\n{err}")

    def _open_viewer(self, rid, sess, holder):
        self.connect_btn.config(state="normal", text=T("התחבר"))
        self.reload_cfg()
        recent = [rid] + [r for r in self.cfg.get("recent", []) if r != rid]
        self.cfg["recent"] = recent[:10]
        core.save_config(self.cfg)
        self.remote_combo["values"] = self.cfg["recent"]
        holder["w"] = ViewerWindow(self, sess, T(f"{APP_NAME} - {core.format_id(rid)}"))
        sess.start()

    def on_exit(self):
        if self.host:
            self.host.stop()
        self.destroy()


def main():
    if "--service" in sys.argv:
        winsvc.run_service()
        return
    if "--agent" in sys.argv:
        winsvc.run_agent()
        return
    # used by the installer (silent)
    if "--install-service" in sys.argv or "--uninstall-service" in sys.argv:
        try:
            if "--install-service" in sys.argv:
                winsvc.install_service()
            else:
                winsvc.uninstall_service()
        except Exception as e:
            winsvc.log(f"install/uninstall failed: {e!r}")
            sys.exit(1)
        return
    if winsvc.IS_WIN and not winsvc.is_admin():
        if winsvc.relaunch_as_admin():
            return
    core.set_dpi_aware()
    try:
        app = App()
    except PermissionError:
        r = tk.Tk()
        r.withdraw()
        error("נדרשות הרשאות מנהל מערכת.\nפתח את התוכנה שוב ובחלון UAC לחץ 'כן'.")
        return
    app.mainloop()


if __name__ == "__main__":
    main()
