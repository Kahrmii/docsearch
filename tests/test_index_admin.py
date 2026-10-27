"""Tests for deleting an index (no Elasticsearch needed: a fake records what would be deleted)."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from docsearch import cloud_policy, index_admin
from docsearch.es import Es, valid_index_name


class FakeEs:
    def __init__(self, indices=None):
        self.existing = indices if indices is not None else {"documents": {"documents": 49277, "size_mb": 723}}
        self.deleted = []

    def index_info(self, index):
        return self.existing.get(index)

    def delete_index(self, index):
        self.deleted.append(index)


class IndexNameTest(unittest.TestCase):
    def test_plain_names_are_valid(self):
        for name in ("documents", "my-index_2", "a1"):
            self.assertTrue(valid_index_name(name), name)

    def test_wildcards_lists_system_and_odd_names_are_refused(self):
        for name in ("*", "doc*", "_all", ".kibana", "a,b", "Docs", "", "-x", "a b", "../x", "a/b"):
            self.assertFalse(valid_index_name(name), repr(name))

    def test_the_client_itself_refuses_wildcards(self):
        for name in ("*", "_all", "a,b", ".security"):
            with self.assertRaises(ValueError):
                Es(password="x").delete_index(name)  # raises before any request is made


class DeleteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(cloud_policy, "POLICY_FILE", Path(self.tmp.name) / "cloud_indices.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.es = FakeEs()

    def test_describe_reports_documents_size_and_cloud_approval(self):
        info = index_admin.describe(self.es, "documents")
        self.assertEqual((info["documents"], info["size_mb"], info["cloud_allowed"]), (49277, 723, False))
        cloud_policy.set_allowed("documents", True)
        self.assertTrue(index_admin.describe(self.es, "documents")["cloud_allowed"])

    def test_describe_rejects_invalid_and_unknown_names(self):
        with self.assertRaises(ValueError):
            index_admin.describe(self.es, "*")
        with self.assertRaises(index_admin.IndexNotFound):
            index_admin.describe(self.es, "missing")

    def test_warning_is_explicit(self):
        text = index_admin.warning_text("documents", {"documents": 49277, "size_mb": 723, "cloud_allowed": True})
        for expected in ("Permanently delete the index 'documents'", "49277 documents", "723 MB",
                         "cannot be undone", "original files", "approval for the cloud"):
            self.assertIn(expected, text)
        self.assertNotIn("approval for the cloud",
                         index_admin.warning_text("documents", {"documents": 1, "size_mb": 1, "cloud_allowed": False}))

    def test_a_wrong_or_missing_confirmation_deletes_nothing(self):
        for typed in ("", "document", "Documents", " documents"):
            with self.assertRaises(ValueError):
                index_admin.delete(self.es, "documents", typed)
        self.assertEqual(self.es.deleted, [])

    def test_confirmed_delete_removes_the_index_and_its_cloud_approval(self):
        cloud_policy.set_allowed("documents", True)
        info = index_admin.delete(self.es, "documents", "documents")
        self.assertEqual(self.es.deleted, ["documents"])
        self.assertEqual(info["documents"], 49277)
        self.assertFalse(cloud_policy.is_allowed("documents"))

    def test_deleting_an_unknown_index_does_not_call_delete(self):
        with self.assertRaises(index_admin.IndexNotFound):
            index_admin.delete(self.es, "missing", "missing")
        self.assertEqual(self.es.deleted, [])

    def test_a_wildcard_is_never_deleted_even_when_confirmed(self):
        with self.assertRaises(ValueError):
            index_admin.delete(self.es, "*", "*")
        self.assertEqual(self.es.deleted, [])


if __name__ == "__main__":
    unittest.main()
