"""Tests for the editable settings. The real .env is never touched (ENV_FILE points to a temp folder)."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from docsearch import config, preferences


class PreferencesTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.env = Path(tmp.name) / ".env"
        self.env.write_text("# my notes\nELASTIC_PASSWORD=abc\n#OCR_LANGUAGES=eng\nLLM_MODEL=old/model\n",
                            encoding="utf-8")
        saved = {f["key"]: getattr(config, f["key"]) for f in preferences.FIELDS}
        for key, value in saved.items():  # restore the running configuration afterwards
            self.addCleanup(setattr, config, key, value)
        for patcher in (mock.patch.object(config, "ENV_FILE", self.env), mock.patch.object(config, "_file_cache", None)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def lines(self):
        return self.env.read_text(encoding="utf-8").splitlines()

    def test_existing_line_is_replaced_and_everything_else_stays(self):
        preferences.save({"LLM_MODEL": "new/model-7b"})
        self.assertEqual(self.lines(), ["# my notes", "ELASTIC_PASSWORD=abc", "#OCR_LANGUAGES=eng",
                                        "LLM_MODEL=new/model-7b"])

    def test_missing_setting_is_appended_and_a_comment_is_not_replaced(self):
        preferences.save({"OCR_LANGUAGES": "deu+eng"})
        self.assertIn("#OCR_LANGUAGES=eng", self.lines())
        self.assertEqual(self.lines()[-1], "OCR_LANGUAGES=deu+eng")

    def test_values_apply_to_the_running_program(self):
        preferences.save({"LLM_THINK": True, "INDEX_WORKERS": "6", "TEXT_ANALYZER": "german"})
        self.assertIs(config.LLM_THINK, True)
        self.assertEqual((config.INDEX_WORKERS, config.TEXT_ANALYZER), (6, "german"))
        self.assertEqual(config.get("INDEX_WORKERS"), "6")  # and .env is re-read

    def test_invalid_values_are_rejected_and_nothing_is_written(self):
        before = self.env.read_text(encoding="utf-8")
        for bad in ({"INDEX_WORKERS": "0"}, {"INDEX_WORKERS": "many"}, {"TEXT_ANALYZER": "nope"},
                    {"OCR_LANGUAGES": "deu eng; rm"}, {"LLM_MODEL": "a\nELASTIC_PASSWORD=x"}, {"LLM_THINK": "yes"},
                    {"ELASTIC_PASSWORD": "x"}, {"ANTHROPIC_API_KEY": "sk-x"}, {}):
            with self.assertRaises(ValueError, msg=str(bad)):
                preferences.save(bad)
        self.assertEqual(self.env.read_text(encoding="utf-8"), before)

    def test_secrets_are_not_editable(self):
        keys = {f["key"] for f in preferences.FIELDS}
        self.assertFalse(keys & {"ELASTIC_PASSWORD", "ANTHROPIC_API_KEY"})

    def test_current_lists_every_field_with_its_value(self):
        shown = {f["key"]: f["value"] for f in preferences.current()}
        self.assertEqual(shown["LLM_MODEL"], config.LLM_MODEL)
        self.assertEqual(set(shown), {f["key"] for f in preferences.FIELDS})


if __name__ == "__main__":
    unittest.main()
