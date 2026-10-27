"""Tests for saved conversations, export and the source preview (fake Elasticsearch, fake model)."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from docsearch import chat, config, conversations, llm


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(conversations, "CONV_DIR", Path(tmp.name) / "conversations")
        patcher.start()
        self.addCleanup(patcher.stop)


class StoreTest(StoreTestCase):
    def conv(self, title="Contracts"):
        return {"id": conversations.new_id(), "title": title, "index": "documents", "provider": "local",
                "turns": [{"question": "Who signed?", "answer": "Anna [a.pdf].", "sources": ["a.pdf"]}],
                "sources": {"a.pdf": "C:/docs/a.pdf"}}

    def test_save_load_list_rename_delete(self):
        conv = self.conv()
        conversations.save(conv)
        self.assertEqual(conversations.load(conv["id"])["turns"][0]["question"], "Who signed?")
        self.assertEqual([c["title"] for c in conversations.listing()], ["Contracts"])
        self.assertTrue(conversations.rename(conv["id"], "  Signatures  "))
        self.assertEqual(conversations.listing()[0]["title"], "Signatures")
        self.assertTrue(conversations.delete(conv["id"]))
        self.assertEqual(conversations.listing(), [])
        self.assertFalse(conversations.delete(conv["id"]))

    def test_ids_are_checked_so_no_other_file_can_be_reached(self):
        for bad in ("../x", "a" * 31, "A" * 32, "", None, r"..\..\evil"):
            with self.assertRaises(ValueError):
                conversations._path(bad)
            self.assertFalse(conversations.delete(bad))
        self.assertIsNone(conversations.load("not-an-id"))

    def test_damaged_files_are_ignored(self):
        conversations.CONV_DIR.mkdir(parents=True)
        (conversations.CONV_DIR / ("b" * 32 + ".json")).write_text("{broken", encoding="utf-8")
        self.assertEqual(conversations.listing(), [])

    def test_markdown_has_questions_answers_and_file_names_but_no_paths(self):
        text = conversations.to_markdown(self.conv())
        self.assertIn("# Contracts", text)
        self.assertIn("## 1. Who signed?", text)
        self.assertIn("Sources: `a.pdf`", text)
        self.assertNotIn("C:/docs", text)

    def test_long_titles_are_shortened(self):
        self.assertLessEqual(len(conversations.default_title("word " * 100)), conversations.TITLE_LENGTH)


class FakeEs:
    def __init__(self, content="Intro. " * 50 + "Anna signed the contract here. " + "More. " * 50):
        self.content, self.queries = content, []

    def search(self, index, body):
        self.queries.append((index, body))
        return {"hits": {"hits": [{"_source": {"content": self.content}}]}}


class SessionTest(StoreTestCase):
    def make(self, answer="Anna [a.pdf] signed."):
        self.es = FakeEs()
        session = chat.ChatSession(lambda: self.es)

        class Client:
            cloud = False

            def label(self):
                return "fake"

            def context_chars(self):
                return 8000

            def chat(self, messages, tools=None, max_tokens=1500, temperature=0.1):
                return {"content": answer, "tool_calls": []}

        patcher = mock.patch.object(llm, "get_client", return_value=Client())
        patcher.start()
        self.addCleanup(patcher.stop)
        return session

    def ask(self, session, question="Who signed?"):
        events = []
        session.sources["a.pdf"] = "C:/docs/a.pdf"
        session.run(question, "documents", "local", lambda kind, value: events.append((kind, value)))
        return events

    def test_every_answer_is_saved_and_can_be_continued(self):
        session = self.make()
        self.ask(session)
        saved = conversations.listing()
        self.assertEqual((len(saved), saved[0]["questions"], saved[0]["title"]), (1, 1, "Who signed?"))
        self.assertEqual(conversations.load(saved[0]["id"])["turns"][0]["sources"], ["a.pdf"])
        other = self.make()
        other.restore(conversations.load(saved[0]["id"]))
        self.assertEqual([m["role"] for m in other.history], ["user", "assistant"])
        self.ask(other, "And then?")
        self.assertEqual(len(conversations.listing()), 1)  # the same file, now with two questions
        self.assertEqual(conversations.listing()[0]["questions"], 2)

    def test_restored_conversation_forgets_its_history_for_another_target(self):
        session = self.make()
        self.ask(session)
        other = self.make()
        other.restore(conversations.load(conversations.listing()[0]["id"]))
        self.assertTrue(other.switch_target("keys", "anthropic"))
        self.assertEqual(other.history, [])

    def test_nothing_is_saved_when_switched_off(self):
        session = self.make()
        with mock.patch.object(config, "SAVE_CONVERSATIONS", False):
            self.ask(session)
        self.assertEqual(conversations.listing(), [])

    def test_new_conversation_starts_a_new_file(self):
        session = self.make()
        self.ask(session)
        session.reset()
        self.ask(session, "Another question")
        self.assertEqual(len(conversations.listing()), 2)

    def test_a_save_error_does_not_lose_the_answer(self):
        session = self.make()
        with mock.patch.object(conversations, "save", side_effect=OSError("disk full")):
            events = self.ask(session)
        self.assertEqual(events[-1][0], "answer")

    def test_citation_helper_only_returns_known_sources(self):
        self.assertEqual(chat.cited_sources("[a.pdf, ghost.pdf] and [a.pdf]; [x]", {"a.pdf": 1}), ["a.pdf"])


class PreviewTest(SessionTest):
    def test_preview_shows_the_passage_around_the_searched_words(self):
        session = self.make()
        self.ask(session)
        session._note_terms({"phrases": ["Anna"], "query": "contract signed"})
        preview = session.preview("a.pdf")
        self.assertIn("Anna signed the contract", preview["passages"][0])
        self.assertIn("Anna", preview["terms"])
        index, body = self.es.queries[-1]
        self.assertEqual(index, "documents")
        self.assertEqual(body["query"]["bool"]["filter"][1], {"term": {"path": "C:/docs/a.pdf"}})

    def test_unknown_files_have_no_preview_and_cause_no_query(self):
        session = self.make()
        self.ask(session)
        self.assertIsNone(session.preview("secret.txt"))
        self.assertEqual(len(self.es.queries), 0)


if __name__ == "__main__":
    unittest.main()
