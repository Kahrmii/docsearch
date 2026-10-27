#!/usr/bin/env python3
"""Main window: tabs "Index" and "Ask". Start it with start.bat (Windows) or start.sh (Linux/macOS).

On the first start it creates .env with a random password and starts the Docker containers."""
import queue
import threading
import tkinter as tk
import traceback
from tkinter import messagebox, ttk

from docsearch import config, services
from docsearch.ui_chat import ChatTab
from docsearch.ui_common import Settings
from docsearch.ui_index import IndexTab


class App(tk.Tk):
    def __init__(self, first_run=False):
        super().__init__()
        self.title("docsearch")
        self.geometry("820x620")
        settings = Settings()
        self.docker_status = tk.StringVar(
            value=(f"First start: {config.ENV_FILE.name} was created with a random Elasticsearch password. "
                   "Starting the Docker services ...") if first_run else "Checking the Docker services ...")
        ttk.Label(self, textvariable=self.docker_status, padding=(8, 2)).pack(side="bottom", anchor="w")
        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True)
        self.chat = ChatTab(notebook, settings)
        self.indexing = IndexTab(notebook, settings, on_finished=self.chat.refresh_indices)
        notebook.add(self.indexing, text="Index")
        notebook.add(self.chat, text="Ask")
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.q = queue.Queue()
        threading.Thread(target=self._start_services, daemon=True).start()
        self.after(300, self.poll)

    def _start_services(self):
        if services.ensure_running(lambda text: self.q.put(("status", text))):
            self.q.put(("ready", None))

    def poll(self):
        try:
            while True:
                kind, value = self.q.get_nowait()
                if kind == "status":
                    self.docker_status.set(value)
                elif kind == "ready":
                    self.chat.refresh_indices()
        except queue.Empty:
            pass
        self.after(300, self.poll)

    def on_close(self):
        if self.indexing.running() and not messagebox.askyesno(
                "Quit", "Indexing is still running. Quit anyway?\n"
                        "The next run continues where this one stopped."):
            return
        self.destroy()


if __name__ == "__main__":
    config.setup_logging("app", console=False)
    try:
        App(first_run=config.ensure_env()).mainloop()
    except Exception:
        messagebox.showerror("docsearch", traceback.format_exc()[-1500:])
