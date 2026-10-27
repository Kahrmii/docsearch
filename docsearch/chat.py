"""Conversation state shared by the window and the web interface."""
import logging
import re
import threading

from . import agent, config, conversations, llm, tools

log = logging.getLogger("docsearch.chat")


def cited_sources(answer, known):
    """Names of known sources that the answer quotes in [square brackets], in order."""
    names = []
    for group in re.findall(r"\[([^\[\]]+)\]", answer):
        for part in re.split(r"[,;]", group):
            if part.strip() in known and part.strip() not in names:
                names.append(part.strip())
    return names


class ChatSession:
    """History, sources and cancellation of one conversation. The user interfaces only display it."""

    def __init__(self, es_factory):
        self.es_factory = es_factory
        self.history = []
        self.sources = {}  # filename -> path of all sources mentioned so far
        self.terms = []  # search terms the model used, to show the matching passage of a source
        self.stop_event = threading.Event()
        self.last_target = None  # (index, provider) of the last question
        self.conv_id = self.title = None
        self.turns = []  # [{"question", "answer", "sources"}] of this conversation, saved to conversations/

    def reset(self):
        self.history.clear()
        self.terms.clear()
        self.conv_id = self.title = None
        self.turns = []

    def snapshot(self):
        """The conversation as it is saved."""
        index, provider = self.last_target or (None, None)
        return {"id": self.conv_id, "title": self.title, "index": index, "provider": provider,
                "turns": self.turns, "sources": self.sources}

    def restore(self, conv):
        """Continue a saved conversation. Its target is remembered, so asking with another index or model starts
        a fresh history instead of passing the old excerpts on."""
        self.reset()
        self.conv_id, self.title = conv["id"], conv.get("title")
        self.turns = list(conv["turns"])
        self.sources = dict(conv.get("sources") or {})
        self.history = [m for t in self.turns for m in ({"role": "user", "content": t["question"]},
                                                         {"role": "assistant", "content": t["answer"]})]
        self.last_target = (conv.get("index"), conv.get("provider"))

    def _save(self):
        if not config.SAVE_CONVERSATIONS:
            return
        try:
            conversations.save(self.snapshot())
        except (OSError, ValueError) as e:  # saving must never cost the user the answer
            log.warning("Could not save the conversation: %s", e)

    def _note_terms(self, args):
        """Remember the words the model searched for (for the preview of a source)."""
        found = []
        for key in ("phrases", "phrase", "around"):
            value = args.get(key)
            found += value if isinstance(value, list) else [value]
        if isinstance(args.get("query"), str):
            found += [w for w in args["query"].split() if len(w) > 2]
        for term in found:
            if isinstance(term, str) and term.strip() and term.strip() not in self.terms:
                self.terms.append(term.strip())
        del self.terms[:-20]

    def preview(self, filename, width=900, limit=4):
        """The passages of a source that match what was searched, or None if it is not a known source.

        Returns {"filename", "terms", "passages", "length"}. Reads the document from the index of the last question;
        nothing leaves the computer."""
        path = self.sources.get(filename)
        if path is None or self.last_target is None:
            return None
        result = self.es_factory().search(self.last_target[0], {
            "size": 1, "_source": ["content"],
            "query": {"bool": {"filter": [{"term": {"filename": filename}}, {"term": {"path": path}}]}}})
        hits = result["hits"]["hits"]
        if not hits:
            return None
        text = hits[0]["_source"].get("content") or ""
        return {"filename": filename, "terms": list(self.terms), "length": len(text),
                "passages": tools.windows(text, self.terms, width, limit)}

    def switch_target(self, index, provider):
        """Drop the history when the index or the model changed. It contains text excerpts of the previous target
        and must not unintentionally reach another model or the cloud. Returns True if it was dropped."""
        dropped = bool(self.history) and self.last_target not in (None, (index, provider))
        if dropped:
            self.history.clear()
        self.last_target = (index, provider)
        return dropped

    def run(self, question, index, provider, emit):
        """Answer a question (blocking, call it in a worker thread).

        `emit(kind, value)` with kind "status" (text), "step" ((tool name, arguments)), "answer" (text) or
        "error" (text). The cloud approval is checked inside `llm.get_client`, before anything is sent."""
        self.stop_event.clear()
        self.last_target = (index, provider)
        try:
            client = llm.get_client(provider, index)
            emit("status", f"Working ... ({client.label()})")
            ctx = tools.Context(self.es_factory(), index, client.context_chars(), self.stop_event.is_set,
                                progress=lambda message: emit("status", message))
            answer = agent.ask(question, ctx, client, self.history, on_step=lambda n, a: (self._note_terms(a), emit("step", (n, a))))
            self.sources.update(ctx.sources)
            self.history.extend([{"role": "user", "content": question}, {"role": "assistant", "content": answer}])
            self.conv_id = self.conv_id or conversations.new_id()
            self.title = self.title or conversations.default_title(question)
            self.turns.append({"question": question, "answer": answer,
                               "sources": cited_sources(answer, self.sources)})
            self._save()
            emit("answer", answer)
        except agent.Cancelled:
            emit("error", "Cancelled.")
        except (Exception, SystemExit) as ex:  # EsError, LlmError, missing API key (SystemExit) ...
            emit("error", str(ex))
