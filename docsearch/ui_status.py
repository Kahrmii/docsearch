"""Tabs "Status" (health overview) and "Settings" (values stored in .env)."""
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from . import __version__, health, preferences

COLORS = {health.OK: "#2e7d32", health.WARN: "#b26a00", health.ERROR: "#b00020"}


class StatusTab(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=12)
        self.q = queue.Queue()
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        bar = ttk.Frame(self)
        bar.grid(row=0, column=0, sticky="ew")
        self.refresh_button = ttk.Button(bar, text="Refresh", command=self.refresh)
        self.refresh_button.pack(side="left")
        self.note = tk.StringVar(value="docsearch " + __version__)
        ttk.Label(bar, textvariable=self.note, padding=(10, 0)).pack(side="left")
        self.text = tk.Text(self, state="disabled", wrap="word", font=("Segoe UI", 10), height=14)
        self.text.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        for status, color in COLORS.items():
            self.text.tag_configure(status, foreground=color, font=("Segoe UI", 10, "bold"))
        self.after(300, self.poll)

    def refresh(self):
        self.refresh_button.config(state="disabled")
        self.note.set("Checking ...")
        threading.Thread(target=lambda: self.q.put(health.run_checks()), daemon=True).start()

    def poll(self):
        try:
            checks = self.q.get_nowait()
        except queue.Empty:
            pass
        else:
            self.show(checks)
        self.after(300, self.poll)

    def show(self, checks):
        self.refresh_button.config(state="normal")
        self.note.set(f"docsearch {__version__}")
        self.text.config(state="normal")
        self.text.delete("1.0", "end")
        for check in checks:
            self.text.insert("end", check["status"].upper().ljust(6), check["status"])
            self.text.insert("end", f"{check['name']}: {check['detail']}\n\n")
        self.text.config(state="disabled")


class SettingsTab(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=12)
        self.columnconfigure(1, weight=1)
        self.vars = {}
        for row, field in enumerate(preferences.current()):
            ttk.Label(self, text=field["label"]).grid(row=row * 2, column=0, sticky="w", pady=(8, 0))
            if field["kind"] == "bool":
                var = tk.BooleanVar(value=bool(field["value"]))
                widget = ttk.Checkbutton(self, variable=var)
            elif field["kind"] == "choice":
                var = tk.StringVar(value=field["value"])
                widget = ttk.Combobox(self, textvariable=var, values=field["choices"], state="readonly", width=22)
            else:
                var = tk.StringVar(value=str(field["value"]))
                widget = ttk.Entry(self, textvariable=var, width=34)
            widget.grid(row=row * 2, column=1, sticky="w", padx=8, pady=(8, 0))
            note = field["help"] + (" (An environment variable overrides this on the next start.)"
                                    if field["env_override"] else "")
            ttk.Label(self, text=note, foreground="gray").grid(row=row * 2 + 1, column=1, sticky="w", padx=8)
            self.vars[field["key"]] = var
        last = len(self.vars) * 2
        bar = ttk.Frame(self)
        bar.grid(row=last, column=0, columnspan=2, sticky="w", pady=14)
        ttk.Button(bar, text="Save", command=self.save).pack(side="left")
        self.note = tk.StringVar()
        ttk.Label(bar, textvariable=self.note, padding=(10, 0)).pack(side="left")
        ttk.Label(self, foreground="gray", text="Saved to the .env file and applied at once. "
                                                "Passwords and API keys are not editable here.").grid(
            row=last + 1, column=0, columnspan=2, sticky="w")

    def save(self):
        try:
            preferences.save({key: var.get() for key, var in self.vars.items()})
        except ValueError as e:
            messagebox.showerror("Settings", str(e))
            return
        self.note.set("Saved.")
