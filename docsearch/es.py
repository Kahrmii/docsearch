"""Thin Elasticsearch client (standard library only)."""
import base64
import json
import re
import urllib.error
import urllib.request

from . import config, embeddings

INDEX_NAME = re.compile(r"[a-z0-9][a-z0-9_\-]*")


def valid_index_name(name):
    """Elasticsearch index names: lowercase letters, digits, _ and -, not starting with _ or -.

    This also rules out wildcards, lists (a,b), _all and system indices (.name)."""
    return bool(INDEX_NAME.fullmatch(name))


# Fields of mails (mbox import). Added to every new index and, when a mailbox is indexed, to existing indices.
MAIL_PROPERTIES = {
    "mail_from": {"type": "keyword"},
    "mail_to": {"type": "keyword"},
    "mail_cc": {"type": "keyword"},
    "mail_subject": {"type": "keyword", "ignore_above": 1024},
    "mail_date": {"type": "date"},
    "mail_message_id": {"type": "keyword"},
    "mail_attachments": {"type": "integer"},
    "mail_attachment_names": {"type": "keyword"},
    "mail_folder": {"type": "keyword"},
}


# fields added after the first release: existing indices get them via ensure_extra_fields
EXTRA_PROPERTIES = {
    "ocr_pages": {"type": "integer"},        # pages Tika had to OCR (scans)
    "content_hash": {"type": "keyword"},     # SHA-256 of the file (mails: of the Message-ID), finds duplicates
}


def mapping():
    return {
        "settings": {"number_of_shards": 1, "number_of_replicas": 0, "refresh_interval": "30s"},
        "mappings": {
            "properties": {
                "filename": {"type": "keyword"},
                "path": {"type": "keyword"},
                "extension": {"type": "keyword"},
                "size_bytes": {"type": "long"},
                "mtime": {"type": "date"},
                "content": {"type": "text", "analyzer": config.TEXT_ANALYZER},
                "content_length": {"type": "long"},
                "content_truncated": {"type": "boolean"},
                "content_type": {"type": "keyword"},
                "author": {"type": "keyword"},
                "created": {"type": "keyword"},
                "page_count": {"type": "integer"},
                "language": {"type": "keyword"},
                "indexed_at": {"type": "date"},
                # all Tika metadata, kept in _source only (not indexed)
                "metadata": {"type": "object", "enabled": False},
                **EXTRA_PROPERTIES,
                **MAIL_PROPERTIES,
            }
        },
    }


