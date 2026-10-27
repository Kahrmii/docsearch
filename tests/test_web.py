"""Tests for the local web interface: a real server on a free port, no Elasticsearch/LLM/Tika needed."""
import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from docsearch import chat as chat_module
from docsearch import cloud_policy, config, conversations, files, health, indexing, preferences, secrets_check, ui_common, webserver


class FakeEs:
    def __init__(self):
        self.deleted = []

    def indices(self):
        return [("documents", 5), ("keys", 2)]

    def index_info(self, index):
        return {"documents": 5, "size_mb": 1} if index in ("documents", "keys") else None

    def delete_index(self, index):
        self.deleted.append(index)


class WebTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # never touch the real settings and approvals of the installation
        for module, name, filename in ((ui_common, "SETTINGS_FILE", "gui_settings.json"),
                                       (cloud_policy, "POLICY_FILE", "cloud_indices.json")):
            patcher = mock.patch.object(module, name, Path(self.tmp.name) / filename)
            patcher.start()
            self.addCleanup(patcher.stop)

    def start_server(self):
        self.state = webserver.WebState()
        self.state._es = FakeEs()
        self.server = webserver.make_server(self.state, 18765)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, body=None, token=True, headers=None):
        all_headers = {"Content-Type": "application/json"}
        if token:
            all_headers["X-Docsearch-Token"] = self.state.token
        all_headers.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request(method, path, json.dumps(body) if body is not None else None, all_headers)
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        try:
            data = json.loads(raw)
        except ValueError:
            data = raw.decode("utf-8", "replace")
        return response.status, data, response

    def wait_until(self, condition, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            if condition():
                return True
            time.sleep(0.05)
        return False


class SecurityTest(WebTestCase):
    def setUp(self):
        super().setUp()
        self.start_server()

    def test_page_needs_the_token_in_the_url(self):
        self.assertEqual(self.request("GET", "/", token=False)[0], 403)
        self.assertEqual(self.request("GET", "/?token=wrong", token=False)[0], 403)
        status, html, response = self.request("GET", f"/?token={self.state.token}", token=False)
        self.assertEqual(status, 200)
        self.assertIn(f'content="{self.state.token}"', html)
        self.assertIn("default-src 'self'", response.getheader("Content-Security-Policy"))
        self.assertEqual(response.getheader("Referrer-Policy"), "no-referrer")

    def test_api_needs_the_token_header(self):
        self.assertEqual(self.request("GET", "/api/state", token=False)[0], 403)
        self.assertEqual(self.request("GET", "/api/state", token=False, headers={"X-Docsearch-Token": "x"})[0], 403)
        status, data, _ = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual([i["name"] for i in data["indices"]], ["documents", "keys"])

    def test_foreign_host_header_is_rejected(self):
        status, _, _ = self.request("GET", "/api/state", headers={"Host": "evil.example:80"})
        self.assertEqual(status, 403)  # DNS rebinding

    def test_foreign_origin_is_rejected(self):
        status, _, _ = self.request("POST", "/api/chat/stop", {"chat_id": "c"},
                                    headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)

    def test_same_origin_is_accepted(self):
        status, _, _ = self.request("POST", "/api/chat/stop", {"chat_id": "c"},
                                    headers={"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(status, 200)

    def test_only_listed_static_files_are_served(self):
        self.assertEqual(self.request("GET", "/static/app.js", token=False)[0], 200)
        self.assertEqual(self.request("GET", "/static/app.css", token=False)[0], 200)
        self.assertEqual(self.request("GET", "/static/index.html", token=False)[0], 404)
        self.assertEqual(self.request("GET", "/static/../webserver.py", token=False)[0], 404)

    def test_bad_requests(self):
        self.assertEqual(self.request("GET", "/api/unknown")[0], 404)
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request("POST", "/api/cloud", "{not json", {"X-Docsearch-Token": self.state.token})
        self.assertEqual(connection.getresponse().status, 400)

    def test_server_listens_on_loopback_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def test_a_busy_port_is_not_shared_but_the_next_free_one_is_used(self):
        second = webserver.make_server(self.state, self.port)
        self.addCleanup(second.server_close)
        self.assertNotEqual(second.server_address[1], self.port)


class OpenSourceTest(WebTestCase):
    def setUp(self):
        super().setUp()
        self.start_server()

    def test_only_files_named_as_a_source_can_be_opened(self):
        chat = self.state.chat("c1")
        chat.session.sources["report.pdf"] = r"C:\docs\report.pdf"
        with mock.patch.object(files, "open_source", return_value=True) as opener:
            self.assertEqual(self.request("POST", "/api/open", {"chat_id": "c1", "filename": "report.pdf"})[0], 200)
            opener.assert_called_once_with(r"C:\docs\report.pdf")
            opener.reset_mock()
            self.assertEqual(self.request("POST", "/api/open", {"chat_id": "c1", "filename": "other.pdf"})[0], 404)
            self.assertEqual(self.request("POST", "/api/open", {"chat_id": "c1", "filename": r"C:\Windows\x.exe"})[0], 404)
            self.assertEqual(self.request("POST", "/api/open", {"chat_id": "nobody", "filename": "report.pdf"})[0], 404)
            opener.assert_not_called()


class ChatFlowTest(WebTestCase):
    def setUp(self):
        super().setUp()
        self.start_server()
        self.gate = threading.Event()

        def fake_run(session, question, index, provider, emit):
            emit("status", "Working ...")
            emit("step", ("count_documents", {"query": "x"}))
            self.gate.wait(5)
            session.sources["a.pdf"] = r"C:\a.pdf"
            emit("answer", f"There are 7 documents [a.pdf] ({provider}/{index}).")

        patcher = mock.patch.object(chat_module.ChatSession, "run", fake_run)
        patcher.start()
        self.addCleanup(patcher.stop)

    def poll_until_done(self, since=0):
        events = []
        end = time.time() + 10
        while time.time() < end:
            _, data, _ = self.request("GET", f"/api/chat/poll?chat_id=c1&since={since}")
            since = data["next"]
            events += data["events"]
            if any(e["type"] == "done" for e in events):
                return events
            time.sleep(0.05)
        self.fail("no 'done' event")

    def test_question_flows_into_events(self):
        self.gate.set()
        status, _, _ = self.request("POST", "/api/chat/ask", {"chat_id": "c1", "question": "How many?",
                                                              "index": "documents", "provider": "local"})
        self.assertEqual(status, 200)
        events = self.poll_until_done()
        self.assertEqual([e["type"] for e in events], ["user", "status", "step", "answer", "done"])
        answer = next(e for e in events if e["type"] == "answer")
        self.assertIn("7 documents", answer["text"])
        self.assertEqual(answer["sources"], ["a.pdf"])  # names only, never paths
        self.assertNotIn("C:", json.dumps(events))

    def test_second_question_while_busy_is_rejected(self):
        body = {"chat_id": "c1", "question": "First?", "index": "documents", "provider": "local"}
        self.assertEqual(self.request("POST", "/api/chat/ask", body)[0], 200)
        self.assertEqual(self.request("POST", "/api/chat/ask", {**body, "question": "Second?"})[0], 409)
        self.gate.set()
        self.poll_until_done()

    def test_validation(self):
        self.assertEqual(self.request("POST", "/api/chat/ask", {"chat_id": "c1", "question": "",
                                                                "index": "documents"})[0], 400)
        self.assertEqual(self.request("POST", "/api/chat/ask", {"chat_id": "c1", "question": "x", "index": "d",
                                                                "provider": "nope"})[0], 400)

    def test_switching_index_starts_a_new_conversation(self):
        self.gate.set()
        body = {"chat_id": "c1", "question": "One?", "index": "documents", "provider": "local"}
        self.request("POST", "/api/chat/ask", body)
        since = len(self.poll_until_done())
        self.state.chat("c1").session.history.append({"role": "user", "content": "excerpts of documents"})
        self.request("POST", "/api/chat/ask", {**body, "index": "keys", "question": "Two?"})
        events = self.poll_until_done(since)
        self.assertEqual(events[0]["type"], "info")
        self.assertEqual(self.state.chat("c1").session.history, [])


class CloudApprovalTest(WebTestCase):
    def setUp(self):
        super().setUp()
        self.start_server()
        self.risky = {"documents": 100, "pem_private_keys": 2, "credential_keywords": 50, "credential_filenames": 3}
        self.clean = {"documents": 100, "pem_private_keys": 0, "credential_keywords": 1, "credential_filenames": 0}

    def scan_returns(self, findings):
        patcher = mock.patch.object(secrets_check, "scan", return_value=findings)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_cloud_question_is_refused_without_approval(self):
        status, data, _ = self.request("POST", "/api/chat/ask", {"chat_id": "c", "question": "x", "index": "documents",
                                                                  "provider": "anthropic"})
        self.assertEqual(status, 403)
        self.assertIn("not approved for the cloud", data["error"])

    def test_risky_index_needs_confirmation_and_stays_blocked_until_confirmed(self):
        self.scan_returns(self.risky)
        status, data, _ = self.request("POST", "/api/cloud", {"index": "documents", "allowed": True})
        self.assertTrue(data["needs_confirmation"])
        self.assertIn("2 documents with private key headers", data["message"])
        self.assertFalse(cloud_policy.is_allowed("documents"))  # not approved yet
        status, data, _ = self.request("POST", "/api/cloud", {"index": "documents", "allowed": True, "confirm": True})
        self.assertEqual(status, 200)
        self.assertTrue(cloud_policy.is_allowed("documents"))

    def test_clean_index_is_approved_directly(self):
        self.scan_returns(self.clean)
        status, data, _ = self.request("POST", "/api/cloud", {"index": "documents", "allowed": True})
        self.assertEqual(status, 200)
        self.assertNotIn("needs_confirmation", data)
        self.assertTrue(cloud_policy.is_allowed("documents"))

    def test_approved_but_risky_index_asks_again_at_the_first_cloud_question(self):
        cloud_policy.set_allowed("documents", True)
        self.scan_returns(self.risky)
        body = {"chat_id": "c", "question": "x", "index": "documents", "provider": "anthropic"}
        self.assertTrue(self.request("POST", "/api/chat/ask", body)[1]["needs_confirmation"])
        with mock.patch.object(chat_module.ChatSession, "run", lambda *a, **k: None):
            self.assertEqual(self.request("POST", "/api/chat/ask", {**body, "confirm": True})[0], 200)

    def test_withdrawing_the_approval(self):
        cloud_policy.set_allowed("documents", True)
        self.assertEqual(self.request("POST", "/api/cloud", {"index": "documents", "allowed": False})[0], 200)
        self.assertFalse(cloud_policy.is_allowed("documents"))


class IndexFlowTest(WebTestCase):
    def setUp(self):
        super().setUp()
        self.start_server()

    def test_job_reports_progress_and_message(self):
        def fake_job(opts, on_progress=None, should_stop=None, on_status=None):
            on_status("Starting Docker containers ...")
            on_progress({"done": 1, "total": 2, "ok": 1, "errors": 0, "skipped": 0, "rate": 1.0, "eta_s": 1})
            return "Done. ok 2, errors 0, skipped 0.", opts.index

        with mock.patch.object(indexing, "run_job", fake_job):
            body = {"folder": self.tmp.name, "index": "my_index", "recursive": True}
            self.assertEqual(self.request("POST", "/api/index/start", body)[0], 200)
            self.assertTrue(self.wait_until(lambda: not self.request("GET", "/api/index/status")[1]["running"]))
        data = self.request("GET", "/api/index/status")[1]
        self.assertEqual(data["message"], "Done. ok 2, errors 0, skipped 0.")
        self.assertEqual(data["progress"]["done"], 1)
        saved = json.loads((Path(self.tmp.name) / "gui_settings.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["index"], "my_index")

    def test_validation(self):
        self.assertEqual(self.request("POST", "/api/index/start", {"folder": "Z:\\does\\not\\exist",
                                                                   "index": "ok"})[0], 400)
        self.assertEqual(self.request("POST", "/api/index/start", {"folder": self.tmp.name,
                                                                   "index": "Bad Name"})[0], 400)


class IndexDeleteTest(WebTestCase):
    def setUp(self):
        super().setUp()
        self.start_server()

    def delete(self, index, confirm=None):
        body = {"index": index} if confirm is None else {"index": index, "confirm": confirm}
        return self.request("POST", "/api/index/delete", body)

    def test_info_gives_facts_and_the_warning(self):
        status, data, _ = self.request("GET", "/api/index/info?index=documents")
        self.assertEqual(status, 200)
        self.assertEqual((data["documents"], data["size_mb"]), (5, 1))
        self.assertIn("Permanently delete the index 'documents'", data["warning"])
        self.assertEqual(self.request("GET", "/api/index/info?index=nope")[0], 404)
        self.assertEqual(self.request("GET", "/api/index/info?index=*")[0], 400)

    def test_the_server_requires_the_typed_name(self):
        for confirm in (None, "", "document", "keys"):
            self.assertEqual(self.delete("documents", confirm)[0], 400)
        self.assertEqual(self.state._es.deleted, [])

    def test_confirmed_delete_removes_index_and_approval(self):
        cloud_policy.set_allowed("documents", True)
        self.state.cloud_confirmed.add("documents")
        status, data, _ = self.delete("documents", "documents")
        self.assertEqual(status, 200)
        self.assertEqual(data["documents"], 5)
        self.assertEqual(self.state._es.deleted, ["documents"])
        self.assertFalse(cloud_policy.is_allowed("documents"))
        self.assertNotIn("documents", self.state.cloud_confirmed)

    def test_wildcards_and_unknown_indices_are_never_deleted(self):
        self.assertEqual(self.delete("*", "*")[0], 400)
        self.assertEqual(self.delete("_all", "_all")[0], 400)
        self.assertEqual(self.delete("nope", "nope")[0], 404)
        self.assertEqual(self.state._es.deleted, [])

    def test_no_deleting_while_indexing_runs(self):
        self.state.index_run.running = True
        self.assertEqual(self.delete("documents", "documents")[0], 409)
        self.assertEqual(self.state._es.deleted, [])

    def test_the_token_is_required(self):
        status, _, _ = self.request("POST", "/api/index/delete", {"index": "documents", "confirm": "documents"},
                                    token=False)
        self.assertEqual(status, 403)
        self.assertEqual(self.state._es.deleted, [])


class StatusAndSettingsTest(WebTestCase):
    def setUp(self):
        super().setUp()
        env = Path(self.tmp.name) / ".env"
        env.write_text("ELASTIC_PASSWORD=abc\n", encoding="utf-8")
        for patcher in (mock.patch.object(config, "ENV_FILE", env), mock.patch.object(config, "_file_cache", None)):
            patcher.start()
            self.addCleanup(patcher.stop)
        for field in preferences.FIELDS:  # restore the running configuration afterwards
            self.addCleanup(setattr, config, field["key"], getattr(config, field["key"]))
        self.start_server()

    def test_health_lists_the_checks(self):
        checks = [{"name": "Tika", "status": "ok", "detail": "fine"}]
        with mock.patch.object(health, "run_checks", return_value=checks):
            status, data, _ = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(data["checks"], checks)
        self.assertTrue(data["version"])

    def test_settings_can_be_read_and_saved(self):
        status, data, _ = self.request("GET", "/api/settings")
        self.assertEqual(status, 200)
        self.assertIn("OCR_LANGUAGES", [f["key"] for f in data["fields"]])
        status, _, _ = self.request("POST", "/api/settings/save", {"OCR_LANGUAGES": "deu+eng", "INDEX_WORKERS": "3"})
        self.assertEqual(status, 200)
        self.assertEqual((config.OCR_LANGUAGES, config.INDEX_WORKERS), ("deu+eng", 3))

    def test_invalid_settings_and_secrets_are_refused(self):
        for body in ({"INDEX_WORKERS": "99"}, {"ELASTIC_PASSWORD": "x"}, {"ANTHROPIC_API_KEY": "sk-x"}):
            self.assertEqual(self.request("POST", "/api/settings/save", body)[0], 400)
        self.assertEqual(config.get("ELASTIC_PASSWORD"), "abc")

    def test_the_token_is_required(self):
        self.assertEqual(self.request("GET", "/api/settings", token=False)[0], 403)
        self.assertEqual(self.request("POST", "/api/settings/save", {"MAX_FILE_MB": "5"}, token=False)[0], 403)
        self.assertEqual(self.request("GET", "/api/health", token=False)[0], 403)


class ConversationApiTest(WebTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(conversations, "CONV_DIR", Path(self.tmp.name) / "conversations")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.conv = {"id": conversations.new_id(), "title": "Contracts", "index": "documents", "provider": "local",
                     "turns": [{"question": "Who signed?", "answer": "Anna [a.pdf].", "sources": ["a.pdf"]}],
                     "sources": {"a.pdf": "C:/docs/a.pdf"}}
        conversations.save(self.conv)
        self.start_server()

    def test_list_and_load_replays_the_conversation(self):
        _, data, _ = self.request("GET", "/api/conversations")
        self.assertEqual([c["title"] for c in data["conversations"]], ["Contracts"])
        status, data, _ = self.request("POST", "/api/conversations/load", {"chat_id": "tab1", "id": self.conv["id"]})
        self.assertEqual((status, data["index"]), (200, "documents"))
        _, poll, _ = self.request("GET", "/api/chat/poll?chat_id=tab1&since=0")
        kinds = [e["type"] for e in poll["events"]]
        self.assertEqual(kinds, ["info", "user", "answer"])
        self.assertEqual(poll["events"][2]["sources"], ["a.pdf"])  # the file links work in the replay
        self.assertEqual(self.state.chats["tab1"].session.history[0]["content"], "Who signed?")

    def test_unknown_and_malformed_ids(self):
        self.assertEqual(self.request("POST", "/api/conversations/load", {"chat_id": "t", "id": "x" * 32})[0], 404)
        self.assertEqual(self.request("POST", "/api/conversations/load", {"chat_id": "t", "id": "../../x"})[0], 404)
        self.assertEqual(self.request("POST", "/api/conversations/delete", {"id": "../../x"})[0], 404)

    def test_rename_and_delete(self):
        self.request("POST", "/api/conversations/load", {"chat_id": "tab1", "id": self.conv["id"]})
        self.assertEqual(self.request("POST", "/api/conversations/rename",
                                      {"id": self.conv["id"], "title": "Signatures"})[0], 200)
        self.assertEqual(conversations.listing()[0]["title"], "Signatures")
        self.assertEqual(self.request("POST", "/api/conversations/rename", {"id": self.conv["id"], "title": " "})[0], 404)
        self.assertEqual(self.request("POST", "/api/conversations/delete", {"id": self.conv["id"]})[0], 200)
        self.assertEqual(conversations.listing(), [])
        self.assertIsNone(self.state.chats["tab1"].session.conv_id)  # the open copy is detached from the deleted file

    def test_export_of_the_open_conversation(self):
        self.assertEqual(self.request("GET", "/api/conversations/export?chat_id=none")[0], 404)
        self.request("POST", "/api/conversations/load", {"chat_id": "tab1", "id": self.conv["id"]})
        status, data, _ = self.request("GET", "/api/conversations/export?chat_id=tab1")
        self.assertEqual(status, 200)
        self.assertEqual(data["filename"], "Contracts.md")
        self.assertIn("## 1. Who signed?", data["markdown"])
        self.assertNotIn("C:/docs", data["markdown"])

    def test_a_busy_conversation_cannot_be_replaced(self):
        self.state.chat("tab1").busy = True
        self.assertEqual(self.request("POST", "/api/conversations/load", {"chat_id": "tab1", "id": self.conv["id"]})[0], 409)

    def test_the_token_is_required(self):
        self.assertEqual(self.request("GET", "/api/conversations", token=False)[0], 403)
        self.assertEqual(self.request("POST", "/api/conversations/delete", {"id": self.conv["id"]}, token=False)[0], 403)
        self.assertEqual(len(conversations.listing()), 1)


class EmbedApiTest(WebTestCase):
    def setUp(self):
        super().setUp()
        self.start_server()

    def test_embedding_job_runs_like_an_indexing_job(self):
        def fake_job(index, on_progress=None, should_stop=None, on_status=None, **kw):
            on_progress({"done": 2, "total": 2, "ok": 2, "errors": 0, "skipped": 0, "rate": 1.0, "eta_s": 0})
            return f"Done. vectors for 2 documents in {index}."

        with mock.patch.object(webserver.embed_job, "run_job", fake_job):
            self.assertEqual(self.request("POST", "/api/embed/start", {"index": "documents"})[0], 200)
            self.assertTrue(self.wait_until(lambda: not self.state.index_run.running))
        _, status, _ = self.request("GET", "/api/index/status?since=0")
        self.assertEqual((status["kind"], status["message"]), ("embed", "Done. vectors for 2 documents in documents."))
        self.assertEqual(status["progress"]["done"], 2)

    def test_bad_names_and_a_running_job_are_refused(self):
        self.assertEqual(self.request("POST", "/api/embed/start", {"index": "../x"})[0], 400)
        self.assertEqual(self.request("POST", "/api/embed/start", {"index": "*"})[0], 400)
        self.state.index_run.running = True
        self.assertEqual(self.request("POST", "/api/embed/start", {"index": "documents"})[0], 409)

    def test_the_token_is_required(self):
        self.assertEqual(self.request("POST", "/api/embed/start", {"index": "documents"}, token=False)[0], 403)


if __name__ == "__main__":
    unittest.main()
