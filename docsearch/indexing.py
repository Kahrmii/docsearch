"""Pipeline: files -> Tika (text + OCR + metadata) -> Elasticsearch."""
import concurrent.futures as cf
import datetime as dt
import json
import logging
import os
import random
import re
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from typing import Optional

from . import config, mbox, services
from .es import Es

log = logging.getLogger("docsearch.indexing")
SKIP_NAMES = {"thumbs.db", "desktop.ini", ".ds_store"}
ERRORS_FILE = config.LOG_DIR / "errors.jsonl"


@dataclass
class Options:
    data_dir: str
    index: str = "documents"
    recursive: bool = False
    limit: Optional[int] = None      # only N files that are not indexed yet
    random: bool = False             # random sample instead of the first N
    seed: int = 42
    workers: int = 4                 # parallel Tika requests (mind Tika's memory)
    batch_size: int = 50
    timeout: int = 300               # seconds per file
    max_chars: int = 10_000_000      # truncate text per document
    max_file_mb: int = 100           # skip larger files (0 = no limit)
    skip_failed: bool = False
    dry_run: bool = False


def scan_files(data_dir, recursive):
    """[(relative path with '/', size, mtime)], sorted."""
    entries = []
    for root, dirs, files in os.walk(data_dir):
        if not recursive:
            dirs[:] = []
        for name in files:
            if name.startswith("~$") or name.lower() in SKIP_NAMES:
                continue
            full = os.path.join(root, name)
            stat = os.stat(full)
            entries.append((os.path.relpath(full, data_dir).replace("\\", "/"), stat.st_size, stat.st_mtime))
    return sorted(entries)


def tika_extract(path, timeout, retries=3):
    """Text and metadata of a file. Waits and retries on 503 (Tika restarting) and connection problems."""
    last = None
    for attempt in range(retries):
        try:
            with open(path, "rb") as f:
                request = urllib.request.Request(
                    config.TIKA_URL + "/rmeta/text", data=f, method="PUT",
                    headers={"Accept": "application/json", "X-Tika-OCRLanguage": config.OCR_LANGUAGES,
                             "Content-Length": str(os.path.getsize(path)),
                             "Content-Type": "application/octet-stream"})
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 503 and attempt < retries - 1:
                time.sleep(15 * (attempt + 1))
                continue
            raise RuntimeError(f"Tika HTTP {e.code}: {e.reason}") from e
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
            last = e
            if attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Tika unreachable or timed out: {last}")


def _first(value):
    return (value[0] if value else None) if isinstance(value, list) else value


def build_doc(rel, size, mtime, data_dir, timeout, max_chars):
    full = os.path.join(data_dir, rel)
    result = tika_extract(full, timeout)
    meta = result[0] if isinstance(result, list) and result else {}
    # Tika 4.x: "tk:content", older versions: "X-TIKA:content"
    content = meta.get("tk:content") or meta.get("X-TIKA:content") or ""
    content = re.sub(r"\n{3,}", "\n\n", content.replace("\r", "")).strip()
    truncated = len(content) > max_chars
    content = content[:max_chars]
    metadata = {k: v for k, v in meta.items() if k not in ("tk:content", "X-TIKA:content")}
    try:
        pages = int(_first(metadata.get("xmpTPg:NPages") or metadata.get("meta:page-count")))
    except (TypeError, ValueError):
        pages = None
    return {
        "filename": os.path.basename(rel),
        "path": full.replace("\\", "/"),
        "extension": os.path.splitext(rel)[1].lower().lstrip("."),
        "size_bytes": size,
        "mtime": dt.datetime.fromtimestamp(mtime, dt.timezone.utc).isoformat(),
        "content": content,
        "content_length": len(content),
        "content_truncated": truncated,
        "content_type": _first(metadata.get("Content-Type")),
        "author": _first(metadata.get("dc:creator")),
        "created": _first(metadata.get("dcterms:created")),
        "page_count": pages,
        "language": _first(metadata.get("language")),
        "indexed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "metadata": metadata,
    }


def _log_error(doc_id, message):
    log.error("ERROR %s: %s", doc_id, message)
    config.LOG_DIR.mkdir(exist_ok=True)
    with open(ERRORS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps({"id": doc_id, "error": message, "time": dt.datetime.now().isoformat()},
                           ensure_ascii=False) + "\n")


def _failed_ids():
    if not ERRORS_FILE.exists():
        return set()
    ids = set()
    for line in ERRORS_FILE.read_text(encoding="utf-8").splitlines():
        try:
            ids.add(json.loads(line)["id"])
        except (ValueError, KeyError):
            pass
    return ids


