"""Semantic search: text -> vectors with a local embedding model (LM Studio, OpenAI-compatible /v1/embeddings).

The vectors are computed on this computer only (LLM_BASE_URL accepts local addresses only). Every document is cut
into overlapping chunks; each chunk gets a vector, stored in a nested field of the document."""
import json
import re
import urllib.error
import urllib.request

from . import config

BATCH = 16  # texts per request


class EmbeddingError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status  # HTTP status if the server answered, None if it was not reachable


def _prefix(kind):
    """nomic-embed models expect a task prefix; other models none."""
    if "nomic" in config.EMBED_MODEL.lower():
        return "search_query: " if kind == "query" else "search_document: "
    return ""


def _request(texts):
    body = json.dumps({"model": config.EMBED_MODEL, "input": texts}).encode("utf-8")
    request = urllib.request.Request(config.LLM_BASE_URL + "/v1/embeddings", body,
                                     {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            data = json.loads(response.read().decode("utf-8"))["data"]
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise EmbeddingError(f"Embedding server {e.code}: {detail}. Is the model '{config.EMBED_MODEL}' loaded in "
                             "LM Studio (an embedding model, not the chat model)?", e.code) from e
    except (urllib.error.URLError, OSError) as e:
        raise EmbeddingError(f"Embedding server not reachable at {config.LLM_BASE_URL}: "
                             f"{getattr(e, 'reason', e)}") from e
    except (ValueError, KeyError) as e:
        raise EmbeddingError(f"Unexpected answer of the embedding server: {e}") from e
    vectors = [item["embedding"] for item in sorted(data, key=lambda item: item.get("index", 0))]
    if len(vectors) != len(texts):
        raise EmbeddingError(f"The embedding server returned {len(vectors)} vectors for {len(texts)} texts.")
    return vectors


def embed(texts, kind="document"):
    """One vector per text. kind is "document" (a passage to store) or "query" (a question to look up)."""
    prefix = _prefix(kind)
    vectors = []
    for i in range(0, len(texts), BATCH):
        vectors += _request([prefix + t for t in texts[i:i + BATCH]])
    return vectors


def chunk_text(text, size=None, max_chunks=None):
    """[(start offset, chunk)]: pieces of about `size` characters, cut at a space where possible, with a small
    overlap. Only the first `max_chunks` are returned, so one huge document cannot dominate the index."""
    size = size or config.EMBED_CHUNK_CHARS
    max_chunks = max_chunks or config.EMBED_MAX_CHUNKS
    overlap = size // 10
    chunks, start, n = [], 0, len(text)
    while start < n and len(chunks) < max_chunks:
        end = min(n, start + size)
        if end < n:
            space = text.rfind(" ", start + size // 2, end)
            end = space if space > start else end
        piece = text[start:end].strip()
        if piece:
            chunks.append((start, piece))
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return chunks


def vector_properties(dims):
    """Mapping of the fields that hold the vectors (added to an existing index when embeddings are created)."""
    return {
        "embedded_chunks": {"type": "integer"},  # chunks stored; 0 = no text, -1 = could not be embedded
        "chunks": {"type": "nested", "properties": {
            "start": {"type": "integer"},        # character offset of the chunk in `content`
            "vector": {"type": "dense_vector", "dims": dims, "index": True, "similarity": "cosine"},
        }},
    }
