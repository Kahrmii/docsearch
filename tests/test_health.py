"""Tests for the health overview (all services faked)."""
import unittest
from unittest import mock

from docsearch import config, health
from docsearch.es import EsError

MODELS = {"data": [
    {"id": "qwen/qwen3-14b", "type": "llm", "state": "loaded", "loaded_context_length": 12544},
    {"id": "nomic-embed", "type": "embeddings", "state": "loaded", "loaded_context_length": 2048},
    {"id": "other", "type": "llm", "state": "not-loaded"}]}


class FakeEs:
    def __init__(self, status="green", used_percent=10, heap=40, fail=False):
        self.status, self.used, self.heap, self.fail = status, used_percent, heap, fail

    def call(self, method, path, body=None, **kw):
        if self.fail:
            raise EsError("down", 0)
        if path == "/_cluster/health":
            return {"status": self.status}
        total = 1000 ** 3
        return {"nodes": {"n": {"fs": {"total": {"total_in_bytes": total,
                                                 "available_in_bytes": total * (100 - self.used) // 100}},
                                "jvm": {"mem": {"heap_used_percent": self.heap}}}}}

    def indices(self):
        return [("a", 10), ("b", 5)]


class ElasticsearchTest(unittest.TestCase):
    def test_healthy(self):
        result = health.check_elasticsearch(FakeEs())
        self.assertEqual(result["status"], health.OK)
        self.assertIn("disk 10% used", result["detail"])

    def test_yellow_and_full_disk_warn(self):
        self.assertEqual(health.check_elasticsearch(FakeEs(status="yellow"))["status"], health.WARN)
        self.assertEqual(health.check_elasticsearch(FakeEs(used_percent=90))["status"], health.WARN)

    def test_red_is_an_error(self):
        self.assertEqual(health.check_elasticsearch(FakeEs(status="red"))["status"], health.ERROR)

    def test_unreachable_is_an_error_not_an_exception(self):
        self.assertEqual(health.check_elasticsearch(FakeEs(fail=True))["status"], health.ERROR)

    def test_indices(self):
        self.assertIn("2 indices, 15 documents", health.check_indices(FakeEs())["detail"])


class LlmTest(unittest.TestCase):
    def check(self, models=MODELS, model="qwen/qwen3-14b", provider="local"):
        with mock.patch.object(health, "_get_json", return_value=models), \
                mock.patch.object(config, "LLM_MODEL", model), mock.patch.object(config, "LLM_PROVIDER", provider):
            return health.check_llm()

    def test_loaded_model_with_enough_context(self):
        result = self.check()
        self.assertEqual(result["status"], health.OK)
        self.assertIn("12544", result["detail"])

    def test_small_context_warns(self):
        small = {"data": [{"id": "qwen/qwen3-14b", "state": "loaded", "loaded_context_length": 4096}]}
        self.assertEqual(self.check(small)["status"], health.WARN)

    def test_model_not_loaded(self):
        result = self.check(model="other")
        self.assertEqual(result["status"], health.ERROR)
        self.assertIn("qwen/qwen3-14b", result["detail"])  # tells what is loaded

    def test_unreachable_is_only_a_warning_in_cloud_mode(self):
        with mock.patch.object(health, "_get_json", side_effect=OSError("refused")):
            with mock.patch.object(config, "LLM_PROVIDER", "anthropic"):
                self.assertEqual(health.check_llm()["status"], health.WARN)
            with mock.patch.object(config, "LLM_PROVIDER", "local"):
                self.assertEqual(health.check_llm()["status"], health.ERROR)


class MiscTest(unittest.TestCase):
    def test_container_memory_parses_docker_stats(self):
        out = mock.Mock(stdout="docsearch-tika|3.7GiB / 4GiB|94.00%\nother|1GiB / 2GiB|50%\nbroken line\n")
        with mock.patch.object(health.subprocess, "run", return_value=out):
            self.assertEqual(health.container_memory(), {"docsearch-tika": ("3.7GiB / 4GiB", 94.0)})

    def test_container_close_to_the_limit_warns(self):
        with mock.patch.object(health, "container_memory", return_value={"docsearch-tika": ("x", 94.0)}):
            self.assertEqual(health.check_containers()["status"], health.WARN)

    def test_cloud_check_never_shows_the_key(self):
        with mock.patch.object(config, "get", return_value="sk-secret-value"):
            self.assertNotIn("sk-secret", health.check_cloud()["detail"])

    def test_a_crashing_check_becomes_an_error_entry(self):
        with mock.patch.object(health, "check_config", side_effect=RuntimeError("boom")), \
                mock.patch.object(health, "check_elasticsearch", return_value=health._check("ES", health.OK, "")), \
                mock.patch.object(health, "check_tika", return_value=health._check("T", health.OK, "")), \
                mock.patch.object(health, "check_containers", return_value=health._check("C", health.OK, "")), \
                mock.patch.object(health, "check_indices", return_value=health._check("I", health.OK, "")), \
                mock.patch.object(health, "check_llm", return_value=health._check("L", health.OK, "")), \
                mock.patch.object(health, "check_embeddings", return_value=health._check("M", health.OK, "")), \
                mock.patch.object(health, "check_cloud", return_value=health._check("K", health.OK, "")):
            checks = health.run_checks()
        self.assertEqual(health.overall(checks), health.ERROR)
        self.assertIn("boom", health.format_text(checks))

    def test_localhost_in_an_address_warns(self):
        with mock.patch.object(config, "ES_URL", "http://localhost:9200"):
            self.assertEqual(health.check_config()["status"], health.WARN)


if __name__ == "__main__":
    unittest.main()
