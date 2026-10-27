"""Tests for the cloud connection and approval: no real cloud access, a local mock server imitates the API."""
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from docsearch import agent, cloud_policy, llm, tools


class PolicyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(cloud_policy, "POLICY_FILE", Path(self.tmp.name) / "cloud_indices.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_default_is_not_allowed_and_toggle_works(self):
        self.assertFalse(cloud_policy.is_allowed("documents"))
        cloud_policy.set_allowed("documents", True)
        self.assertTrue(cloud_policy.is_allowed("documents"))
        self.assertFalse(cloud_policy.is_allowed("credentials"))
        cloud_policy.set_allowed("documents", False)
        self.assertFalse(cloud_policy.is_allowed("documents"))

    def test_client_selection_enforces_policy(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            with self.assertRaises(llm.CloudNotAllowed):
                llm.get_client("anthropic", "documents")
            cloud_policy.set_allowed("documents", True)
            self.assertTrue(llm.get_client("anthropic", "documents").cloud)
            with self.assertRaises(llm.CloudNotAllowed):  # other indices stay blocked
                llm.get_client("anthropic", "credentials")
        self.assertFalse(llm.get_client("local", "credentials").cloud)
        with self.assertRaises(llm.LlmError):
            llm.get_client("unknown", "documents")

    def test_missing_key_stops_before_anything_is_sent(self):
        cloud_policy.set_allowed("documents", True)
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}), \
                mock.patch("docsearch.config._file_cache", {}):
            with self.assertRaises(SystemExit):
                llm.get_client("anthropic", "documents")


class ConversionTest(unittest.TestCase):
    def test_messages_become_anthropic_blocks(self):
        call = {"id": "t1", "type": "function", "function": {"name": "count_documents", "arguments": '{"query": "x"}'}}
        messages = [
            {"role": "system", "content": "Rules"},
            {"role": "user", "content": "Question"},
            {"role": "assistant", "content": "", "tool_calls": [call]},
            {"role": "tool", "tool_call_id": "t1", "content": '{"documents": 3}'},
            {"role": "user", "content": "Answer now."},
        ]
        system, out = llm.to_anthropic_messages(messages)
        self.assertEqual(system, "Rules")
        self.assertEqual([m["role"] for m in out], ["user", "assistant", "user"])
        self.assertEqual(out[1]["content"], [{"type": "tool_use", "id": "t1", "name": "count_documents",
                                              "input": {"query": "x"}}])
        self.assertEqual([b["type"] for b in out[2]["content"]], ["tool_result", "text"])  # result first

    def test_orphan_tool_result_and_leading_assistant_are_dropped(self):
        messages = [{"role": "assistant", "content": "old"},
                    {"role": "tool", "tool_call_id": "gone", "content": "x"},
                    {"role": "user", "content": "Question"}]
        _, out = llm.to_anthropic_messages(messages)
        self.assertEqual(out, [{"role": "user", "content": [{"type": "text", "text": "Question"}]}])

    def test_reply_is_normalised(self):
        reply = {"content": [{"type": "text", "text": "Let me look."},
                             {"type": "tool_use", "id": "t9", "name": "group_by", "input": {"field": "extension"}}]}
        message = llm.from_anthropic(reply)
        self.assertEqual(message["content"], "Let me look.")
        self.assertEqual(message["tool_calls"][0]["function"],
                         {"name": "group_by", "arguments": '{"field": "extension"}'})

    def test_tool_schema_conversion(self):
        converted = [llm.to_anthropic_tool(t) for t in tools.TOOLS]
        self.assertTrue(all({"name", "description", "input_schema"} <= set(t) for t in converted))


class FakeEs:
    def count(self, index, query):
        return 7


class MockAnthropic(BaseHTTPRequestHandler):
    requests = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        MockAnthropic.requests.append(({k.lower(): v for k, v in self.headers.items()}, body))
        if len(MockAnthropic.requests) == 1:  # first request: ask for a tool
            reply = {"content": [{"type": "tool_use", "id": "toolu_1", "name": "count_documents",
                                  "input": {"query": "contract"}}], "stop_reason": "tool_use"}
        else:
            reply = {"content": [{"type": "text", "text": "There are 7 documents."}], "stop_reason": "end_turn"}
        data = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class AgentWithCloudTest(unittest.TestCase):
    def test_full_loop_against_mock_api(self):
        MockAnthropic.requests = []
        server = HTTPServer(("127.0.0.1", 0), MockAnthropic)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)  # runs before server_close (reverse order)
        url = f"http://127.0.0.1:{server.server_port}/v1/messages"
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}), mock.patch.object(llm, "ANTHROPIC_URL", url):
            client = llm.AnthropicClient()
            steps = []
            answer = agent.ask("How many documents mention contract?", tools.Context(FakeEs(), "x", 120000), client,
                               on_step=lambda name, args: steps.append(name))
        self.assertEqual(answer, "There are 7 documents.")
        self.assertEqual(steps, ["count_documents"])
        headers, first = MockAnthropic.requests[0]
        self.assertEqual(headers["x-api-key"], "test-key")
        self.assertEqual(headers["anthropic-version"], llm.ANTHROPIC_VERSION)
        self.assertNotIn("no_think", first["system"])  # the Qwen switch does not belong in cloud requests
        self.assertEqual(first["tools"][0]["input_schema"]["type"], "object")
        second = MockAnthropic.requests[1][1]
        self.assertEqual(second["messages"][-1]["content"][0]["type"], "tool_result")
        self.assertIn('"documents": 7', second["messages"][-1]["content"][0]["content"])


if __name__ == "__main__":
    unittest.main()
