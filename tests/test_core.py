"""Tests for the pure functions (no Elasticsearch, Tika or LLM needed): python -m unittest discover -s tests -t ."""
import os
import tempfile
import unittest

from docsearch import agent, indexing, tools


class WindowsTest(unittest.TestCase):
    def test_prefers_places_where_terms_are_close(self):
        text = "Anna " + "x " * 800 + " Anna and Bernd are talking. " + "y " * 800 + " Bernd"
        first = tools.windows(text, ["Anna", "Bernd"], width=200, limit=1)[0]
        self.assertIn("Anna and Bernd", first)

    def test_falls_back_to_start_of_text(self):
        self.assertEqual(tools.windows("Nothing found here", ["Zebra"], width=100), ["Nothing found here"])


class PatternsTest(unittest.TestCase):
    def test_email(self):
        found = tools.PATTERNS["email"].findall("Mail a.b-c@mail.example.org, or x@y.org.")
        self.assertEqual(found, ["a.b-c@mail.example.org", "x@y.org"])

    def test_url_and_date(self):
        self.assertTrue(tools.PATTERNS["url"].search("see https://example.org/a?b=1)"))
        self.assertEqual(tools.PATTERNS["date"].findall("on 3.5.2021 and 12.10.1999"), ["3.5.2021", "12.10.1999"])


class QueryTest(unittest.TestCase):
    def test_match_all(self):
        self.assertEqual(tools.build_query(), {"match_all": {}})

    def test_phrases_and_extension(self):
        q = tools.build_query("contract", ["Jane Doe"], ".PDF")["bool"]
        self.assertEqual(len(q["must"]), 2)
        self.assertEqual(q["filter"], [{"term": {"extension": "pdf"}}])


class FilenameQueryTest(unittest.TestCase):
    def test_wildcard_is_case_insensitive_and_filters_extension(self):
        q = tools.filename_query("Report", ".TXT")
        self.assertEqual(q["bool"]["must"][0]["wildcard"]["filename"],
                         {"value": "*Report*", "case_insensitive": True})
        self.assertEqual(q["bool"]["filter"], [{"term": {"extension": "txt"}}])


class ToJsonTest(unittest.TestCase):
    def test_shortens_lists_to_fit(self):
        obj = {"hits": [{"doc": "d%d" % i, "text": "x" * 200} for i in range(20)]}
        text = tools.to_json(obj, 1500)
        self.assertLessEqual(len(text), 1500)
        self.assertIn("truncated", text)


class RunToolTest(unittest.TestCase):
    def test_unknown_tool_and_bad_args(self):
        ctx = tools.Context(None, "x", 8000)
        self.assertIn("unknown tool", tools.run_tool(ctx, "does_not_exist", {}))
        self.assertIn("invalid arguments", tools.run_tool(ctx, "count_term", {"wrong": 1}))

    def test_phrases_given_as_string_are_accepted(self):
        class FakeEs:
            def count(self, index, query):
                self.query = query
                return 3

        es = FakeEs()
        result = tools.run_tool(tools.Context(es, "x", 8000), "count_documents", {"phrases": "Jane Doe"})
        self.assertIn('"documents": 3', result)
        self.assertEqual(es.query["bool"]["must"], [{"match_phrase": {"content": "Jane Doe"}}])


class TrimTest(unittest.TestCase):
    def test_shortens_old_tool_results_first(self):
        messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "question"},
                    {"role": "tool", "content": "r" * 5000}, {"role": "user", "content": "go on"}]
        trimmed = agent.trim_messages(messages, 1000)
        self.assertEqual(trimmed[2]["content"], "[result shortened]")
        self.assertEqual(trimmed[-1]["content"], "go on")


class PromptTest(unittest.TestCase):
    def test_answer_language_follows_the_question(self):
        self.assertIn("language of the user's question", agent.SYSTEM)


class FakeClient:
    """Returns prepared replies in order."""
    cloud = False

    def __init__(self, *contents):
        self.replies = [{"content": c, "tool_calls": []} for c in contents]

    def chat(self, messages, tools=None, max_tokens=1500):
        return self.replies.pop(0)


class EmptyAnswerTest(unittest.TestCase):
    def ask(self, *contents):
        return agent.ask("Question?", tools.Context(None, "x", 8000), FakeClient(*contents))

    def test_empty_answer_is_retried_once(self):
        self.assertEqual(self.ask("", "Nothing found."), "Nothing found.")

    def test_still_empty_gives_an_honest_message(self):
        self.assertIn("returned no answer", self.ask("", ""))

    def test_normal_answer_is_returned_directly(self):
        self.assertEqual(self.ask("Answer."), "Answer.")


class ScanFilesTest(unittest.TestCase):
    def test_flat_recursive_and_skips(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "sub"))
            for name in ("a.txt", "~$lock.docx", "Thumbs.db", os.path.join("sub", "b.txt")):
                with open(os.path.join(d, name), "w") as f:
                    f.write("x")
            self.assertEqual([e[0] for e in indexing.scan_files(d, False)], ["a.txt"])
            self.assertEqual([e[0] for e in indexing.scan_files(d, True)], ["a.txt", "sub/b.txt"])


if __name__ == "__main__":
    unittest.main()
