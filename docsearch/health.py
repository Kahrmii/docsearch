"""Health overview: are the services up, is there room left, is the model's context large enough?

Every check answers {"name", "status" (ok / warn / error), "detail"} and never raises, so one broken
service cannot hide the others. `python -m docsearch.health` prints the same report in the terminal."""
import json
import subprocess
import urllib.request

from . import __version__, config, services
from .es import Es, EsError

OK, WARN, ERROR = "ok", "warn", "error"
MIN_CONTEXT = 8192        # tokens; below this the agent has to cut tool results very short
DISK_WARN_PERCENT = 85    # Elasticsearch stops accepting writes at 95 %
HEAP_WARN_PERCENT = 90
MEMORY_WARN_PERCENT = 85  # a Docker container above this is close to being killed (out of memory)


def _check(name, status, detail):
    return {"name": name, "status": status, "detail": detail}


def _get_json(url, timeout=4):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def container_memory():
    """{container name: (memory text, percent)} for the docsearch containers; {} if Docker does not answer."""
    try:
        out = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{.Name}}|{{.MemUsage}}|{{.MemPerc}}"],
                             capture_output=True, text=True, timeout=30, creationflags=services.NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    result = {}
    for line in out.splitlines():
        parts = line.strip().split("|")
        if len(parts) == 3 and parts[0].startswith("docsearch-"):
            try:
                result[parts[0]] = (parts[1].strip(), float(parts[2].strip().rstrip("%")))
            except ValueError:
                pass
    return result


def check_elasticsearch(es=None):
    try:
        es = es or Es()
        health = es.call("GET", "/_cluster/health")
    except (EsError, SystemExit, OSError) as e:
        return _check("Elasticsearch", ERROR, str(e))
    cluster = health.get("status", "?")
    status = {"green": OK, "yellow": WARN}.get(cluster, ERROR)
    detail = [f"cluster {cluster}"]
    try:
        node = next(iter(es.call("GET", "/_nodes/stats/fs,jvm")["nodes"].values()))
        total, free = node["fs"]["total"]["total_in_bytes"], node["fs"]["total"]["available_in_bytes"]
        used = 100 * (total - free) / total
        heap = node["jvm"]["mem"]["heap_used_percent"]
        detail.append(f"disk {used:.0f}% used, {free / 1024 ** 3:.0f} GB free")
        detail.append(f"heap {heap}%")
        if used >= DISK_WARN_PERCENT or heap >= HEAP_WARN_PERCENT:
            status = WARN if status == OK else status
            detail.append("(running full)")
    except (EsError, KeyError, StopIteration, ZeroDivisionError, OSError):
        pass
    return _check("Elasticsearch", status, ", ".join(detail))


def check_tika():
    version = services.tika_version()
    if version is None:
        return _check("Tika", ERROR, f"not reachable at {config.TIKA_URL}. Are the Docker containers running?")
    return _check("Tika", OK, version)


def check_containers():
    memory = container_memory()
    if not memory:
        return _check("Docker memory", WARN, "no data (is Docker running?)")
    status, detail = OK, []
    for name, (text, percent) in sorted(memory.items()):
        detail.append(f"{name} {text} ({percent:.0f}%)")
        if percent >= MEMORY_WARN_PERCENT:
            status = WARN
    if status == WARN:
        detail.append("a container close to its limit may be killed: lower the Tika workers or raise the limit")
    return _check("Docker memory", status, ", ".join(detail))


def check_llm():
    """The local model: reachable, loaded, and with a context that is big enough."""
    needed = config.LLM_PROVIDER == "local"
    try:
        models = _get_json(config.LLM_BASE_URL + "/api/v0/models")["data"]
    except Exception as e:
        reason = getattr(e, "reason", None) or e
        return _check("Local model", ERROR if needed else WARN,
                      f"LM Studio not reachable at {config.LLM_BASE_URL} ({reason}). "
                      + ("Start its server." if needed else "Only needed for the local model."))
    loaded = {m["id"]: m for m in models if m.get("state") == "loaded" and m.get("type") != "embeddings"}
    model = loaded.get(config.LLM_MODEL)
    if model is None:
        names = ", ".join(sorted(loaded)) or "nothing"
        return _check("Local model", ERROR if needed else WARN,
                      f"{config.LLM_MODEL} is not loaded (loaded: {names}).")
    context = model.get("loaded_context_length")
    detail = f"{config.LLM_MODEL} loaded, context {context} tokens"
    if context and context < MIN_CONTEXT:
        return _check("Local model", WARN, detail + f". Below {MIN_CONTEXT}: answers get cut short. "
                                                    "Raise the context length when loading the model in LM Studio.")
    return _check("Local model", OK, detail)


def check_embeddings():
    """The embedding model for the semantic search (only needed to create vectors and to ask with them)."""
    try:
        models = _get_json(config.LLM_BASE_URL + "/api/v0/models")["data"]
    except Exception:
        return _check("Embedding model", OK, "LM Studio not reachable (only needed for the semantic search)")
    match = next((m for m in models if m["id"] == config.EMBED_MODEL), None)
    if match is None:
        return _check("Embedding model", OK, f"{config.EMBED_MODEL} is not installed in LM Studio "
                                              "(only needed for the semantic search)")
    if match.get("state") != "loaded":
        return _check("Embedding model", OK, f"{config.EMBED_MODEL} is installed but not loaded; load it before "
                                              "creating embeddings or asking with the semantic search")
    return _check("Embedding model", OK, f"{config.EMBED_MODEL} loaded")


def check_cloud():
    if config.get("ANTHROPIC_API_KEY"):
        return _check("Cloud model", OK, f"API key is set; model {config.ANTHROPIC_MODEL}")  # the key is never shown
    return _check("Cloud model", OK, "no API key (only needed for the Claude cloud mode)")


def check_config():
    slow = [name for name, url in (("ES_URL", config.ES_URL), ("TIKA_URL", config.TIKA_URL),
                                   ("LLM_BASE_URL", config.LLM_BASE_URL)) if "localhost" in url]
    if slow:
        return _check("Configuration", WARN, ", ".join(slow) + " says localhost: on Windows every request is "
                                                                "about 2 seconds slower. Use 127.0.0.1.")
    return _check("Configuration", OK, f"version {__version__}, OCR {config.OCR_LANGUAGES}, "
                                       f"analyzer {config.TEXT_ANALYZER}")


def check_indices(es=None):
    try:
        indices = (es or Es()).indices()
    except (EsError, SystemExit, OSError) as e:
        return _check("Indices", ERROR, str(e))
    documents = sum(count for _, count in indices)
    return _check("Indices", OK, f"{len(indices)} indices, {documents} documents")


def run_checks():
    """All checks, in display order."""
    checks = []
    for check in (check_config, check_elasticsearch, check_tika, check_containers, check_indices, check_llm,
                  check_embeddings, check_cloud):
        try:
            checks.append(check())
        except Exception as e:  # a check must never take the overview down
            checks.append(_check(getattr(check, "__name__", "check").replace("check_", "").title(), ERROR, f"{type(e).__name__}: {e}"))
    return checks


def overall(checks):
    statuses = {c["status"] for c in checks}
    return ERROR if ERROR in statuses else WARN if WARN in statuses else OK


def format_text(checks):
    marks = {OK: "OK   ", WARN: "WARN ", ERROR: "ERROR"}
    return "\n".join(f"[{marks[c['status']]}] {c['name']}: {c['detail']}" for c in checks)


def main():
    print(format_text(run_checks()))


if __name__ == "__main__":
    main()
