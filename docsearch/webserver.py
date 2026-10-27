"""Local web interface (standard library only): the same functions as the window, in the browser.

Security:
- binds to 127.0.0.1 only;
- every API call needs the random token of this server run (given in the URL printed at start);
- the Host and Origin headers are checked (protects against DNS rebinding and requests from other websites);
- the page loads only its own scripts (Content-Security-Policy) and renders all text with textContent;
- files are opened only if the conversation named them as a source; the browser never sends a path."""
import json
import logging
import queue
import secrets
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import cloud_policy, config, files, indexing, llm, secrets_check, services
from .chat import ChatSession
from .es import Es, EsError
from .ui_common import QueueHandler, Settings

log = logging.getLogger("docsearch.web")
HOST = "127.0.0.1"
STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_TYPES = {"app.js": "text/javascript; charset=utf-8", "app.css": "text/css; charset=utf-8"}
MAX_BODY = 1_000_000
MAX_CHATS = 50
CSP = "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
PICK_FOLDER = ("import tkinter as tk\nfrom tkinter import filedialog\nr = tk.Tk()\nr.withdraw()\n"
               "r.attributes('-topmost', True)\nprint(filedialog.askdirectory() or '')")


class ChatRun:
    """One browser conversation: the shared ChatSession plus the events the page polls."""

    def __init__(self, es_factory):
        self.session = ChatSession(es_factory)
        self.events = []
        self.busy = False
        self.lock = threading.Lock()

    def emit(self, event):
        with self.lock:
            self.events.append(event)

    def _on_event(self, kind, value):
        if kind == "status":
            self.emit({"type": "status", "text": value})
        elif kind == "step":
            self.emit({"type": "step", "name": value[0], "args": value[1]})
        elif kind == "answer":
            self.emit({"type": "answer", "text": value or "(no answer)", "sources": sorted(self.session.sources)})
        else:
            self.emit({"type": "error", "text": value})

    def start(self, question, index, provider):
        with self.lock:
            if self.busy:
                return False
            self.busy = True
        if self.session.switch_target(index, provider):
            self.emit({"type": "info", "text": "(New conversation: index or model changed.)"})
        self.emit({"type": "user", "text": question})
        threading.Thread(target=self._work, args=(question, index, provider), daemon=True).start()
        return True

    def _work(self, question, index, provider):
        try:
            self.session.run(question, index, provider, self._on_event)
        finally:
            with self.lock:
                self.busy = False
            self.emit({"type": "done"})

    def poll(self, since):
        with self.lock:
            return {"events": self.events[since:], "next": len(self.events), "busy": self.busy}

    def reset(self):
        with self.lock:
            self.session.reset()
            self.events.clear()


