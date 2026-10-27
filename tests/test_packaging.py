"""Tests for the pip package: packaged copies, first-run files in the home folder, entry points."""
import importlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from docsearch import config

REPO = Path(__file__).resolve().parent.parent


class PackagedCopiesTest(unittest.TestCase):
    def test_packaged_copies_equal_the_files_in_the_repository(self):
        """docker-compose.yml and .env.example exist twice (checkout and package): they must not drift apart."""
        for target, packaged in config.HOME_FILES.items():
            original = REPO / target
            if not original.exists():
                self.skipTest("not a checkout")
            self.assertEqual((config.PACKAGE_DATA / packaged).read_bytes(), original.read_bytes(),
                             f"copy {target} to docsearch/data/{packaged}")


class HomeFolderTest(unittest.TestCase):
    def test_missing_files_are_copied_from_the_package_and_nothing_is_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docker-compose.yml").write_text("mine", encoding="utf-8")
            with mock.patch.object(config, "ROOT", root):
                config.ensure_home()
            self.assertEqual((root / "docker-compose.yml").read_text(encoding="utf-8"), "mine")
            self.assertIn("ELASTIC_PASSWORD", (root / ".env.example").read_text(encoding="utf-8"))

    def test_first_run_creates_env_with_a_random_password_from_the_packaged_example(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(config, "ROOT", root), mock.patch.object(config, "ENV_FILE", root / ".env"), \
                    mock.patch.object(config, "ENV_EXAMPLE", root / ".env.example"), \
                    mock.patch.object(config, "_file_cache", None):
                self.assertTrue(config.ensure_env())
            text = (root / ".env").read_text(encoding="utf-8")
            self.assertNotIn(config.PASSWORD_PLACEHOLDER, text)
            self.assertTrue((root / "docker-compose.yml").exists())

    def test_home_is_the_checkout_only_if_it_has_the_compose_file(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict("os.environ", {"DOCSEARCH_HOME": tmp}):
            self.assertEqual(config._find_root(), Path(tmp).resolve())


class EntryPointTest(unittest.TestCase):
    def test_every_script_in_pyproject_points_to_a_function(self):
        try:
            import tomllib
        except ImportError:
            self.skipTest("tomllib needs Python 3.11")
        project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        entries = {**project["scripts"], **project["gui-scripts"]}
        self.assertIn("docsearch", entries)
        for name, target in entries.items():
            module, function = target.split(":")
            try:
                loaded = importlib.import_module(module)
            except ImportError as e:  # for example tkinter missing on a headless machine
                self.skipTest(f"{module}: {e}")
            self.assertTrue(callable(getattr(loaded, function)), name)


if __name__ == "__main__":
    unittest.main()
