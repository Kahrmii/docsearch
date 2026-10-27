"""Tests for the keys/credentials safeguard (counting only, no Elasticsearch needed)."""
import json
import unittest

from docsearch import secrets_check


class FakeEs:
    """Answers counts depending on which kind of query it receives."""

    def __init__(self, documents=100, pem=0, keywords=0, filenames=0):
        self.answers = {"BEGIN PRIVATE KEY": pem, "aws_secret_access_key": keywords, "wildcard": filenames}
        self.documents = documents
        self.queries = []

    def count(self, index, query):
        self.queries.append(query)
        text = json.dumps(query)
        for marker, value in self.answers.items():
            if marker in text:
                return value
        return self.documents  # match_all


class SecretsCheckTest(unittest.TestCase):
    def test_clean_index_is_not_risky(self):
        findings = secrets_check.scan(FakeEs(keywords=3), "docs")
        self.assertFalse(secrets_check.risky(findings))

    def test_private_key_headers_make_it_risky(self):
        self.assertTrue(secrets_check.risky(secrets_check.scan(FakeEs(pem=1), "docs")))

    def test_credential_filenames_make_it_risky(self):
        self.assertTrue(secrets_check.risky(secrets_check.scan(FakeEs(filenames=2), "docs")))

    def test_many_secret_keywords_make_it_risky_but_a_few_do_not(self):
        self.assertFalse(secrets_check.risky(secrets_check.scan(FakeEs(keywords=9), "docs")))
        self.assertTrue(secrets_check.risky(secrets_check.scan(FakeEs(keywords=10), "docs")))

    def test_queries_are_case_insensitive_filename_wildcards_and_phrases(self):
        es = FakeEs()
        secrets_check.scan(es, "docs")
        text = json.dumps(es.queries)
        self.assertIn('"case_insensitive": true', text)
        self.assertIn("match_phrase", text)

    def test_description_names_counts_but_no_values(self):
        message = secrets_check.describe("docs", {"documents": 49277, "pem_private_keys": 4,
                                                  "credential_keywords": 120, "credential_filenames": 7})
        self.assertIn("49277 documents", message)
        self.assertIn("4 documents with private key headers", message)
        self.assertIn("7 files", message)


if __name__ == "__main__":
    unittest.main()