class IndexRun:
    """The indexing job started from the page (one at a time)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.stop = threading.Event()
        self.q = queue.Queue()
        self.log, self.progress, self.status, self.message = [], None, "", ""

    def start(self, opts):
        with self.lock:
            if self.running:
                return False
            self.running = True
        self.stop.clear()
        self.q = queue.Queue()
        self.log, self.progress, self.status, self.message = [], None, "Starting ...", ""
        threading.Thread(target=self._work, args=(opts,), daemon=True).start()
        return True

    def _work(self, opts):
        handler = QueueHandler(self.q)
        logging.getLogger("docsearch").addHandler(handler)
        try:
            message, _ = indexing.run_job(opts, on_progress=lambda p: setattr(self, "progress", p),
                                          should_stop=self.stop.is_set,
                                          on_status=lambda s: setattr(self, "status", s))
        except Exception as ex:
            message = f"Error: {ex}"
        finally:
            logging.getLogger("docsearch").removeHandler(handler)
        self.message = message
        with self.lock:
            self.running = False

    def snapshot(self, since):
        try:
            while True:
                self.log.append(self.q.get_nowait()[1])
        except queue.Empty:
            pass
        return {"running": self.running, "progress": self.progress, "status": self.status,
                "message": self.message, "log": self.log[since:], "next": len(self.log)}


class WebState:
    def __init__(self):
        self.token = secrets.token_urlsafe(32)
        self.settings = Settings()
        self.docker_status = "Checking the Docker services ..."
        self.cloud_confirmed = set()  # indices accepted for the cloud despite a keys/credentials warning
        self.index_run = IndexRun()
        self.chats = {}
        self.lock = threading.Lock()
        self._es = None

    def es(self):
        if self._es is None:
            self._es = Es()
        return self._es

    def chat(self, chat_id):
        with self.lock:
            if chat_id not in self.chats:
                if len(self.chats) >= MAX_CHATS:  # forget the oldest conversation
                    self.chats.pop(next(iter(self.chats)))
                self.chats[chat_id] = ChatRun(self.es)
            return self.chats[chat_id]


class BadRequest(Exception):
    pass


def _text(body, key, default=""):
    value = body.get(key, default)
    if not isinstance(value, str):
        raise BadRequest(f"'{key}' must be text")
    return value.strip()


def _check_cloud(state, index, confirm):
    """None if sending excerpts of this index to the cloud is fine, otherwise the (status, payload) to answer with."""
    if index in state.cloud_confirmed:
        return None
    try:
        findings = secrets_check.scan(state.es(), index)
    except (EsError, SystemExit, OSError) as e:
        return 502, {"error": f"Could not check the index for keys and credentials ({e}); nothing was sent."}
    if secrets_check.risky(findings) and not confirm:
        return 200, {"needs_confirmation": True,
                     "message": secrets_check.describe(index, findings) + "\n\nSend text excerpts to the cloud anyway?"}
    state.cloud_confirmed.add(index)
    return None


# ---- API routes: (state, query, body) -> (status, payload) -------------------------------------

def api_state(state, query, body):
    indices, es_error = [], None
    try:
        indices = [{"name": name, "documents": count, "cloud_allowed": cloud_policy.is_allowed(name)}
                   for name, count in state.es().indices()]
    except (EsError, SystemExit, OSError) as e:
        es_error = str(e)
    return 200, {"providers": [{"key": k, "label": v} for k, v in llm.PROVIDER_LABELS.items()],
                 "indices": indices, "es_error": es_error, "docker": state.docker_status,
                 "settings": dict(state.settings)}


def api_pick_folder(state, query, body):
    """Native folder dialog on this machine (the browser cannot show server-side folders)."""
    try:
        result = subprocess.run([sys.executable, "-c", PICK_FOLDER], capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.SubprocessError) as e:
        return 200, {"path": "", "error": f"The folder dialog is not available: {e}"}
    path = result.stdout.strip().replace("/", "\\") if sys.platform == "win32" else result.stdout.strip()
    return 200, {"path": path}


def api_index_start(state, query, body):
    folder, index = _text(body, "folder"), _text(body, "index").lower()
    if not Path(folder).is_dir():
        raise BadRequest("Please choose an existing folder.")
    if not indexing.valid_index_name(index):
        raise BadRequest("Index name: only lowercase letters, digits, _ and -.")
    recursive = bool(body.get("recursive"))
    state.settings.update(folder=folder, index=index, recursive=recursive)
    state.settings.save()
    opts = indexing.Options(data_dir=folder, index=index, recursive=recursive, dry_run=bool(body.get("dry_run")))
    if not state.index_run.start(opts):
        return 409, {"error": "An indexing job is already running."}
    return 200, {"ok": True}


def api_index_stop(state, query, body):
    state.index_run.stop.set()
    return 200, {"ok": True}


def api_index_status(state, query, body):
    return 200, state.index_run.snapshot(int(query.get("since", 0) or 0))


def api_cloud(state, query, body):
    index, allowed = _text(body, "index").lower(), bool(body.get("allowed"))
    if not index:
        raise BadRequest("'index' is required")
    if allowed:
        response = _check_cloud(state, index, bool(body.get("confirm")))
        if response:
            return response
    cloud_policy.set_allowed(index, allowed)
    return 200, {"ok": True, "cloud_allowed": allowed}


def api_chat_ask(state, query, body):
    question, index, provider = _text(body, "question"), _text(body, "index").lower(), _text(body, "provider", "local")
    chat_id = _text(body, "chat_id")
    if not question or not index or not chat_id:
        raise BadRequest("'chat_id', 'question' and 'index' are required")
    if provider not in llm.PROVIDER_LABELS:
        raise BadRequest(f"unknown provider '{provider}'")
    if provider != "local":
        if not cloud_policy.is_allowed(index):
            return 403, {"error": f"Index '{index}' is not approved for the cloud; nothing was sent. "
                                  "Tick 'Allow cloud for this index' or choose the local model."}
        response = _check_cloud(state, index, bool(body.get("confirm")))
        if response:
            return response
    state.settings.update(chat_index=index, chat_provider=provider)
    state.settings.save()
    if not state.chat(chat_id).start(question, index, provider):
        return 409, {"error": "Still working on the previous question."}
    return 200, {"ok": True}


def api_chat_poll(state, query, body):
    return 200, state.chat(query.get("chat_id", "")).poll(int(query.get("since", 0) or 0))


def api_chat_stop(state, query, body):
    state.chat(_text(body, "chat_id")).session.stop_event.set()
    return 200, {"ok": True}


def api_chat_reset(state, query, body):
    chat = state.chat(_text(body, "chat_id"))
    if chat.busy:
        return 409, {"error": "Still working on a question; stop it first."}
    chat.reset()
    return 200, {"ok": True}


def api_open(state, query, body):
    chat = state.chats.get(_text(body, "chat_id"))
    path = chat.session.sources.get(_text(body, "filename")) if chat else None  # only files named as a source
    if not path or not files.open_source(path):
        return 404, {"error": "That file is not available."}
    return 200, {"ok": True}


ROUTES = {
    ("GET", "/api/state"): api_state,
    ("GET", "/api/pick-folder"): api_pick_folder,
    ("POST", "/api/index/start"): api_index_start,
    ("POST", "/api/index/stop"): api_index_stop,
    ("GET", "/api/index/status"): api_index_status,
    ("POST", "/api/cloud"): api_cloud,
    ("POST", "/api/chat/ask"): api_chat_ask,
    ("GET", "/api/chat/poll"): api_chat_poll,
    ("POST", "/api/chat/stop"): api_chat_stop,
    ("POST", "/api/chat/reset"): api_chat_reset,
    ("POST", "/api/open"): api_open,
}


class Handler(BaseHTTPRequestHandler):
    state = None  # set in make_server
    server_version = "docsearch"

    def log_message(self, fmt, *args):
        log.debug(fmt, *args)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    # -- security checks
    def _allowed_hosts(self):
        port = self.server.server_address[1]
        return {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}

    def _headers_ok(self):
        origin = self.headers.get("Origin")
        origins = {f"http://{host}" for host in self._allowed_hosts()}
        return self.headers.get("Host", "") in self._allowed_hosts() and (origin is None or origin in origins)

    def _token_ok(self, supplied):
        return secrets.compare_digest((supplied or "").encode(), self.state.token.encode())

    # -- responses
    def _send(self, status, data, content_type, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, status, payload):
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _page(self, token):
        if not self._token_ok(token):
            return self._send(403, b"Forbidden: open the address printed when the server started.",
                              "text/plain; charset=utf-8")
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8").replace("{{TOKEN}}", self.state.token)
        self._send(200, html.encode("utf-8"), "text/html; charset=utf-8",
                   {"Content-Security-Policy": CSP, "Referrer-Policy": "no-referrer"})

    def _static(self, name):
        if name not in STATIC_TYPES:
            return self._json(404, {"error": "not found"})
        self._send(200, (STATIC_DIR / name).read_bytes(), STATIC_TYPES[name])

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise BadRequest("request too large")
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError as e:
            raise BadRequest("invalid JSON") from e
        if not isinstance(body, dict):
            raise BadRequest("JSON object expected")
        return body

    def _dispatch(self, method):
        try:
            url = urlparse(self.path)
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            if not self._headers_ok():
                return self._json(403, {"error": "forbidden"})
            if method == "GET" and url.path == "/":
                return self._page(query.get("token"))
            if method == "GET" and url.path.startswith("/static/"):
                return self._static(url.path[len("/static/"):])
            route = ROUTES.get((method, url.path))
            if route is None:
                return self._json(404, {"error": "not found"})
            if not self._token_ok(self.headers.get("X-Docsearch-Token")):
                return self._json(403, {"error": "forbidden"})
            status, payload = route(self.state, query, self._body() if method == "POST" else {})
            self._json(status, payload)
        except BadRequest as e:
            self._json(400, {"error": str(e)})
        except (Exception, SystemExit) as e:  # SystemExit: missing password/key raised by the configuration
            log.exception("request failed")
            self._json(500, {"error": f"{type(e).__name__}: {e}"})


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets several programs listen on the same port, so another server (or program) could
    # share or take over ours. An exclusive bind there makes a busy port an error. Elsewhere SO_REUSEADDR only
    # allows a quick restart and is harmless.
    allow_reuse_address = sys.platform != "win32"


def make_server(state, port=8765):
    """HTTP server on 127.0.0.1; takes the next free port if `port` is busy."""
    handler = type("BoundHandler", (Handler,), {"state": state})
    for candidate in range(port, port + 20):
        try:
            return LocalServer((HOST, candidate), handler)
        except OSError:
            continue
    raise OSError(f"No free port between {port} and {port + 19}")


def start_services_in_background(state):
    """Create .env if needed and start the Docker services without blocking the server."""
    config.ensure_env()

    def set_status(text):
        state.docker_status = text

    threading.Thread(target=services.ensure_running, args=(set_status,), daemon=True).start()
