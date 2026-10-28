"""Tests for the semantic search: chunking, the embedding client (fake local server), the job and the tool."""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

from docsearch import config, embed_job, embeddings, tools
from docsearch.embeddings import EmbeddingError


class FakeServer:
    """A local server that answers /v1/embeddings with a 3-dimensional vector per text (in reversed order)."""

    def __init__(self, status=200):
        outer = self
        self.requests, self.status = [], status

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(body)
                if outer.status != 200:
                    self.send_response(outer.status)
                    self.send_header("Content-Length", "2")
                    self.end_headers()
                    self.wfile.write(b"{}")
                    return
                data = [{"index": i, "embedding": [float(len(t)), 1.0, 0.0]} for i, t in enumerate(body["input"])]
                out = json.dumps({"data": data[::-1]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class ServerTestCase(unittest.TestCase):
    def serve(self, status=200, model="fake-embed"):
        server = FakeServer(status)
        self.addCleanup(server.close)
        for name, value in (("LLM_BASE_URL", server.url), ("EMBED_MODEL", model)):
            patcher = mock.patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        return server


class ChunkTest(unittest.TestCase):
    def test_chunks_overlap_cut_at_spaces_and_keep_their_offsets(self):
        text = "word " * 700
        chunks = embeddings.chunk_text(text, size=1000, max_chunks=50)
        self.assertGreater(len(chunks), 3)
        for start, piece in chunks:
            self.assertLessEqual(len(piece), 1000)
            self.assertTrue(text[start:].lstrip().startswith(piece[:20]))
        self.assertLess(chunks[1][0], chunks[0][0] + 1000)  # overlap

    def test_only_the_first_chunks_are_kept(self):
        self.assertEqual(len(embeddings.chunk_text("x " * 10000, size=200, max_chunks=5)), 5)

    def test_empty_and_blank_text_have_no_chunks(self):
        self.assertEqual(embeddings.chunk_text(""), [])
        self.assertEqual(embeddings.chunk_text("   \n  "), [])

    def test_vector_mapping_is_nested_cosine(self):
        props = embeddings.vector_properties(768)
        vector = props["chunks"]["properties"]["vector"]
        self.assertEqual((props["chunks"]["type"], vector["dims"], vector["similarity"]), ("nested", 768, "cosine"))


class ClientTest(ServerTestCase):
    def test_vectors_come_back_in_the_order_of_the_texts(self):
        self.serve()
        self.assertEqual([v[0] for v in embeddings.embed(["a", "bbb", "cc"])], [1.0, 3.0, 2.0])

    def test_long_lists_are_sent_in_batches(self):
        server = self.serve()
        embeddings.embed(["x"] * 40)
        self.assertEqual([len(r["input"]) for r in server.requests], [16, 16, 8])

    def test_nomic_models_get_task_prefixes(self):
        server = self.serve(model="text-embedding-nomic-embed-text-v1.5")
        embeddings.embed(["hello"])
        embeddings.embed(["hello"], "query")
        self.assertEqual([r["input"][0] for r in server.requests], ["search_document: hello", "search_query: hello"])

    def test_other_models_get_no_prefix(self):
        server = self.serve()
        embeddings.embed(["hello"])
        self.assertEqual(server.requests[0]["input"], ["hello"])

    def test_a_model_that_is_not_loaded_gives_a_helpful_error_with_status(self):
        self.serve(status=404)
        with self.assertRaises(EmbeddingError) as raised:
            embeddings.embed(["x"])
        self.assertEqual(raised.exception.status, 404)
        self.assertIn("loaded in LM Studio", str(raised.exception))

    def test_unreachable_server_has_no_status(self):
        with mock.patch.object(config, "LLM_BASE_URL", "http://127.0.0.1:9"):
            with self.assertRaises(EmbeddingError) as raised:
                embeddings.embed(["x"])
        self.assertIsNone(raised.exception.status)


class FakeEs:
    def __init__(self, docs):
        self.docs, self.updates, self.mapping_dims, self.refreshed = docs, {}, None, False

    def ensure_vector_fields(self, index, dims):
        self.mapping_dims = dims

    def count(self, index, query):
        return len([d for d in self.docs if d["_id"] not in self.updates])

    def scan(self, index, query, fields, limit, page=100, should_stop=None):
        for doc in [d for d in self.docs if d["_id"] not in self.updates][:limit]:
            if should_stop and should_stop():
                return
            yield doc

    def bulk_update(self, index, updates):
        self.updates.update(dict(updates))
        return {}

    def call(self, method, path, body=None, **kw):
        self.refreshed = path.endswith("_refresh")


def doc(doc_id, text):
    return {"_id": doc_id, "_source": {"content": text}}


class JobTest(ServerTestCase):
    def test_every_document_gets_vectors_empty_ones_are_marked(self):
        self.serve()
        es = FakeEs([doc("a", "some words " * 50), doc("b", ""), doc("c", "other text " * 300)])
        progress = []
        stats = embed_job.embed_index(es, "x", on_progress=progress.append, batch_size=2)
        self.assertEqual((stats["ok"], stats["empty"], stats["errors"], stats["total"]), (2, 1, 0, 3))
        self.assertEqual(es.mapping_dims, 3)
        self.assertEqual(es.updates["b"], {"embedded_chunks": 0})
        self.assertEqual(es.updates["a"]["embedded_chunks"], len(es.updates["a"]["chunks"]))
        self.assertEqual(es.updates["a"]["chunks"][0]["start"], 0)
        self.assertEqual(progress[-1]["done"], 3)
        self.assertTrue(es.refreshed)

    def test_a_second_run_has_nothing_left_to_do(self):
        self.serve()
        es = FakeEs([doc("a", "some words " * 50)])
        embed_job.embed_index(es, "x")
        self.assertEqual(embed_job.embed_index(es, "x")["total"], 0)

    def test_a_scan_that_ends_early_is_repeated_for_the_documents_left(self):
        class EarlyEnd(FakeEs):
            passes = 0

            def scan(self, index, query, fields, limit, page=100, should_stop=None):
                self.passes += 1
                for i, d in enumerate(super().scan(index, query, fields, limit, page, should_stop)):
                    if self.passes == 1 and i >= 3:
                        return  # the first pass stops after 3 documents, like the real one did
                    yield d

        self.serve()
        es = EarlyEnd([doc(str(i), "text " * 40) for i in range(8)])
        stats = embed_job.embed_index(es, "x", batch_size=1)
        self.assertEqual(stats["ok"], 8)
        self.assertEqual(len(es.updates), 8)
        self.assertEqual(es.passes, 2)

    def test_limit_and_stop(self):
        self.serve()
        es = FakeEs([doc(str(i), "text " * 40) for i in range(6)])
        self.assertEqual(embed_job.embed_index(es, "x", limit=2, batch_size=1)["ok"], 2)
        stopped = embed_job.embed_index(FakeEs([doc("a", "text")]), "x", should_stop=lambda: True)
        self.assertTrue(stopped["stopped"])

    def test_an_unusable_model_stops_the_run_instead_of_marking_every_document(self):
        self.serve()
        es = FakeEs([doc(str(i), "text " * 40) for i in range(20)])
        real = embeddings.embed
        calls = []

        def broken(texts, kind="document"):
            calls.append(1)
            if len(calls) == 1:
                return real(texts, kind)  # the probe works
            raise EmbeddingError("model crashed", 500)

        with mock.patch.object(embeddings, "embed", broken):
            with self.assertRaises(EmbeddingError):
                embed_job.embed_index(es, "x", batch_size=1)
        failed = [u for u in es.updates.values() if u.get("embedded_chunks") == -1]
        self.assertLess(len(failed), embed_job.MAX_IN_A_ROW)

    def test_server_gone_aborts_at_once(self):
        with mock.patch.object(config, "LLM_BASE_URL", "http://127.0.0.1:9"):
            with self.assertRaises(EmbeddingError):
                embed_job.embed_index(FakeEs([doc("a", "text")]), "x")


class ToolTest(ServerTestCase):
    class Es:
        def __init__(self, vectors=True):
            self.vectors, self.bodies = vectors, []

        def embedding_stats(self, index):
            return {"embedded": 5 if self.vectors else 0, "missing": 0, "dims": 3}

        def search(self, index, body):
            self.bodies.append(body)
            intro = "Intro text. " * 20
            text = intro + "The contract was cancelled on Monday. " + "tail " * 50
            inner = {"fields": {"chunks": [{"start": [len(intro)]}]}}
            return {"hits": {"hits": [{"_score": 0.8766, "inner_hits": {"chunks": {"hits": {"hits": [inner]}}},
                                       "_source": {"filename": "a.pdf", "path": "x/a.pdf", "content": text}}]}}

    def names(self, es):
        return [t["function"]["name"] for t in tools.tools_for(tools.Context(es, "x", 8000))]

    def test_tool_list_contains_the_semantic_search_only_with_vectors(self):
        self.assertIn("semantic_search", self.names(self.Es(True)))
        self.assertNotIn("semantic_search", self.names(self.Es(False)))
        self.assertNotIn("semantic_search", self.names(None))  # no Elasticsearch: no vectors, and no crash

    def test_query_uses_nested_knn_and_the_result_shows_the_best_passage(self):
        self.serve()
        es = self.Es()
        ctx = tools.Context(es, "x", 12000)
        result = json.loads(tools.run_tool(ctx, "semantic_search",
                                           {"question": "ending a contract", "extension": ".PDF", "size": 3}))
        knn = es.bodies[0]["knn"]
        self.assertEqual((knn["field"], knn["k"], knn["filter"]), ("chunks.vector", 3, {"term": {"extension": "pdf"}}))
        self.assertEqual(len(knn["query_vector"]), 3)
        hit = result["hits"][0]
        self.assertEqual((hit["doc"], hit["similarity"]), ("a.pdf", 0.877))
        self.assertTrue(hit["excerpts"][0].startswith("The contract was cancelled"))
        self.assertEqual(ctx.sources["a.pdf"], "x/a.pdf")

    def test_embedding_errors_are_reported_to_the_model(self):
        with mock.patch.object(config, "LLM_BASE_URL", "http://127.0.0.1:9"):
            result = tools.run_tool(tools.Context(self.Es(), "x", 8000), "semantic_search", {"question": "q"})
        self.assertIn("not reachable", result)


if __name__ == "__main__":
    unittest.main()
