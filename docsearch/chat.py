"""Conversation state shared by the window and the web interface."""
import threading

from . import agent, llm, tools


class ChatSession:
    """History, sources and cancellation of one conversation. The user interfaces only display it."""

    def __init__(self, es_factory):
        self.es_factory = es_factory
        self.history = []
        self.sources = {}  # filename -> path of all sources mentioned so far
        self.stop_event = threading.Event()
        self.last_target = None  # (index, provider) of the last question

    def reset(self):
        self.history.clear()

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
        try:
            client = llm.get_client(provider, index)
            emit("status", f"Working ... ({client.label()})")
            ctx = tools.Context(self.es_factory(), index, client.context_chars(), self.stop_event.is_set,
                                progress=lambda message: emit("status", message))
            answer = agent.ask(question, ctx, client, self.history, on_step=lambda n, a: emit("step", (n, a)))
            self.sources.update(ctx.sources)
            self.history.extend([{"role": "user", "content": question}, {"role": "assistant", "content": answer}])
            emit("answer", answer)
        except agent.Cancelled:
            emit("error", "Cancelled.")
        except (Exception, SystemExit) as ex:  # EsError, LlmError, missing API key (SystemExit) ...
            emit("error", str(ex))
