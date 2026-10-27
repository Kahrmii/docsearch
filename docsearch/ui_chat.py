"""Tab "Ask": chat with the agent, sources are clickable."""
import json
import queue
import re
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from . import cloud_policy, files, llm, secrets_check
from .chat import ChatSession
from .es import Es, EsError


class ChatTab(ttk.Frame):
    def __init__(self, master, settings):
        super().__init__(master, padding=8)
        self.settings = settings
        self.q = queue.Queue()
        self.es = None
        self.session = ChatSession(self._es)  # history, sources and cancellation of the conversation
        self.busy = False
        self.link_count = 0
        self.cloud_confirmed = set()  # indices the user accepted for the cloud despite a secrets warning

        bar = ttk.Frame(self)
        bar.pack(fill="x")
        ttk.Label(bar, text="Index").pack(side="left")
        self.index = tk.StringVar(value=settings.get("chat_index", "documents"))
        self.combo = ttk.Combobox(bar, textvariable=self.index, width=28)
        self.combo.pack(side="left", padx=6)
        self.combo.bind("<<ComboboxSelected>>", lambda e: self.sync_cloud_box())
        ttk.Button(bar, text="Refresh", command=self.refresh_indices).pack(side="left")
        ttk.Button(bar, text="New conversation", command=self.reset).pack(side="right")

        model_bar = ttk.Frame(self)
        model_bar.pack(fill="x", pady=(6, 0))
        ttk.Label(model_bar, text="Model").pack(side="left")
        self.provider = tk.StringVar(value=llm.PROVIDER_LABELS.get(settings.get("chat_provider"),
                                                                    llm.PROVIDER_LABELS["local"]))
        ttk.Combobox(model_bar, textvariable=self.provider, values=list(llm.PROVIDER_LABELS.values()),
                     state="readonly", width=20).pack(side="left", padx=6)
        self.cloud_ok = tk.BooleanVar()
        ttk.Checkbutton(model_bar, text="Allow cloud for this index (text excerpts may be sent)",
                        variable=self.cloud_ok, command=self.toggle_cloud).pack(side="left", padx=6)

        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True, pady=6)
        self.text = tk.Text(frame, wrap="word", state="disabled", font=("Segoe UI", 10), padx=6, pady=6)
        scroll = ttk.Scrollbar(frame, command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        self.text.tag_configure("user", font=("Segoe UI", 10, "bold"))
        self.text.tag_configure("step", foreground="#777777", font=("Consolas", 8))
        self.text.tag_configure("error", foreground="#b00020")

        entry = ttk.Frame(self)
        entry.pack(fill="x")
        self.entry = ttk.Entry(entry)
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<Return>", lambda e: self.send())
        self.btn_send = ttk.Button(entry, text="Send", command=self.send)
        self.btn_send.pack(side="left", padx=6)
        self.btn_stop = ttk.Button(entry, text="Stop", command=self.session.stop_event.set, state="disabled")
        self.btn_stop.pack(side="left")
        self.status = tk.StringVar(value="Ready.")
        ttk.Label(self, textvariable=self.status).pack(anchor="w", pady=(4, 0))

        self.refresh_indices()
        self.sync_cloud_box()
        self.after(150, self.poll)

    # ---- Cloud approval
    def provider_key(self):
        return next((key for key, label in llm.PROVIDER_LABELS.items() if label == self.provider.get()), "local")

    def sync_cloud_box(self):
        self.cloud_ok.set(cloud_policy.is_allowed(self.index.get().strip().lower()))

    def toggle_cloud(self):
        index = self.index.get().strip().lower()
        if self.cloud_ok.get() and not self.confirm_cloud(index):
            self.cloud_ok.set(False)
            return
        cloud_policy.set_allowed(index, self.cloud_ok.get())

    def confirm_cloud(self, index):
        """True if sending excerpts of this index to the cloud is fine: nothing suspicious found, or the user
        confirmed after a warning. The check only counts documents; it never shows secret values."""
        if index in self.cloud_confirmed:
            return True
        try:
            findings = secrets_check.scan(self._es(), index)
        except (EsError, SystemExit, OSError) as e:
            self.write(f"Could not check the index for keys and credentials ({e}); nothing was sent.\n\n", "error")
            return False
        if not secrets_check.risky(findings):
            self.cloud_confirmed.add(index)
            return True
        if messagebox.askyesno("Keys or credentials in this index?",
                               secrets_check.describe(index, findings) + "\n\nSend text excerpts to the cloud anyway?",
                               icon="warning", default="no"):
            self.cloud_confirmed.add(index)
            return True
        return False

    # ---- Index selection
    def _es(self):
        if self.es is None:
            self.es = Es()
        return self.es

    def refresh_indices(self, select=None):
        try:
            names = [name for name, _ in self._es().indices()]
        except (EsError, SystemExit, OSError) as e:
            self.status.set(f"Elasticsearch is not reachable: {e}")
            return
        self.combo.configure(values=names)
        if select:
            self.index.set(select)
        self.sync_cloud_box()

    def reset(self):
        self.session.reset()
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")
        self.status.set("New conversation.")

    # ---- Output
    def write(self, text, tag=None):
        self.text.configure(state="normal")
        self.text.insert("end", text, tag)
        self.text.see("end")
        self.text.configure(state="disabled")

    def write_answer(self, answer):
        """Insert the answer; filenames in [..] that are known sources become clickable."""
        self.text.configure(state="normal")
        start = self.text.index("end-1c")
        self.text.insert("end", answer + "\n\n")
        for group in re.finditer(r"\[([^\[\]]+)\]", answer):
            for name_match in re.finditer(r"[^,;]+", group.group(1)):
                name = name_match.group().strip()
                path = self.session.sources.get(name)
                if not path:
                    continue
                begin = group.start(1) + name_match.start() + name_match.group().index(name)
                tag = f"link{self.link_count}"
                self.link_count += 1
                self.text.tag_add(tag, f"{start}+{begin}c", f"{start}+{begin + len(name)}c")
                self.text.tag_configure(tag, foreground="#0b57d0", underline=True)
                self.text.tag_bind(tag, "<Button-1>", lambda e, p=path: files.open_source(p))
                self.text.tag_bind(tag, "<Enter>", lambda e: self.text.configure(cursor="hand2"))
                self.text.tag_bind(tag, "<Leave>", lambda e: self.text.configure(cursor=""))
        self.text.see("end")
        self.text.configure(state="disabled")

    # ---- Send a question
    def send(self):
        question = self.entry.get().strip()
        index = self.index.get().strip().lower()
        if self.busy or not question or not index:
            return
        provider = self.provider_key()
        if provider != "local" and not cloud_policy.is_allowed(index):
            self.write(f"Index '{index}' is not approved for the cloud; nothing was sent. "
                       "Tick 'Allow cloud for this index' or choose the local model.\n\n", "error")
            return
        if provider != "local" and not self.confirm_cloud(index):
            self.write("Cancelled: nothing was sent to the cloud. Choose the local model for this index.\n\n",
                       "error")
            return
        self.entry.delete(0, "end")
        self.settings.update(chat_index=index, chat_provider=provider)
        self.settings.save()
        if self.session.switch_target(index, provider):  # drops the history of the previous index/model
            self.write("(New conversation: index or model changed.)\n", "step")
        self.write(f"You: {question}\n", "user")
        self.busy = True
        self.btn_send.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.status.set("Working ...")
        threading.Thread(target=self._work, args=(question, index, provider), daemon=True).start()

    def _work(self, question, index, provider):
        try:
            self.session.run(question, index, provider, lambda kind, value: self.q.put((kind, value)))
        finally:
            self.q.put(("done", None))

    def poll(self):
        try:
            while True:
                kind, value = self.q.get_nowait()
                if kind == "step":
                    name, args = value
                    shown = ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in args.items())
                    self.write(f"  -> {name}({shown[:160]})\n", "step")
                    self.status.set(f"Tool: {name} ...")
                elif kind == "status":
                    self.status.set(value)
                elif kind == "answer":
                    self.write_answer(value or "(no answer)")
                elif kind == "error":
                    self.write(value + "\n\n", "error")
                elif kind == "done":
                    self.busy = False
                    self.btn_send.configure(state="normal")
                    self.btn_stop.configure(state="disabled")
                    self.status.set("Ready.")
        except queue.Empty:
            pass
        self.after(150, self.poll)
