"""Tab "Index": choose a folder, press Start."""
import logging
import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from . import embed_job, index_admin, indexing
from .es import Es, EsError
from .ui_common import QueueHandler


class IndexTab(ttk.Frame):
    def __init__(self, master, settings, on_finished=None, on_deleted=None):
        super().__init__(master, padding=12)
        self.settings = settings
        self.on_finished = on_finished  # on_finished(index name) after a successful run
        self.on_deleted = on_deleted  # on_deleted(index name) after an index was deleted
        self.es = None
        self.q = queue.Queue()
        self.stop_event = threading.Event()
        self.folder = tk.StringVar(value=settings.get("folder", ""))
        self.index = tk.StringVar(value=settings.get("index", "documents"))
        self.recursive = tk.BooleanVar(value=settings.get("recursive", True))
        self.kind = "File"  # what the progress counts: files while indexing, documents while creating embeddings

        self.columnconfigure(1, weight=1)
        self.rowconfigure(5, weight=1)
        ttk.Label(self, text="Folder").grid(row=0, column=0, sticky="w")
        ttk.Entry(self, textvariable=self.folder).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(self, text="Browse ...", command=self.pick).grid(row=0, column=2)
        ttk.Label(self, text="Index").grid(row=1, column=0, sticky="w", pady=6)
        ttk.Entry(self, textvariable=self.index, width=24).grid(row=1, column=1, sticky="w", padx=6)
        ttk.Checkbutton(self, text="Include subfolders", variable=self.recursive).grid(
            row=2, column=1, sticky="w", padx=6)

        bar = ttk.Frame(self)
        bar.grid(row=3, column=0, columnspan=3, sticky="ew", pady=10)
        self.btn_start = ttk.Button(bar, text="Start", command=self.start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(bar, text="Stop", command=self.stop_event.set, state="disabled")
        self.btn_stop.pack(side="left", padx=6)
        self.btn_count = ttk.Button(bar, text="Count only", command=lambda: self.start(dry_run=True))
        self.btn_count.pack(side="left")
        self.btn_embed = ttk.Button(bar, text="Create embeddings", command=self.start_embed)  # semantic search
        self.btn_embed.pack(side="left", padx=6)
        # deliberately apart from Start/Stop: deleting is permanent
        self.btn_delete = ttk.Button(bar, text="Delete index ...", command=self.delete_index)
        self.btn_delete.pack(side="right")

        self.bar = ttk.Progressbar(self, maximum=100)
        self.bar.grid(row=4, column=0, columnspan=3, sticky="ew")
        self.text = tk.Text(self, height=14, state="disabled", wrap="none", font=("Consolas", 9))
        self.text.grid(row=5, column=0, columnspan=3, sticky="nsew", pady=(8, 4))
        self.status = tk.StringVar(value="Ready.")
        ttk.Label(self, textvariable=self.status).grid(row=6, column=0, columnspan=3, sticky="w")
        self.after(200, self.poll)

    def pick(self):
        folder = filedialog.askdirectory(initialdir=self.folder.get() or None)
        if folder:
            self.folder.set(folder.replace("/", "\\"))

    def log(self, line):
        self.text.configure(state="normal")
        self.text.insert("end", line + "\n")
        self.text.see("end")
        self.text.configure(state="disabled")

    def set_running(self, running):
        self.btn_start.configure(state="disabled" if running else "normal")
        self.btn_count.configure(state="disabled" if running else "normal")
        self.btn_embed.configure(state="disabled" if running else "normal")
        self.btn_delete.configure(state="disabled" if running else "normal")
        self.btn_stop.configure(state="normal" if running else "disabled")

    def _es(self):
        if self.es is None:
            self.es = Es()
        return self.es

    def delete_index(self):
        """Delete the index named in the "Index" field after a warning and the typed name."""
        index = self.index.get().strip().lower()
        try:
            info = index_admin.describe(self._es(), index)
        except ValueError as e:
            messagebox.showerror("Delete index", str(e))
            return
        except index_admin.IndexNotFound:
            existing = ", ".join(name for name, _ in self._es().indices()) or "none"
            messagebox.showerror("Delete index", f"The index '{index}' does not exist.\nExisting indices: {existing}")
            return
        except (EsError, SystemExit, OSError) as e:
            messagebox.showerror("Delete index", f"Elasticsearch is not reachable: {e}")
            return
        typed = simpledialog.askstring("Delete index", index_admin.warning_text(index, info)
                                       + "\n\nType the index name to confirm:", parent=self)
        if typed is None:  # cancelled
            return
        try:
            index_admin.delete(self._es(), index, typed.strip())
        except ValueError as e:
            messagebox.showinfo("Delete index", str(e))
            return
        except (EsError, SystemExit, OSError) as e:
            messagebox.showerror("Delete index", f"The index could not be deleted: {e}")
            return
        message = f"Index '{index}' deleted ({info['documents']} documents)."
        self.status.set(message)
        self.log(message)
        if self.on_deleted:
            self.on_deleted(index)

    def start(self, dry_run=False):
        folder, index = self.folder.get().strip(), self.index.get().strip().lower()
        if not os.path.isdir(folder):
            messagebox.showerror("Folder", "Please choose an existing folder.")
            return
        if not indexing.valid_index_name(index):
            messagebox.showerror("Index", "Index name: only lowercase letters, digits, _ and -.")
            return
        self.settings.update(folder=folder, index=index, recursive=self.recursive.get())
        self.settings.save()
        self.kind = "File"
        self.stop_event.clear()
        self.bar.configure(value=0)
        self.set_running(True)
        opts = indexing.Options(data_dir=folder, index=index, recursive=self.recursive.get(), dry_run=dry_run)
        threading.Thread(target=self._work, args=(opts,), daemon=True).start()

    def start_embed(self):
        """Create the vectors of the index named in the "Index" field (semantic search; needs an embedding model)."""
        index = self.index.get().strip().lower()
        if not indexing.valid_index_name(index):
            messagebox.showerror("Index", "Index name: only lowercase letters, digits, _ and -.")
            return
        self.kind = "Document"
        self.stop_event.clear()
        self.bar.configure(value=0)
        self.set_running(True)
        threading.Thread(target=self._work_embed, args=(index,), daemon=True).start()

    def _work_embed(self, index):
        handler = QueueHandler(self.q)
        logging.getLogger("docsearch").addHandler(handler)
        try:
            message = embed_job.run_job(index, on_progress=lambda p: self.q.put(("progress", p)),
                                        should_stop=self.stop_event.is_set,
                                        on_status=lambda s: self.q.put(("status", s)))
            self.q.put(("done", (message, None)))
        except Exception as ex:
            self.q.put(("done", (f"Error: {ex}", None)))
        finally:
            logging.getLogger("docsearch").removeHandler(handler)

    def _work(self, opts):
        handler = QueueHandler(self.q)
        logging.getLogger("docsearch").addHandler(handler)
        try:
            message, index = indexing.run_job(opts, on_progress=lambda p: self.q.put(("progress", p)),
                                              should_stop=self.stop_event.is_set,
                                              on_status=lambda s: self.q.put(("status", s)))
            self.q.put(("done", (message, index)))
        except Exception as ex:
            self.q.put(("done", (f"Error: {ex}", None)))
        finally:
            logging.getLogger("docsearch").removeHandler(handler)

    def poll(self):
        try:
            while True:
                kind, value = self.q.get_nowait()
                if kind == "log":
                    self.log(value)
                elif kind == "status":
                    self.status.set(value)
                elif kind == "progress":
                    pct = 100 * value["done"] / max(value["total"], 1)
                    eta = f"{int(value['eta_s']) // 3600}h{int(value['eta_s']) % 3600 // 60:02d}m" if value["eta_s"] else "?"
                    self.bar.configure(value=pct)
                    self.status.set(f"{self.kind} {value['done']}/{value['total']} ({pct:.1f}%) | ok {value['ok']} | "
                                    f"errors {value['errors']} | skipped {value['skipped']} | "
                                    f"{value['rate']:.2f}/s | left {eta}")
                elif kind == "done":
                    message, index = value
                    self.status.set(message)
                    self.log(message)
                    self.set_running(False)
                    if index and self.on_finished:
                        self.on_finished(index)
        except queue.Empty:
            pass
        self.after(200, self.poll)

    def running(self):
        return str(self.btn_stop.cget("state")) == "normal"
