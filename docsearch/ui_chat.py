"""Tab "Ask": chat with the agent, sources are clickable."""
import json
import queue
import re
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from . import cloud_policy, conversations, files, llm, secrets_check
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
        ttk.Button(bar, text="Export ...", command=self.export).pack(side="right", padx=6)
        ttk.Button(bar, text="History ...", command=self.history).pack(side="right")

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
        elif names and self.index.get().strip().lower() not in names:  # e.g. the index was deleted
            self.index.set(names[0])
        self.sync_cloud_box()

    def index_deleted(self, index):
        self.cloud_confirmed.discard(index)
        self.refresh_indices()

    def reset(self):
        self.session.reset()
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")
        self.status.set("New conversation.")

    # ---- Saved conversations
    def export(self):
        """Save the current conversation as a Markdown file."""
        if not self.session.turns:
            messagebox.showinfo("Export", "There is nothing to export yet.")
            return
        conv = {**self.session.snapshot(), "updated": ""}
        path = filedialog.asksaveasfilename(defaultextension=".md", filetypes=[("Markdown", "*.md")],
                                            initialfile=(conv["title"] or "conversation")[:40].strip() + ".md")
        if path:
            Path(path).write_text(conversations.to_markdown(conv), encoding="utf-8")
            self.status.set(f"Exported to {path}")

    def open_conversation(self, conv):
        self.session.restore(conv)
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")
        self.write(f"(Conversation loaded: {conv.get('title') or 'untitled'})\n", "step")
        for turn in conv["turns"]:
            self.write(f"You: {turn['question']}\n", "user")
            self.write_answer(turn["answer"])
        if conv.get("index"):
            self.index.set(conv["index"])
        self.provider.set(llm.PROVIDER_LABELS.get(conv.get("provider"), self.provider.get()))
        self.sync_cloud_box()

    def history(self):
        """Window with the saved conversations: open, rename or delete one."""
        window = tk.Toplevel(self)
        window.title("Saved conversations")
        window.geometry("560x320")
        items = []
        box = tk.Listbox(window, activestyle="dotbox")
        box.pack(fill="both", expand=True, padx=8, pady=8)

        def fill():
            items[:] = conversations.listing()
            box.delete(0, "end")
            for c in items:
                box.insert("end", f"{c['updated'][:16].replace('T', ' ')}  {c['title']}  ({c['questions']})")

        def selected():
            return items[box.curselection()[0]] if box.curselection() else None

        def open_it():
            item = selected()
            conv = conversations.load(item["id"]) if item else None
            if conv is None:
                return
            if self.busy:
                messagebox.showinfo("History", "Still working on a question; stop it first.", parent=window)
                return
            self.open_conversation(conv)
            window.destroy()

        def rename():
            item = selected()
            title = simpledialog.askstring("Rename", "New title:", initialvalue=item["title"], parent=window) \
                if item else None
            if title and conversations.rename(item["id"], title):
                if self.session.conv_id == item["id"]:
                    self.session.title = title
                fill()

        def delete():
            item = selected()
            if item and messagebox.askyesno("Delete", f"Delete '{item['title']}' permanently?", parent=window):
                conversations.delete(item["id"])
                if self.session.conv_id == item["id"] and not self.busy:
                    self.reset()  # otherwise the next answer would bring the file back
                fill()

        bar = ttk.Frame(window)
        bar.pack(fill="x", padx=8, pady=(0, 8))
        for label, command in (("Open", open_it), ("Rename", rename), ("Delete", delete)):
            ttk.Button(bar, text=label, command=command).pack(side="left", padx=(0, 6))
        box.bind("<Double-Button-1>", lambda e: open_it())
        fill()

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
                self.text.tag_bind(tag, "<Button-1>", lambda e, n=name, p=path: self.show_preview(n, p))
                self.text.tag_bind(tag, "<Enter>", lambda e: self.text.configure(cursor="hand2"))
                self.text.tag_bind(tag, "<Leave>", lambda e: self.text.configure(cursor=""))
        self.text.see("end")
        self.text.configure(state="disabled")

    def show_preview(self, name, path):
        """Window with the passages of a source that match the search, and a button to open the file."""
        try:
            preview = self.session.preview(name)
        except (EsError, SystemExit, OSError) as ex:
            messagebox.showerror("Preview", f"Elasticsearch is not reachable: {ex}")
            return
        if preview is None:
            files.open_source(path)  # no preview available: open the file as before
            return
        window = tk.Toplevel(self)
        window.title(name)
        window.geometry("720x420")
        bar = ttk.Frame(window, padding=6)
        bar.pack(fill="x")
        ttk.Label(bar, text=f"{preview['length']} characters of text").pack(side="left")
        ttk.Button(bar, text="Open file", command=lambda: files.open_source(path)).pack(side="right")
        toggle = ttk.Button(bar, text="Show full text")
        toggle.pack(side="right", padx=4)
        box = tk.Text(window, wrap="word", font=("Segoe UI", 10), padx=8, pady=6)
        box.pack(fill="both", expand=True)
        box.tag_configure("hit", background="#ffe066", foreground="#1d2330")
        terms = [t for t in preview["terms"] if t]
        pattern = re.compile("(" + "|".join(re.escape(t) for t in terms) + ")", re.I) if terms else None

        def fill(full):
            data = self.session.preview(name, full=full) or preview
            toggle.configure(text="Show passages" if full else "Show full text",
                             command=lambda: fill(not full))
            box.configure(state="normal")
            box.delete("1.0", "end")
            for passage in data["passages"]:
                if not full:
                    box.insert("end", "… ")
                for i, piece in enumerate(pattern.split(passage) if pattern else [passage]):
                    box.insert("end", piece, "hit" if i % 2 else "")
                box.insert("end", "\n" if full else " …\n\n")
            box.configure(state="disabled")

        fill(False)

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