def _format_eta(seconds):
    seconds = int(seconds)
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def _progress(stats, on_progress, done, n_total, processed, start):
    """Tell the callback how far we are. Returns (rate, eta in seconds or None)."""
    rate = processed / max(time.time() - start, 1e-9)
    eta = (n_total - done) / rate if rate else None
    on_progress({"done": done, "total": n_total, "ok": stats["ok"], "errors": stats["errors"],
                 "skipped": stats["skipped"], "rate": rate, "eta_s": eta})
    return rate, eta


def _log_progress(stats, done, n_total, rate, eta):
    log.info("File %d/%d (%.1f%%) | ok %d | errors %d | skipped %d | %.2f files/s | ETA %s",
             done, n_total, 100 * done / max(n_total, 1), stats["ok"], stats["errors"], stats["skipped"],
             rate, _format_eta(eta) if eta is not None else "?")


def _bulk(es, index, docs, stats):
    """Write documents; rejected ones are counted and logged as errors."""
    try:
        rejected = es.bulk(index, docs)
    except Exception as ex:
        rejected = {d[0]: f"Bulk request failed: {ex}" for d in docs}
    for rid, message in rejected.items():
        stats["errors"] += 1
        _log_error(rid, message)
    stats["ok"] += len(docs) - len(rejected)


def _mail_batch(es, index, opts, path, prefix, rel, mtime, batch, failed, stats, ctx):
    """Index one batch of (number, raw message) of a mailbox. Returns False when the run has to end."""
    if ctx["should_stop"]():
        stats["stopped"] = True
        log.warning("Abort requested.")
        return False
    ids = {number: f"{prefix}/{rel}#{number}" for number, _ in batch}
    present = es.existing_ids(index, list(ids.values()))  # resume: skip messages that are already indexed
    stats["skipped"] += len(present)
    ctx["seen"] += len(batch)
    todo = [(number, raw) for number, raw in batch if ids[number] not in present and ids[number] not in failed]
    if opts.limit is not None:
        todo = todo[:opts.limit - ctx["processed"]]
    docs = []
    for number, raw in todo:
        try:
            docs.append((ids[number], mbox.build_mail_doc(raw, number, path, mtime, opts.max_chars)))
        except Exception as ex:  # one broken message must not end the run
            stats["errors"] += 1
            _log_error(ids[number], f"Cannot read message: {ex}")
    if docs:
        _bulk(es, index, docs, stats)
    ctx["processed"] += len(todo)
    done = ctx["processed"] if opts.limit is not None else ctx["base"] + ctx["seen"]
    rate, eta = _progress(stats, ctx["on_progress"], done, ctx["n_total"], ctx["processed"], ctx["start"])
    if todo or stats["skipped"] % 5000 < len(batch):
        _log_progress(stats, done, ctx["n_total"], rate, eta)
    return opts.limit is None or ctx["processed"] < opts.limit


def _index_mailboxes(es, index, opts, data_dir, prefix, mailboxes, failed, stats, ctx):
    """Index the messages of all mailboxes, batch by batch (message ID = <folder>/<mailbox>#<number>)."""
    es.ensure_mail_fields(index)
    for rel, _, mtime in mailboxes:
        path = os.path.join(data_dir, rel)
        batch = []
        for number, raw in mbox.iter_messages(path):
            batch.append((number, raw))
            if len(batch) >= opts.batch_size:
                if not _mail_batch(es, index, opts, path, prefix, rel, mtime, batch, failed, stats, ctx):
                    return
                batch = []
        if batch and not _mail_batch(es, index, opts, path, prefix, rel, mtime, batch, failed, stats, ctx):
            return


def run_job(opts, on_progress=None, should_stop=None, on_status=None):
    """`run()` for the user interfaces: starts the Docker services first if they are not running.

    Returns (message for the user, index name or None for a dry run). Errors are not caught here."""
    if not opts.dry_run and not services.services_up():
        if on_status:
            on_status("Starting Docker containers ...")
        services.start_containers()
    stats = run(opts, on_progress, should_stop)
    if opts.dry_run:
        return f"{stats['total']} files found.", None
    message = ("Stopped. " if stats["stopped"] else "Done. ") + \
              f"ok {stats['ok']}, errors {stats['errors']}, skipped {stats['skipped']}."
    return message, opts.index.strip().lower()