class EsError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class Es:
    def __init__(self, password=None):
        token = base64.b64encode(f"elastic:{password or config.elastic_password()}".encode()).decode()
        self._auth = {"Authorization": f"Basic {token}"}

    def call(self, method, path, body=None, *, ndjson=False, timeout=120):
        data, headers = None, dict(self._auth)
        if body is not None:
            data = body.encode("utf-8") if ndjson else json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/x-ndjson" if ndjson else "application/json"
        request = urllib.request.Request(config.ES_URL + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise EsError("Elasticsearch rejected the login (401). Check ELASTIC_PASSWORD in .env: Elasticsearch "
                              "only accepts the password the Docker volume was first created with, and the "
                              "container on this port may belong to another setup.", 401) from e
            detail = e.read().decode("utf-8", "replace")[:500]
            raise EsError(f"Elasticsearch {e.code} on {method} {path}: {detail}", e.code) from e
        except urllib.error.URLError as e:
            raise EsError(f"Elasticsearch is not reachable: {e.reason}") from e

    def ensure_index(self, index):
        """Create the index if it does not exist. Returns True if it was created."""
        try:
            self.call("GET", f"/{index}")
        except EsError as e:
            if e.status != 404:
                raise
        else:
            self.call("PUT", f"/{index}/_mapping", {"properties": EXTRA_PROPERTIES})  # older indices lack them
            return False
        self.call("PUT", f"/{index}", mapping())
        return True

    def ensure_mail_fields(self, index):
        """Add the mail_* fields to an existing index (additive and harmless if they are already there)."""
        self.call("PUT", f"/{index}/_mapping", {"properties": MAIL_PROPERTIES})

    def indices(self):
        """[(index name, document count)] without system indices."""
        rows = self.call("GET", "/_cat/indices?format=json&h=index,docs.count")
        return sorted((r["index"], int(r["docs.count"] or 0)) for r in rows if not r["index"].startswith("."))

    def index_info(self, index):
        """{"documents": n, "size_mb": n} of an index, or None if it does not exist."""
        try:
            rows = self.call("GET", f"/_cat/indices/{index}?format=json&h=docs.count,store.size&bytes=mb")
        except EsError as e:
            if e.status == 404:
                return None
            raise
        return {"documents": int(rows[0]["docs.count"] or 0), "size_mb": int(float(rows[0]["store.size"] or 0))}

    def delete_index(self, index):
        """Delete one index by its exact name. Wildcards and lists are refused."""
        if not valid_index_name(index):
            raise ValueError(f"Refusing to delete '{index}': not a plain index name")
        self.call("DELETE", f"/{index}")

    def existing_ids(self, index, ids):
        found = set()
        for i in range(0, len(ids), 500):
            result = self.call("POST", f"/{index}/_mget?_source=false", {"ids": ids[i:i + 500]})
            found.update(d["_id"] for d in result["docs"] if d.get("found"))
        return found

    def bulk_update(self, index, updates):
        """updates: [(id, partial document)]. Returns {id: error text} for the rejected ones."""
        lines = []
        for doc_id, doc in updates:
            lines.append(json.dumps({"update": {"_index": index, "_id": doc_id}}))
            lines.append(json.dumps({"doc": doc}, ensure_ascii=False))
        result = self.call("POST", "/_bulk", "\n".join(lines) + "\n", ndjson=True, timeout=300)
        return {item["update"]["_id"]: json.dumps(item["update"]["error"])[:500]
                for item in result["items"] if item["update"].get("error")}

    def vector_dims(self, index):
        """Dimensions of the stored vectors, or None if the index has no vector field yet."""
        properties = self.call("GET", f"/{index}/_mapping")[index]["mappings"].get("properties", {})
        return properties.get("chunks", {}).get("properties", {}).get("vector", {}).get("dims")

    def ensure_vector_fields(self, index, dims):
        """Add the vector fields (additive). Refuses vectors of another size than the ones already stored."""
        existing = self.vector_dims(index)
        if existing is not None and existing != dims:
            raise EsError(f"The index '{index}' stores vectors with {existing} dimensions, but the embedding model "
                          f"returns {dims}. Use the same model as before, or delete the index and start again.")
        if existing is None:
            self.call("PUT", f"/{index}/_mapping", {"properties": embeddings.vector_properties(dims)})

    def embedding_stats(self, index):
        """{"embedded": documents with vectors, "missing": documents without, "dims"}."""
        total = self.count(index, {"match_all": {}})
        embedded = self.count(index, {"range": {"embedded_chunks": {"gt": 0}}}) if self.vector_dims(index) else 0
        done = self.count(index, {"exists": {"field": "embedded_chunks"}}) if self.vector_dims(index) else 0
        return {"embedded": embedded, "missing": total - done, "dims": self.vector_dims(index)}

    def existing_hashes(self, index, hashes):
        """Which of the content hashes are already in the index."""
        found = set()
        hashes = list(hashes)
        for i in range(0, len(hashes), 500):
            part = hashes[i:i + 500]
            result = self.search(index, {"size": len(part) * 2, "_source": ["content_hash"],
                                         "query": {"terms": {"content_hash": part}}})
            found.update(h["_source"]["content_hash"] for h in result["hits"]["hits"])
        return found

    def bulk(self, index, docs):
        """docs: [(id, document)]. Returns {id: error text} for the rejected documents."""
        lines = []
        for doc_id, doc in docs:
            lines.append(json.dumps({"index": {"_index": index, "_id": doc_id}}))
            lines.append(json.dumps(doc, ensure_ascii=False))
        result = self.call("POST", "/_bulk", "\n".join(lines) + "\n", ndjson=True, timeout=300)
        return {item["index"]["_id"]: json.dumps(item["index"]["error"])[:500]
                for item in result["items"] if item["index"].get("error")}

    def count(self, index, query):
        return self.call("POST", f"/{index}/_count", {"query": query})["count"]

    def search(self, index, body):
        return self.call("POST", f"/{index}/_search", body)

    def scan(self, index, query, fields, limit, page=100, should_stop=None):
        """All hits page by page (search_after), at most `limit` documents."""
        got, after = 0, None
        while got < limit:
            if should_stop and should_stop():
                return
            body = {"size": min(page, limit - got), "query": query, "_source": fields, "sort": [{"_doc": "asc"}]}
            if after:
                body["search_after"] = after
            hits = self.search(index, body)["hits"]["hits"]
            if not hits:
                return
            yield from hits
            got += len(hits)
            after = hits[-1]["sort"]
