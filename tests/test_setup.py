"""Tests for the first-run setup (.env creation) without touching the real .env."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from docsearch import config


class EnsureEnvTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.env, self.example = root / ".env", root / ".env.example"
        self.example.write_text(f"# comment\nELASTIC_PASSWORD={config.PASSWORD_PLACEHOLDER}\nES_HEAP=4g\n",
                                encoding="utf-8")
        for target, value in (("ENV_FILE", self.env), ("ENV_EXAMPLE", self.example), ("_file_cache", None)):
            patcher = mock.patch.object(config, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.dict(os.environ, {"ELASTIC_PASSWORD": ""})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_creates_env_with_a_random_password(self):
        self.assertTrue(config.ensure_env())
        text = self.env.read_text(encoding="utf-8")
        self.assertNotIn(config.PASSWORD_PLACEHOLDER, text)
        self.assertIn("ES_HEAP=4g", text)  # the rest of the template is kept
        password = config.elastic_password()
        self.assertEqual(len(password), 48)

    def test_two_installations_get_different_passwords(self):
        config.ensure_env()
        first = config.elastic_password()
        self.env.unlink()
        config._file_cache = None
        config.ensure_env()
        self.assertNotEqual(first, config.elastic_password())

    def test_existing_env_is_never_touched(self):
        self.env.write_text("ELASTIC_PASSWORD=my-own-password\n", encoding="utf-8")
        self.assertFalse(config.ensure_env())
        self.assertEqual(self.env.read_text(encoding="utf-8"), "ELASTIC_PASSWORD=my-own-password\n")
        self.assertEqual(config.elastic_password(), "my-own-password")

    def test_placeholder_password_is_rejected(self):
        self.env.write_text(f"ELASTIC_PASSWORD={config.PASSWORD_PLACEHOLDER}\n", encoding="utf-8")
        with self.assertRaises(SystemExit):
            config.elastic_password()

    def test_env_file_with_bom_is_read(self):
        self.env.write_bytes("﻿ELASTIC_PASSWORD=with-bom\n".encode("utf-8"))
        self.assertEqual(config.elastic_password(), "with-bom")


if __name__ == "__main__":
    unittest.main()
