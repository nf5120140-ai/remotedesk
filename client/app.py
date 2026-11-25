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
import approval
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
        self.geometry("620x500")
        self.resizable(False, False)
        style = ttk.Style(self)
        try:
            style.theme_use("vista" if sys.platform == "win32" else "clam")
        except tk.TclError:
            pass
        style.configure("TLabelframe.Label", font=(FONT, 11, "bold"))

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        self.nb = nb
        root = ttk.Frame(nb, padding=12)
        tab_saved = ttk.Frame(nb, padding=12)
        tab_trusted = ttk.Frame(nb, padding=12)
        tab_settings = ttk.Frame(nb, padding=12)
        nb.add(root, text=T("  ראשי  "))
        nb.add(tab_saved, text=T("  מחשבים שמורים  "))
        nb.add(tab_trusted, text=T("  מורשים להיכנס אליי  "))
        nb.add(tab_settings, text=T("  הגדרות  "))

        # ================= Main tab
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

        box2 = ttk.LabelFrame(root, text=T(" שליטה במחשב מרוחק "), padding=10, labelanchor="ne")
        box2.pack(fill="x", pady=(12, 0))
        row = ttk.Frame(box2)
        row.pack(fill="x")
        ttk.Label(row, text=T("מספר המחשב המרוחק:"), font=(FONT, 10)).pack(side="right")
        self.remote_var = tk.StringVar()
        self.remote_combo = ttk.Combobox(row, textvariable=self.remote_var, width=14,
                                         font=(FONT, 14), values=self.cfg.get("recent", []))
        self.remote_combo.pack(side="right", padx=8)
        self.remote_combo.bind("<Return>", lambda e: self.connect())
        self.connect_btn = ttk.Button(row, text=T("התחבר"), command=self.connect)
        self.connect_btn.pack(side="right")
        ttk.Label(box2, text=T("טיפ: מחשבים שסימנת \"שמור\" מופיעים בלשונית \"מחשבים שמורים\" - "
                               "לחיצה כפולה ונכנסים בלי סיסמה."), foreground="gray",
                  font=(FONT, 9), wraplength=540, justify="right").pack(anchor="e", pady=(8, 0))

        # ================= Saved computers tab (computers I can enter)
        ttk.Label(tab_saved, text=T("מחשבים שאפשר להיכנס אליהם בלחיצה כפולה, בלי סיסמה:"),
                  font=(FONT, 10)).pack(anchor="e")
        self.saved_list = tk.Listbox(tab_saved, height=11, font=(FONT, 12), justify="right",
                                     activestyle="none", selectbackground="#c62828")
        self.saved_list.pack(fill="both", expand=True, pady=6)
        self.saved_list.bind("<Double-Button-1>", lambda e: self.connect_saved())
        self.saved_list.bind("<Return>", lambda e: self.connect_saved())
        sb = ttk.Frame(tab_saved)
        sb.pack(fill="x")
        ttk.Button(sb, text=T("התחבר"), command=self.connect_saved).pack(side="right", padx=2)
        ttk.Button(sb, text=T("שנה שם"), command=self.rename_saved).pack(side="right", padx=2)
        ttk.Button(sb, text=T("מחק"), command=self.delete_saved).pack(side="right", padx=2)
        ttk.Button(sb, text=T("הוסף מחשב..."), command=self.add_saved).pack(side="left", padx=2)
        self.saved_ids = []

        # ================= Trusted tab (computers that can enter me)
        ttk.Label(tab_trusted, text=T("מחשבים וטלפונים שמורשים להיכנס למחשב הזה בלי סיסמה:"),
                  font=(FONT, 10)).pack(anchor="e")
        self.trusted_list = tk.Listbox(tab_trusted, height=11, font=(FONT, 12), justify="right",
                                       activestyle="none", selectbackground="#c62828")
        self.trusted_list.pack(fill="both", expand=True, pady=6)
        tb = ttk.Frame(tab_trusted)
        tb.pack(fill="x")
        ttk.Button(tb, text=T("בטל הרשאה"), command=self.revoke_trusted).pack(side="right", padx=2)
        ttk.Button(tb, text=T("בטל הכול"), command=self.revoke_all).pack(side="right", padx=2)
        ttk.Label(tab_trusted, text=T("מחשב נכנס לרשימה כשמאשרים בקשת גישה עם \"אפשר תמיד\", "
                                      "או כשמתחברים עם הסיסמה ומסמנים \"שמור\"."),
                  foreground="gray", font=(FONT, 9), wraplength=540, justify="right").pack(anchor="e", pady=(8, 0))
        self.trusted_ids = []

        # ================= Settings tab
        box3 = ttk.LabelFrame(tab_settings, text=T(" גישה מלאה (מנהל מערכת) "), padding=10, labelanchor="ne")
        box3.pack(fill="x")
        self.svc_label = ttk.Label(box3, text="", font=(FONT, 10), justify="right", wraplength=520)
        self.svc_label.pack(side="top", anchor="e")
        btns = ttk.Frame(box3)
        btns.pack(side="top", anchor="e", pady=(6, 0))
        self.install_btn = ttk.Button(btns, text=T("התקן כשירות מערכת"), command=self.install)
        self.install_btn.pack(side="right", padx=2)
        self.uninstall_btn = ttk.Button(btns, text=T("הסר שירות"), command=self.uninstall)
        self.uninstall_btn.pack(side="right", padx=2)

        box6 = ttk.Frame(tab_settings)
        box6.pack(fill="x", pady=(14, 0))
        ttk.Label(box6, text=T("שם המחשב (מוצג לצד השני):")).pack(side="right")
        self.name_var = tk.StringVar(value=self.cfg.get("device_name", ""))
        ttk.Entry(box6, textvariable=self.name_var, width=26, justify="right").pack(side="right", padx=6)
        ttk.Button(box6, text=T("שמור"), command=self.save_name).pack(side="right")

        box4 = ttk.Frame(tab_settings)
        box4.pack(fill="x", pady=(14, 0))
        ttk.Label(box4, text=T("כתובת שרת:")).pack(side="right")
        self.server_var = tk.StringVar(value=self.cfg["server"])
        ttk.Entry(box4, textvariable=self.server_var, width=40).pack(side="right", padx=6)
        ttk.Button(box4, text=T("החל"), command=self.save_server).pack(side="right")

        box5 = ttk.Frame(tab_settings)
        box5.pack(fill="x", pady=(14, 0))
        self.version_label = ttk.Label(box5, text=T(f"גרסה {updater.VERSION}"), foreground="gray")
        self.version_label.pack(side="right")
        self.update_btn = ttk.Button(box5, text="", command=self.do_update)
        self.update_info = None
        threading.Thread(target=self.check_update, daemon=True).start()

        self.refresh_lists()
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
        try:
            m = core.CONFIG_FILE.stat().st_mtime
        except OSError:
            m = 0
        if m != getattr(self, "_cfg_mtime", None):
            self._cfg_mtime = m
            self.refresh_lists()
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
            on_session=lambda a, ip: self.after(0, self.set_session, a, ip),
            on_request=lambda info: approval.ask(self, info, T))
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
            if updater.already_installed(self.update_info, exe):
                # the service already installed it - just restart this window
                self.after(0, self.restart_after_update)
                return
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

    # ---------------------------------------------------------------- lists
    def refresh_lists(self):
        self.reload_cfg()
        self.saved_list.delete(0, "end")
        self.saved_ids = []
        for hid, e in sorted(self.cfg.get("saved", {}).items(), key=lambda kv: kv[1].get("name", "")):
            lock = "🔓" if e.get("trust_key") else "🔒"
            self.saved_list.insert("end", T(f"{lock}  {e.get('name') or 'מחשב'}   ·   {core.format_id(hid)}"))
            self.saved_ids.append(hid)
        self.trusted_list.delete(0, "end")
        self.trusted_ids = []
        for did, e in sorted(self.cfg.get("trusted", {}).items(), key=lambda kv: kv[1].get("added", 0)):
            when = time.strftime("%d/%m/%Y", time.localtime(e.get("added", 0)))
            self.trusted_list.insert("end", T(f"{e.get('name', 'מחשב')}   ·   נוסף {when}"))
            self.trusted_ids.append(did)

    def _selected(self, lb, ids):
        sel = lb.curselection()
        return ids[sel[0]] if sel else None

    def connect_saved(self):
        hid = self._selected(self.saved_list, self.saved_ids)
        if hid:
            self.start_connect(hid)

    def rename_saved(self):
        hid = self._selected(self.saved_list, self.saved_ids)
        if not hid:
            return
        e = self.cfg["saved"][hid]
        name = simpledialog.askstring(T(APP_NAME), T("שם חדש:"), initialvalue=e.get("name", ""), parent=self)
        if name:
            self.reload_cfg()
            self.cfg["saved"][hid]["name"] = name.strip()[:60]
            core.save_config(self.cfg)
            self.refresh_lists()

    def delete_saved(self):
        hid = self._selected(self.saved_list, self.saved_ids)
        if hid and messagebox.askyesno(T(APP_NAME), T("למחוק את המחשב מהרשימה?")):
            self.reload_cfg()
            self.cfg["saved"].pop(hid, None)
            core.save_config(self.cfg)
            self.refresh_lists()

    def add_saved(self):
        rid = simpledialog.askstring(T(APP_NAME), T("מספר המחשב (9 ספרות):"), parent=self)
        if rid:
            self.remote_var.set(rid)
            self.connect(force_save=True)

    def revoke_trusted(self):
        did = self._selected(self.trusted_list, self.trusted_ids)
        if did and messagebox.askyesno(T(APP_NAME), T("לבטל את ההרשאה של המחשב הזה?")):
            self.reload_cfg()
            self.cfg["trusted"].pop(did, None)
            core.save_config(self.cfg)
            self.refresh_lists()

    def revoke_all(self):
        if self.cfg.get("trusted") and messagebox.askyesno(T(APP_NAME), T("לבטל את ההרשאות של כל המחשבים?")):
            self.reload_cfg()
            self.cfg["trusted"] = {}
            core.save_config(self.cfg)
            self.refresh_lists()

    def save_name(self):
        self.reload_cfg()
        self.cfg["device_name"] = self.name_var.get().strip()[:60] or "Computer"
        core.save_config(self.cfg)
        info("השם נשמר.")

    # ---------------------------------------------------------------- viewer
    def connect(self, force_save=False):
        rid = self.remote_var.get().replace(" ", "")
        if not (rid.isdigit() and len(rid) == 9):
            warn("הקלד מספר מחשב בן 9 ספרות.")
            return
        self.start_connect(rid)

    def start_connect(self, rid):
        if rid == self.cfg["id"]:
            warn("זה המספר של המחשב הזה עצמו.")
            return
        self.reload_cfg()
        saved = self.cfg.get("saved", {}).get(rid)
        if saved and saved.get("trust_key"):
            self._run_connect(rid, "trust", trust_key=saved["trust_key"])
        else:
            self.ask_how_to_connect(rid)

    def ask_how_to_connect(self, rid, note=""):
        d = tk.Toplevel(self)
        d.title(T(f"התחברות ל-{core.format_id(rid)}"))
        d.resizable(False, False)
        d.transient(self)
        d.grab_set()
        f = ttk.Frame(d, padding=16)
        f.pack(fill="both")
        if note:
            ttk.Label(f, text=T(note), foreground="#b26a00", wraplength=380, justify="right").pack(anchor="e", pady=(0, 8))
        ttk.Label(f, text=T(f"התחברות למחשב {core.format_id(rid)}"), font=(FONT, 12, "bold")).pack(anchor="e")
        ttk.Label(f, text=T("סיסמה:")).pack(anchor="e", pady=(12, 2))
        pw = tk.StringVar()
        e = ttk.Entry(f, textvariable=pw, show="*", width=30)
        e.pack(anchor="e")
        e.focus_set()
        remember = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text=T("שמור את המחשב - בפעם הבאה נכנסים בלי סיסמה"),
                        variable=remember).pack(anchor="e", pady=(10, 0))
        b = ttk.Frame(f)
        b.pack(fill="x", pady=(14, 0))

        def with_password(_e=None):
            if not pw.get():
                e.focus_set()
                return
            d.destroy()
            self._run_connect(rid, "password", password=pw.get(), remember=remember.get())

        def with_request():
            d.destroy()
            self._run_connect(rid, "request", remember=remember.get())

        ttk.Button(b, text=T("התחבר עם סיסמה"), command=with_password).pack(side="right", padx=2)
        ttk.Button(b, text=T("בקש אישור מהצד השני"), command=with_request).pack(side="right", padx=2)
        ttk.Button(b, text=T("ביטול"), command=d.destroy).pack(side="left", padx=2)
        ttk.Label(f, text=T("\"בקש אישור\" - במחשב השני יופיע חלון עם קוד אימות, ומי שיושב שם יאשר."),
                  foreground="gray", font=(FONT, 9), wraplength=380, justify="right").pack(anchor="e", pady=(10, 0))
        e.bind("<Return>", with_password)

    def _run_connect(self, rid, method, password="", trust_key=None, remember=False):
        self.connect_btn.config(state="disabled", text=T("מתחבר..."))
        wait = {"win": None}
        if method == "request":
            w = tk.Toplevel(self)
            w.title(T("ממתין לאישור"))
            w.resizable(False, False)
            w.transient(self)
            ff = ttk.Frame(w, padding=18)
            ff.pack()
            ttk.Label(ff, text=T("ממתין שהצד השני יאשר את הבקשה..."), font=(FONT, 11)).pack()
            ttk.Label(ff, text=T("קוד האימות (צריך להיות זהה לקוד שמופיע שם):")).pack(pady=(12, 0))
            code_lbl = ttk.Label(ff, text="···", font=("Consolas", 26, "bold"), foreground="#c62828")
            code_lbl.pack()
            wait.update(win=w, code=code_lbl)
        holder = {}
        sess = core.ViewerSession(
            self.cfg["server"], rid, password,
            on_frame=lambda d: holder["w"].push_frame(d) if "w" in holder else sess.ack(),
            on_message=lambda m: self.after(0, self._viewer_message, rid, m, holder),
            on_closed=lambda r: holder["w"].push_closed(r) if "w" in holder else None)
        if wait["win"]:
            ttk.Button(wait["win"].winfo_children()[0], text=T("ביטול"), command=sess.cancel).pack(pady=(12, 0))

        def work():
            try:
                sess.handshake(method, trust_key=trust_key, remember=remember,
                               device_id=self.cfg["device_id"], device_name=self.cfg.get("device_name", ""),
                               on_code=lambda c: self.after(0, lambda: wait["code"].config(text=c)))
            except Exception as e:
                sess.close()
                code = getattr(e, "code", None)
                msg = str(e) or e.__class__.__name__
                if not isinstance(e, core.AuthError):
                    msg = core.explain_connection_error(e)
                self.after(0, self._connect_failed, rid, method, code, msg, wait["win"])
                return
            self.after(0, self._open_viewer, rid, sess, holder, wait["win"], remember or method == "trust")

        threading.Thread(target=work, daemon=True).start()

    def _connect_failed(self, rid, method, code, err, wait_win):
        if wait_win:
            wait_win.destroy()
        self.connect_btn.config(state="normal", text=T("התחבר"))
        if method == "trust" and code == "not_trusted":
            self.reload_cfg()
            if rid in self.cfg.get("saved", {}):
                self.cfg["saved"][rid].pop("trust_key", None)
                core.save_config(self.cfg)
                self.refresh_lists()
            self.ask_how_to_connect(rid, "ההרשאה למחשב הזה בוטלה. התחבר שוב עם סיסמה או בקש אישור.")
            return
        error(f"החיבור נכשל:\n{err}")

    def _viewer_message(self, rid, m, holder):
        if m.get("type") == "trust_grant":
            self.reload_cfg()
            entry = self.cfg.setdefault("saved", {}).setdefault(rid, {})
            entry["trust_key"] = m.get("key")
            entry.setdefault("name", m.get("host_name") or core.format_id(rid))
            core.save_config(self.cfg)
            self.refresh_lists()
            return
        if "w" in holder:
            holder["w"].handle_message(m)

    def _open_viewer(self, rid, sess, holder, wait_win, save):
        if wait_win:
            wait_win.destroy()
        self.connect_btn.config(state="normal", text=T("התחבר"))
        self.reload_cfg()
        recent = [rid] + [r for r in self.cfg.get("recent", []) if r != rid]
        self.cfg["recent"] = recent[:10]
        if save:
            entry = self.cfg.setdefault("saved", {}).setdefault(rid, {})
            entry.setdefault("name", sess.remote_name or core.format_id(rid))
        core.save_config(self.cfg)
        self.remote_combo["values"] = self.cfg["recent"]
        self.refresh_lists()
        name = sess.remote_name or core.format_id(rid)
        holder["w"] = ViewerWindow(self, sess, T(f"{APP_NAME} - {name}"))
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
