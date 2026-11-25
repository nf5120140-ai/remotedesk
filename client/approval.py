"""
"Someone asks to control this computer" dialog (host side).

ask(root, info, T) is called from a background thread; it shows a topmost
dialog on the Tk thread and waits for the answer:
    (True, always)  accepted
    (False, False)  declined
    (None, False)   nobody answered in time
"""
import threading
import tkinter as tk

import core

RED = "#c62828"


def ask(root, info, T=lambda s: s, timeout=core.APPROVAL_TIMEOUT):
    done = threading.Event()
    result = {"v": (None, False)}

    def show():
        win = tk.Toplevel(root)
        win.title(T("RemoteDesk - בקשת גישה"))
        win.attributes("-topmost", True)
        win.resizable(False, False)
        win.configure(bg="white")
        always = tk.BooleanVar(value=False)
        left = {"n": timeout}

        head = tk.Label(win, text=T("🔔 בקשת גישה למחשב הזה"), bg=RED, fg="white",
                        font=("Segoe UI", 14, "bold"), padx=16, pady=10, anchor="e")
        head.pack(fill="x")
        body = tk.Frame(win, bg="white", padx=18, pady=12)
        body.pack(fill="both")
        tk.Label(body, text=T(f"«{info.get('name', '')}» מבקש לשלוט במחשב הזה."), bg="white",
                 font=("Segoe UI", 12), anchor="e", justify="right").pack(fill="x")
        tk.Label(body, text=T(f"כתובת: {info.get('ip', '?')}"), bg="white", fg="gray",
                 font=("Segoe UI", 9), anchor="e").pack(fill="x")
        tk.Label(body, text=T("קוד אימות - ודא שזה אותו קוד שמופיע אצל המבקש:"), bg="white",
                 font=("Segoe UI", 10), anchor="e").pack(fill="x", pady=(12, 0))
        tk.Label(body, text=info.get("code", ""), bg="white", fg=RED,
                 font=("Consolas", 26, "bold")).pack()
        tk.Checkbutton(body, text=T("אפשר למחשב הזה להיכנס תמיד, בלי לשאול שוב"), variable=always,
                       bg="white", font=("Segoe UI", 10), anchor="e").pack(fill="x", pady=(8, 0))
        btns = tk.Frame(body, bg="white")
        btns.pack(fill="x", pady=(14, 0))

        def finish(v):
            if done.is_set():
                return
            result["v"] = v
            done.set()
            try:
                win.destroy()
            except tk.TclError:
                pass

        tk.Button(btns, text=T("✔ אשר"), bg="#0a7d00", fg="white", relief="flat", width=12,
                  font=("Segoe UI", 11, "bold"),
                  command=lambda: finish((True, always.get()))).pack(side="right", padx=4)
        decline = tk.Button(btns, text="", bg="#eeeeee", relief="flat", width=12,
                            font=("Segoe UI", 11), command=lambda: finish((False, False)))
        decline.pack(side="left", padx=4)

        def tick():
            if done.is_set():
                return
            decline.config(text=T(f"✖ דחה ({left['n']})"))
            if left["n"] <= 0:
                finish((None, False))
                return
            left["n"] -= 1
            win.after(1000, tick)

        win.protocol("WM_DELETE_WINDOW", lambda: finish((False, False)))
        win.update_idletasks()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        win.geometry(f"+{(win.winfo_screenwidth() - w) // 2}+{(win.winfo_screenheight() - h) // 3}")
        win.deiconify()
        win.lift()
        win.focus_force()
        win.bell()
        tick()

    try:
        root.after(0, show)
    except Exception:
        return (None, False)
    done.wait(timeout + 5)
    return result["v"]
