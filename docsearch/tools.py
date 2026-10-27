"""Tools of the agent. Counting, searching and analysing is done by Elasticsearch or plain code;
the model only receives compact JSON results."""
import json
import re
from collections import Counter

PATTERNS = {
    "email": re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}"),
    "url": re.compile(r"(?:https?://|www\.)[^\s<>\"'()\[\]]+", re.I),
    # day.month.year as written in German/European documents (e.g. 31.12.2024)
    "date": re.compile(r"\b\d{1,2}\.\d{1,2}\.(?:19|20)\d{2}\b"),
}
PATTERN_KINDS = ["email", "url", "domain", "date"]  # "domain" = the part after the @ of email addresses
GROUP_FIELDS = ["extension", "author", "content_type", "language", "mtime_year"]
SOURCE_FIELDS = ["filename", "path", "content", "author", "created", "extension", "page_count"]


class Cancelled(Exception):
    """The user cancelled the analysis."""


class Context:
    """Runtime environment of the tools: index, space budget, cancellation and the source list (filename -> path)."""

    def __init__(self, es, index, ctx_chars, should_stop=None, progress=None):
        self.es = es
        self.index = index
        self.progress = progress or (lambda message: None)  # status messages of long analyses
        self.ctx_chars = ctx_chars
        self.max_result_chars = max(2000, min(24000, ctx_chars // 4))
        self.should_stop = should_stop or (lambda: False)
        self.sources = {}
        self.scan_cache = {}


def _clamp(value, low, high, default):
    try:
        return max(low, min(int(value), high))
    except (TypeError, ValueError):
        return default


def words(text):
    return re.findall(r"\w{3,}", text or "")


def build_query(query=None, phrases=None, extension=None):
    must, flt = [], []
    if query:
        must.append({"match": {"content": {"query": query, "operator": "and"}}})
    must += [{"match_phrase": {"content": p}} for p in phrases or [] if p]
    if extension:
        flt.append({"term": {"extension": extension.lower().lstrip(".")}})
    if not must and not flt:
        return {"match_all": {}}
    return {"bool": {"must": must, "filter": flt}}


def windows(text, terms, width=700, limit=3):
    """Text windows around matches; places where several terms are close together come first."""
    positions = []
    for term in {t.lower() for t in terms if t}:
        for i, m in enumerate(re.finditer(re.escape(term), text, re.I)):
            if i >= 300:
                break
            positions.append((m.start(), term))
    positions.sort()
    scored = sorted((-len({u for q, u in positions if abs(q - p) <= width and u != t}), p) for p, t in positions)
    out, used = [], []
    for _, p in scored:
        if any(abs(p - u) < width for u in used):
            continue
        used.append(p)
        out.append(re.sub(r"\s+", " ", text[max(0, p - width // 2): p + width // 2]).strip())
        if len(out) >= limit:
            break
    return out or [re.sub(r"\s+", " ", text[:width]).strip()]


def to_json(obj, limit):
    """JSON text; lists that are too long are shortened so the result fits the space budget."""
    text = json.dumps(obj, ensure_ascii=False)
    for key in ("hits", "top", "groups"):
        while len(text) > limit and isinstance(obj.get(key), list) and len(obj[key]) > 1:
            obj[key] = obj[key][:max(1, len(obj[key]) * 2 // 3)]
            obj["truncated"] = True
            text = json.dumps(obj, ensure_ascii=False)
    return text[:limit]


def _check_stop(ctx):
    if ctx.should_stop():
        raise Cancelled()


# ---- Tools -------------------------------------------------------------------------------------

def search_documents(ctx, query=None, phrases=None, extension=None, size=8):
    size = _clamp(size, 1, 20, 8)
    result = ctx.es.search(ctx.index, {
        "size": size, "track_total_hits": True, "_source": SOURCE_FIELDS,
        "query": build_query(query, phrases, extension)})
    per_doc = 3 if size <= 8 else 2
    width = _clamp(ctx.max_result_chars // (size * per_doc) - 60, 200, 700, 400)
    terms = list(phrases or []) + words(query)
    hits = []
    for h in result["hits"]["hits"]:
        src = h["_source"]
        ctx.sources[src["filename"]] = src.get("path")
        hits.append({"doc": src["filename"], "author": src.get("author"), "created": src.get("created"),
                     "excerpts": windows(src.get("content") or "", terms, width, per_doc)})
    return {"hits_total": result["hits"]["total"]["value"], "hits": hits}


def count_documents(ctx, query=None, phrases=None, extension=None):
    return {"documents": ctx.es.count(ctx.index, build_query(query, phrases, extension))}


def count_term(ctx, phrase, extension=None, scan_limit=20000):
    """How often does a phrase occur: documents according to the search and literal occurrences (by counting)."""
    query = build_query(phrases=[phrase], extension=extension)
    docs_search = ctx.es.count(ctx.index, query)
    pattern = re.compile(re.escape(phrase), re.I)
    per_doc, total = [], 0
    for i, h in enumerate(ctx.es.scan(ctx.index, query, ["content", "filename", "path"], scan_limit,
                                      should_stop=ctx.should_stop), 1):
        if i % 500 == 0:
            ctx.progress(f"count_term: counted {i}/{min(docs_search, scan_limit)} documents ...")
        n = len(pattern.findall(h["_source"].get("content") or ""))
        if n:
            total += n
            per_doc.append((n, h["_source"]["filename"], h["_source"].get("path")))
    _check_stop(ctx)
    per_doc.sort(reverse=True)
    for _, name, path in per_doc[:10]:
        ctx.sources[name] = path
    return {"phrase": phrase, "documents_by_search": docs_search, "documents_with_exact_spelling": len(per_doc),
            "occurrences_exact": total, "top": [{"doc": name, "count": n} for n, name, _ in per_doc[:10]],
            "note": f"only the first {scan_limit} documents were counted" if docs_search > scan_limit else None}


def find_patterns(ctx, kind, phrases=None, extension=None, top=20, scan_limit=100000):
    """Count email addresses, URLs, domains or dates by text pattern in all (matching) documents."""
    if kind not in PATTERN_KINDS:
        return {"error": f"kind must be one of {PATTERN_KINDS}"}
    top = _clamp(top, 1, 100, 20)
    key = (kind, tuple(phrases or ()), extension)
    if key not in ctx.scan_cache:
        pattern = PATTERNS["email" if kind == "domain" else kind]
        query = build_query(phrases=phrases, extension=extension)
        occurrences, doc_counts, with_hits, seen = Counter(), Counter(), 0, 0
        total = min(ctx.es.count(ctx.index, query), scan_limit)
        for h in ctx.es.scan(ctx.index, query, ["content"], scan_limit, should_stop=ctx.should_stop):
            seen += 1
            if seen % 500 == 0:
                ctx.progress(f"find_patterns: read {seen}/{total} documents ({100 * seen // max(total, 1)} %) ...")
            found = [f.lower().rstrip(".,;:") for f in pattern.findall(h["_source"].get("content") or "")]
            if kind == "domain":
                found = [f.split("@", 1)[1] for f in found]
            with_hits += bool(found)
            occurrences.update(found)
            doc_counts.update(set(found))
        _check_stop(ctx)
        ctx.scan_cache[key] = (occurrences, doc_counts, with_hits, seen)
    occurrences, doc_counts, with_hits, seen = ctx.scan_cache[key]
    return {"kind": kind, "distinct": len(occurrences), "occurrences": sum(occurrences.values()),
            "documents_with_match": with_hits, "documents_checked": seen,
            "top": [{"value": v, "occurrences": c, "documents": doc_counts[v]} for v, c in occurrences.most_common(top)],
            "note": "plain text pattern matching; OCR errors can distort spellings"}


def group_by(ctx, field, query=None, phrases=None, extension=None, size=10):
    if field not in GROUP_FIELDS:
        return {"error": f"field must be one of {GROUP_FIELDS}"}
    size = _clamp(size, 1, 50, 10)
    agg = ({"date_histogram": {"field": "mtime", "calendar_interval": "year", "format": "yyyy"}}
           if field == "mtime_year" else {"terms": {"field": field, "size": size}})
    result = ctx.es.search(ctx.index, {"size": 0, "track_total_hits": True,
                                       "query": build_query(query, phrases, extension), "aggs": {"g": agg}})
    buckets = result["aggregations"]["g"]["buckets"]
    if field == "mtime_year":
        buckets = [b for b in buckets if b["doc_count"]]
    return {"field": field, "documents_total": result["hits"]["total"]["value"],
            "groups": [{"value": b.get("key_as_string", b["key"]), "documents": b["doc_count"]} for b in buckets]}


def filename_query(name, extension=None):
    """Part of a filename, case-insensitive."""
    query = {"wildcard": {"filename": {"value": f"*{name}*", "case_insensitive": True}}}
    if extension:
        return {"bool": {"must": [query], "filter": [{"term": {"extension": extension.lower().lstrip(".")}}]}}
    return query


def find_files(ctx, name, extension=None, size=10):
    """Find documents by (part of) the filename. Also shows whether text for a file is in the index."""
    size = _clamp(size, 1, 30, 10)
    query = filename_query(name, extension)
    result = ctx.es.search(ctx.index, {"size": size, "track_total_hits": True, "query": query,
                                       "_source": ["filename", "path", "size_bytes", "content_length", "page_count",
                                                   "content_truncated"]})
    files = []
    for h in result["hits"]["hits"]:
        src = h["_source"]
        ctx.sources[src["filename"]] = src.get("path")
        files.append({"doc": src["filename"], "size_mb": round((src.get("size_bytes") or 0) / 1048576, 2),
                      "text_length": src.get("content_length"), "pages": src.get("page_count"),
                      "text_truncated": src.get("content_truncated") or None})
    total = result["hits"]["total"]["value"]
    return {"found": total, "hits": files,
            "note": None if total else "No file with this name is in the index. It may not have been indexed "
                                       "(too large, error, different index); this is not a search failure."}


def get_document(ctx, filename, around=None, offset=0, length=3000):
    result = ctx.es.search(ctx.index, {"size": 1, "query": {"term": {"filename": filename}},
                                       "_source": SOURCE_FIELDS + ["content_length"]})
    hits = result["hits"]["hits"]
    if not hits:
        return {"error": f"Document '{filename}' not found (use the exact filename from a search result)"}
    src = hits[0]["_source"]
    ctx.sources[src["filename"]] = src.get("path")
    length = _clamp(length, 200, 6000, 3000)
    text = src.get("content") or ""
    if around:
        excerpts = windows(text, [around], min(length, 1500), 3)
    else:
        offset = _clamp(offset, 0, max(len(text) - 1, 0), 0)
        excerpts = [text[offset:offset + length]]
    return {"doc": src["filename"], "author": src.get("author"), "created": src.get("created"),
            "pages": src.get("page_count"), "text_length": len(text), "text": excerpts}


# ---- Description for the model -----------------------------------------------------------------

def _fn(name, description, properties, required=()):
    return {"type": "function", "function": {"name": name, "description": description,
                                             "parameters": {"type": "object", "properties": properties,
                                                            "required": list(required)}}}


_STR = {"type": "string"}
_PHRASES = {"type": "array", "items": _STR,
            "description": "Exact phrases (names, terms) that must ALL occur in the document"}
_EXT = {"type": "string", "description": "Only this file type, e.g. pdf, docx, xlsx"}
_QUERY = {"type": "string", "description": "Free search words (all must occur)"}

TOOLS = [
    _fn("search_documents", "Full-text search. Returns the hit count and text excerpts (with several phrases, the "
        "places where they stand close together). Use it to answer 'What does it say about X' or "
        "'What did A write to B'.",
        {"query": _QUERY, "phrases": _PHRASES, "extension": _EXT,
         "size": {"type": "integer", "description": "Number of documents, 1-20, default 8"}}),
    _fn("count_documents", "Counts the documents matching search words, phrases and file type.",
        {"query": _QUERY, "phrases": _PHRASES, "extension": _EXT}),
    _fn("count_term", "How often does a name or term occur? Returns the document count, literal occurrences and the "
        "documents with the most occurrences.", {"phrase": _STR, "extension": _EXT}, ["phrase"]),
    _fn("find_patterns", "Counts patterns across all documents: email addresses (email), web addresses (url), "
        "domains (domain) or dates (date). Returns counts and the most frequent values. Optionally only in "
        "documents containing certain phrases. Takes several minutes on large archives.",
        {"kind": {"type": "string", "enum": PATTERN_KINDS}, "phrases": _PHRASES, "extension": _EXT,
         "top": {"type": "integer", "description": "Number of entries in the ranking, default 20"}}, ["kind"]),
    _fn("group_by", "Distribution of the documents by file type (extension), author (author), content type "
        "(content_type), language (language) or modification year (mtime_year).",
        {"field": {"type": "string", "enum": GROUP_FIELDS}, "query": _QUERY, "phrases": _PHRASES,
         "extension": _EXT, "size": {"type": "integer", "description": "Number of groups, default 10"}}, ["field"]),
    _fn("find_files", "Finds documents by FILENAME (also just a part of it, case-insensitive), not by content. "
        "Shows size and text length; reports when no file with this name is in the index.",
        {"name": {"type": "string", "description": "Part of the filename, e.g. 'report' or '000304_k1'"},
         "extension": _EXT, "size": {"type": "integer", "description": "Number of hits, default 10"}}, ["name"]),
    _fn("get_document", "Reads text from a single document (exact filename from a search result). "
        "With 'around' the places around a term, otherwise starting at 'offset'.",
        {"filename": _STR, "around": {"type": "string", "description": "Term around which excerpts are returned"},
         "offset": {"type": "integer"}, "length": {"type": "integer", "description": "Characters, max 6000"}},
        ["filename"]),
]

_FUNCS = {f.__name__: f for f in (search_documents, count_documents, count_term, find_patterns, group_by,
                                  find_files, get_document)}


def run_tool(ctx, name, args):
    """Run a tool and return the result as (shortened) JSON text."""
    func = _FUNCS.get(name)
    if func is None:
        return json.dumps({"error": f"unknown tool '{name}'"})
    args = dict(args or {})
    if isinstance(args.get("phrases"), str):  # models sometimes pass a string instead of a list
        args["phrases"] = [args["phrases"]]
    try:
        result = func(ctx, **args)
    except Cancelled:
        raise
    except TypeError as e:
        return json.dumps({"error": f"invalid arguments: {e}"}, ensure_ascii=False)
    except Exception as e:  # report errors to the model so it can try another way
        return json.dumps({"error": str(e)[:300]}, ensure_ascii=False)
    return to_json({k: v for k, v in result.items() if v is not None}, ctx.max_result_chars)
