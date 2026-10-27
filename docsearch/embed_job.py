"""Create the vectors of an index for the semantic search: every document is cut into chunks and each chunk is
embedded with the local embedding model. Resumable: only documents without vectors are processed."""
import logging
import time

from . import embeddings, services
from .es import Es, EsError, valid_index_name

log = logging.getLogger("docsearch.embed")
MISSING = {"bool": {"must_not": [{"exists": {"field": "embedded_chunks"}}]}}
FAILED = {"term": {"embedded_chunks": -1}}
MAX_IN_A_ROW = 5  # consecutive failures that mean the model is not working (instead of marking every document)


def _flush(es, index, hits, stats, state):
    updates = []
    for hit in hits:
        chunks = embeddings.chunk_text(hit["_source"].get("content") or "")
        if not chunks:
            updates.append((hit["_id"], {"embedded_chunks": 0}))
            stats["empty"] += 1
            continue
        try:
            vectors = embeddings.embed([text for _, text in chunks])
        except embeddings.EmbeddingError as e:
            state["in_a_row"] += 1
            if e.status is None or state["in_a_row"] >= MAX_IN_A_ROW:
                raise  # the server is gone or the model does not work: stop instead of marking documents as failed
            log.error("ERROR %s: %s", hit["_id"], e)
            updates.append((hit["_id"], {"embedded_chunks": -1}))
            stats["errors"] += 1
            continue
        state["in_a_row"] = 0
        updates.append((hit["_id"], {"embedded_chunks": len(chunks),
                                     "chunks": [{"start": s, "vector": v} for (s, _), v in zip(chunks, vectors)]}))
        stats["ok"] += 1
    for doc_id, message in es.bulk_update(index, updates).items():
        stats["errors"] += 1
        stats["ok"] -= 1
        log.error("ERROR %s: %s", doc_id, message)


def embed_index(es, index, on_progress=None, should_stop=None, batch_size=8, limit=None, retry_failed=False):
    """Store the vectors of all documents that have none yet. Returns {"total", "ok", "empty", "errors",
    "seconds", "stopped"}. Raises EmbeddingError if the embedding server is not usable."""
    on_progress = on_progress or (lambda p: None)
    should_stop = should_stop or (lambda: False)
    dims = len(embeddings.embed(["dimension probe"])[0])  # also proves that the model works
    es.ensure_vector_fields(index, dims)
    query = {"bool": {"should": [MISSING, FAILED], "minimum_should_match": 1}} if retry_failed else MISSING
    total = es.count(index, query)
    if limit is not None:
        total = min(total, limit)
    stats = {"total": total, "ok": 0, "empty": 0, "errors": 0, "seconds": 0, "stopped": False}
    log.info("%d documents without vectors in '%s' (%d dimensions)", total, index, dims)
    start, state, pending = time.time(), {"in_a_row": 0}, []

    def report():
        done = stats["ok"] + stats["empty"] + stats["errors"]
        rate = done / max(time.time() - start, 1e-9)
        on_progress({"done": done, "total": total, "ok": stats["ok"], "errors": stats["errors"],
                     "skipped": stats["empty"], "rate": rate, "eta_s": (total - done) / rate if rate else None})
        log.info("Documents %d/%d | ok %d | no text %d | errors %d | %.1f documents/s",
                 done, total, stats["ok"], stats["empty"], stats["errors"], rate)

    for hit in es.scan(index, query, ["content"], total, page=batch_size, should_stop=should_stop):
        pending.append(hit)
        if len(pending) >= batch_size:
            _flush(es, index, pending, stats, state)
            pending = []
            report()
    if pending and not should_stop():
        _flush(es, index, pending, stats, state)
        report()
    stats["stopped"] = should_stop()
    es.call("POST", f"/{index}/_refresh")
    stats["seconds"] = time.time() - start
    return stats


def run_job(index, on_progress=None, should_stop=None, on_status=None, limit=None, retry_failed=False):
    """`embed_index()` for the user interfaces: starts the Docker services first if needed.
    Returns the message for the user. Errors are not caught here."""
    index = index.strip().lower()
    if not valid_index_name(index):
        raise ValueError("Index name: only lowercase letters, digits, _ and -.")
    if not services.services_up():
        if on_status:
            on_status("Starting Docker containers ...")
        services.start_containers()
    es = Es()
    if es.index_info(index) is None:
        raise EsError(f"The index '{index}' does not exist.", 404)
    stats = embed_index(es, index, on_progress, should_stop, limit=limit, retry_failed=retry_failed)
    return ("Stopped. " if stats["stopped"] else "Done. ") + \
        f"vectors for {stats['ok']} documents, no text {stats['empty']}, errors {stats['errors']}."