def run(opts, on_progress=None, should_stop=None):
    """Index a folder. `on_progress(dict)` per batch, `should_stop()` for a clean abort.

    Returns {"total", "ok", "errors", "skipped", "seconds", "stopped"}.
    """
    on_progress = on_progress or (lambda p: None)
    should_stop = should_stop or (lambda: False)
    data_dir = opts.data_dir.rstrip("/\\")
    if not os.path.isdir(data_dir):
        raise FileNotFoundError(f"Folder not found: {data_dir}")
    index = opts.index.strip().lower()
    prefix = os.path.basename(data_dir)

    entries = scan_files(data_dir, opts.recursive)
    log.info("%d files in %s", len(entries), data_dir)
    stats = {"total": len(entries), "ok": 0, "errors": 0, "skipped": 0, "seconds": 0, "stopped": False}
    if opts.dry_run:
        for ext, n in Counter(os.path.splitext(r)[1].lower() or "(none)" for r, _, _ in entries).most_common(12):
            log.info("  %-10s %d", ext, n)
        for rel, _, _ in entries:
            if mbox.looks_like_mbox(os.path.join(data_dir, rel)):
                log.info("  mailbox %s: %d messages", rel, mbox.count_messages(os.path.join(data_dir, rel)))
        return stats

    version = services.tika_version()
    if version is None:
        raise RuntimeError("Tika is not reachable. Are the Docker containers running?")
    es = Es()
    log.info("Tika: %s | Elasticsearch: %s", version, es.call("GET", "/_cluster/health")["status"])
    if es.ensure_index(index):
        log.info("Index '%s' created", index)

    def doc_id(rel):
        return f"{prefix}/{rel}"

    failed = _failed_ids() if opts.skip_failed else set()
    candidates = [e for e in entries if doc_id(e[0]) not in failed]
    # Mailboxes are read message by message (no size limit, no Tika); everything else goes through Tika
    mailboxes = [e for e in candidates if mbox.looks_like_mbox(os.path.join(data_dir, e[0]))]
    candidates = [e for e in candidates if e not in mailboxes]
    mail_total = 0
    for rel, _, _ in mailboxes:
        count = mbox.count_messages(os.path.join(data_dir, rel))
        log.info("Mailbox %s: %d messages", rel, count)
        mail_total += count
    if opts.max_file_mb:
        limit_bytes = opts.max_file_mb * 1024 * 1024
        too_big = [e for e in candidates if e[1] > limit_bytes]
        if too_big:
            log.warning("%d files larger than %d MB are skipped:", len(too_big), opts.max_file_mb)
            for rel, size, _ in too_big[:20]:
                log.warning("  too large: %s (%.0f MB)", rel, size / 1024 / 1024)
            if len(too_big) > 20:
                log.warning("  ... and %d more", len(too_big) - 20)
            candidates = [e for e in candidates if e[1] <= limit_bytes]
    if opts.random:
        random.Random(opts.seed).shuffle(candidates)
    n_total = opts.limit if opts.limit is not None else len(candidates) + mail_total

    start = time.time()
    processed = pos = 0
    while pos < len(candidates) and (opts.limit is None or processed < opts.limit):
        if should_stop():
            stats["stopped"] = True
            log.warning("Abort requested.")
            break
        chunk = candidates[pos:pos + opts.batch_size]
        pos += len(chunk)
        # Resume: check only this batch against the index
        present = es.existing_ids(index, [doc_id(e[0]) for e in chunk])
        stats["skipped"] += len(present)
        batch = [e for e in chunk if doc_id(e[0]) not in present]
        if opts.limit is not None:
            batch = batch[:opts.limit - processed]

        docs = []
        if batch:
            with cf.ThreadPoolExecutor(max_workers=opts.workers) as pool:
                futures = {pool.submit(build_doc, rel, size, mtime, data_dir, opts.timeout, opts.max_chars): rel
                           for rel, size, mtime in batch}
                for future in cf.as_completed(futures):
                    rel = futures[future]
                    try:
                        docs.append((doc_id(rel), future.result()))
                    except Exception as ex:  # one broken file must not end the run
                        stats["errors"] += 1
                        _log_error(doc_id(rel), str(ex))
            if docs:
                _bulk(es, index, docs, stats)
            processed += len(batch)

        done = processed if opts.limit is not None else pos
        rate, eta = _progress(stats, on_progress, done, n_total, processed, start)
        if batch or stats["skipped"] % 5000 < len(chunk):
            _log_progress(stats, done, n_total, rate, eta)

    if mailboxes and not stats["stopped"] and (opts.limit is None or processed < opts.limit):
        ctx = {"seen": 0, "processed": processed, "base": pos, "n_total": n_total, "start": start,
               "on_progress": on_progress, "should_stop": should_stop}
        _index_mailboxes(es, index, opts, data_dir, prefix, mailboxes, failed, stats, ctx)

    es.call("POST", f"/{index}/_refresh")
    stats["seconds"] = time.time() - start
    log.info("Done: ok %d, errors %d, skipped %d, duration %s",
             stats["ok"], stats["errors"], stats["skipped"], _format_eta(stats["seconds"]))
    return stats
