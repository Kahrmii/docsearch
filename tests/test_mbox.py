"""Tests for mbox mailboxes: parser, document fields and the indexing run (fake Elasticsearch, no Tika needed)."""
import base64
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from docsearch import config, indexing, mbox, services

PLAIN = b"""From MAILER-DAEMON Mon May  3 10:00:00 2021
From: Anna Beispiel <Anna@Example.org>
To: bernd@example.org, Carla <carla@example.org>
Cc: dora@example.org
Subject: =?utf-8?q?K=C3=BCndigung?= Liefervertrag
Date: Mon, 3 May 2021 10:00:00 +0200
Message-ID: <1@example.org>
Content-Type: text/plain; charset=utf-8

Hallo Bernd,
ich k\xc3\xbcndige den Liefervertrag.
From the desk of Anna: this line must not split the message.
>From here on the line was quoted by mbox.
"""
HTML_ONLY = b"""From - Tue May  4 09:30:00 2021
From: news@example.org
To: anna@example.org
Subject: Newsletter
Date: Tue, 4 May 2021 09:30:00 +0000
Content-Type: text/html; charset=utf-8

<html><head><title>hidden</title><style>p {color:red}</style></head><body><h1>Neuigkeiten</h1><p>Rabatt &amp; mehr</p>
<script>alert(1)</script>Zeile<br>zwei</body></html>
"""
ALTERNATIVE = b"""From 123@xxx Wed May  5 08:00:00 2021
From: a@example.org
To: b@example.org
Subject: Both parts
Date: Wed, 5 May 2021 08:00:00 +0000
MIME-Version: 1.0
Content-Type: multipart/alternative; boundary="B"

--B
Content-Type: text/plain; charset=utf-8

PLAINTEXT-PART
--B
Content-Type: text/html; charset=utf-8

<p>HTML-PART</p>
--B--
"""
WITH_ATTACHMENT = ("""From x@y Thu May  6 08:00:00 2021
From: a@example.org
To: b@example.org
Subject: Contract attached
Date: Thu, 6 May 2021 08:00:00 +0000
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="M"

--M
Content-Type: text/plain; charset=utf-8

See the attachment.
--M
Content-Type: application/pdf; name="contract.pdf"
Content-Disposition: attachment; filename="contract.pdf"
Content-Transfer-Encoding: base64

""" + base64.b64encode(b"%PDF-1.4 SECRETPAYLOAD " * 5).decode() + """
--M--
""").encode()
BROKEN = b"""From nobody Fri May  7 08:00:00 2021
From: weird
Date: not a date at all

Body without subject.
"""
SAMPLE = b"\n".join([PLAIN, HTML_ONLY, ALTERNATIVE, WITH_ATTACHMENT, BROKEN])


class MboxTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.box = self.dir / "box.mbox"
        self.box.write_bytes(SAMPLE)

    def doc(self, number):
        raw = dict(mbox.iter_messages(str(self.box)))[number]
        return mbox.build_mail_doc(raw, number, str(self.box), 1_600_000_000, 1_000_000)


class SplitTest(MboxTestCase):
    def test_messages_are_split_at_separator_lines_only(self):
        numbers = [n for n, _ in mbox.iter_messages(str(self.box))]
        self.assertEqual(numbers, [1, 2, 3, 4, 5])
        self.assertEqual(mbox.count_messages(str(self.box)), 5)
        self.assertIn(b"From the desk of Anna", dict(mbox.iter_messages(str(self.box)))[1])  # not a separator

    def test_mboxrd_quoting_is_removed(self):
        self.assertIn(b"\nFrom here on the line was quoted", dict(mbox.iter_messages(str(self.box)))[1])

    def test_detection(self):
        self.assertTrue(mbox.looks_like_mbox(str(self.box)))
        empty = self.dir / "empty.mbox"
        empty.write_bytes(b"")
        self.assertFalse(mbox.looks_like_mbox(str(empty)))
        thunderbird = self.dir / "Inbox"  # no extension
        thunderbird.write_bytes(HTML_ONLY)
        self.assertTrue(mbox.looks_like_mbox(str(thunderbird)))
        plain = self.dir / "Notes"
        plain.write_bytes(b"just some text\n")
        self.assertFalse(mbox.looks_like_mbox(str(plain)))
        other = self.dir / "mail.txt"  # starts like a mailbox, but the extension says it is not one
        other.write_bytes(HTML_ONLY)
        self.assertFalse(mbox.looks_like_mbox(str(other)))

    def test_the_mailbox_is_only_read(self):
        before = os.stat(self.box)
        list(mbox.iter_messages(str(self.box)))
        self.assertEqual(self.box.read_bytes(), SAMPLE)
        self.assertEqual(os.stat(self.box).st_mtime, before.st_mtime)


class DocumentTest(MboxTestCase):
    def test_header_fields_and_text(self):
        doc = self.doc(1)
        self.assertEqual(doc["filename"], "box.mbox#1")
        self.assertEqual(doc["extension"], "mail")
        self.assertEqual(doc["mail_from"], "anna@example.org")
        self.assertEqual(doc["mail_to"], ["bernd@example.org", "carla@example.org"])
        self.assertEqual(doc["mail_cc"], ["dora@example.org"])
        self.assertEqual(doc["mail_subject"], "Kündigung Liefervertrag")  # RFC 2047 decoded
        self.assertEqual(doc["mail_date"], "2021-05-03T08:00:00+00:00")  # UTC
        self.assertEqual(doc["mtime"], doc["mail_date"])
        self.assertEqual(doc["author"], "anna@example.org")
        self.assertEqual(doc["mail_message_id"], "<1@example.org>")
        self.assertTrue(doc["content"].startswith("Subject: Kündigung Liefervertrag\nFrom: Anna Beispiel <anna@example.org>"))
        self.assertIn("Cc: dora@example.org", doc["content"])
        self.assertIn("ich kündige den Liefervertrag", doc["content"])

    def test_path_points_to_the_mailbox_with_forward_slashes(self):
        self.assertEqual(self.doc(1)["path"], str(self.box).replace("\\", "/"))

    def test_html_only_mail_becomes_text(self):
        content = self.doc(2)["content"]
        self.assertIn("Neuigkeiten", content)
        self.assertIn("Rabatt & mehr", content)
        self.assertIn("Zeile\nzwei", content)
        for hidden in ("alert(1)", "color:red", "hidden", "<p>"):
            self.assertNotIn(hidden, content)

    def test_plain_part_is_preferred_over_html(self):
        content = self.doc(3)["content"]
        self.assertIn("PLAINTEXT-PART", content)
        self.assertNotIn("HTML-PART", content)

    def test_attachments_are_not_indexed_but_recorded(self):
        doc = self.doc(4)
        self.assertEqual((doc["mail_attachments"], doc["mail_attachment_names"]), (1, ["contract.pdf"]))
        self.assertIn("See the attachment.", doc["content"])
        self.assertIn("Attachments (not indexed): contract.pdf", doc["content"])
        self.assertNotIn("SECRETPAYLOAD", doc["content"])
        self.assertNotIn("JVBERi0", doc["content"])  # base64 of "%PDF-"

    def test_broken_mail_still_gets_a_document(self):
        doc = self.doc(5)
        self.assertEqual(doc["mail_subject"], "(no subject)")
        self.assertIsNone(doc["mail_date"])
        self.assertTrue(doc["mtime"].startswith("2020-09-13"))  # falls back to the file time
        self.assertIn("Body without subject.", doc["content"])

    def test_long_text_is_truncated(self):
        raw = dict(mbox.iter_messages(str(self.box)))[1]
        doc = mbox.build_mail_doc(raw, 1, str(self.box), 0, 50)
        self.assertEqual((len(doc["content"]), doc["content_truncated"]), (50, True))

    def test_mail_dates_outside_a_sane_range_are_dropped(self):
        raw = b"From: a@b.org\nDate: Thu, 1 Jan 1970 00:00:00 +0000\n\nold\n"
        self.assertIsNone(mbox.build_mail_doc(raw, 1, str(self.box), 0, 100)["mail_date"])


class FakeEs:
    def __init__(self):
        self.docs, self.mail_mapping = {}, 0

    def call(self, method, path, body=None, **kwargs):
        return {"status": "green"}

    def ensure_index(self, index):
        return True

    def ensure_mail_fields(self, index):
        self.mail_mapping += 1

    def existing_ids(self, index, ids):
        return {i for i in ids if i in self.docs}

    def existing_hashes(self, index, hashes):
        stored = {d.get("content_hash") for d in self.docs.values()}
        return {h for h in hashes if h in stored}

    def bulk(self, index, docs):
        self.docs.update(dict(docs))
        return {}


class IndexingRunTest(MboxTestCase):
    def setUp(self):
        super().setUp()
        (self.dir / "note.txt").write_text("a normal file", encoding="utf-8")
        self.es = FakeEs()
        for patcher in (mock.patch.object(indexing, "Es", return_value=self.es),
                        mock.patch.object(services, "tika_version", return_value="test"),
                        mock.patch.object(indexing, "build_doc", lambda rel, *a: {"content": "note"}),
                        mock.patch.object(indexing, "ERRORS_FILE", self.dir / "errors.jsonl"),
                        mock.patch.object(config, "LOG_DIR", self.dir)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.progress = []

    def run_index(self, **options):
        opts = indexing.Options(data_dir=str(self.dir), index="mails", **options)
        return indexing.run(opts, on_progress=self.progress.append)

    def test_mails_and_normal_files_are_indexed_together(self):
        stats = self.run_index()
        prefix = self.dir.name
        self.assertEqual(sorted(self.es.docs), sorted([f"{prefix}/note.txt"] + [f"{prefix}/box.mbox#{n}" for n in range(1, 6)]))
        self.assertEqual((stats["ok"], stats["errors"], stats["skipped"]), (6, 0, 0))
        self.assertEqual(self.es.mail_mapping, 1)  # mail fields added once
        self.assertEqual((self.progress[-1]["done"], self.progress[-1]["total"]), (6, 6))
        self.assertEqual(self.es.docs[f"{prefix}/box.mbox#1"]["mail_from"], "anna@example.org")

    def test_a_second_run_skips_everything_that_is_indexed(self):
        self.run_index()
        stats = self.run_index()
        self.assertEqual((stats["ok"], stats["skipped"], len(self.es.docs)), (0, 6, 6))

    def test_resume_after_an_interruption_adds_only_the_missing_mails(self):
        self.run_index()
        del self.es.docs[f"{self.dir.name}/box.mbox#3"]
        stats = self.run_index()
        self.assertEqual((stats["ok"], stats["skipped"], len(self.es.docs)), (1, 5, 6))

    def test_small_batches_give_the_same_result(self):
        self.run_index(batch_size=2)
        self.assertEqual(len(self.es.docs), 6)

    def test_the_same_mails_in_a_second_mailbox_are_duplicates(self):
        (self.dir / "all_mail.mbox").write_bytes(SAMPLE)
        stats = self.run_index()
        self.assertEqual(stats["duplicates"], 5)
        self.assertEqual(len(self.es.docs), 6)  # one copy of every mail + the note

    def test_duplicates_can_be_kept(self):
        (self.dir / "all_mail.mbox").write_bytes(SAMPLE)
        stats = self.run_index(skip_duplicates=False)
        self.assertEqual((stats["duplicates"], len(self.es.docs)), (0, 11))

    def test_mail_hash_follows_the_message_id(self):
        self.assertEqual(self.doc(1)["content_hash"], self.doc(1)["content_hash"])
        self.assertNotEqual(self.doc(1)["content_hash"], self.doc(2)["content_hash"])

    def test_limit_counts_files_and_mails(self):
        stats = self.run_index(limit=3)
        self.assertEqual((stats["ok"], len(self.es.docs)), (3, 3))

    def test_a_broken_message_is_logged_and_the_rest_is_indexed(self):
        real = mbox.build_mail_doc

        def flaky(raw, number, *args):
            if number == 2:
                raise ValueError("boom")
            return real(raw, number, *args)

        with mock.patch.object(mbox, "build_mail_doc", flaky):
            stats = self.run_index()
        self.assertEqual((stats["ok"], stats["errors"]), (5, 1))
        self.assertIn("box.mbox#2", (self.dir / "errors.jsonl").read_text(encoding="utf-8"))
        self.assertNotIn(f"{self.dir.name}/box.mbox#2", self.es.docs)

    def test_stop_request_ends_the_mail_phase(self):
        stats = indexing.run(indexing.Options(data_dir=str(self.dir), index="mails", batch_size=2),
                             should_stop=lambda: True)
        self.assertTrue(stats["stopped"])
        self.assertEqual(self.es.docs, {})

    def test_mailboxes_are_not_subject_to_the_file_size_limit(self):
        with open(self.box, "ab") as f:  # a mailbox of more than 1 MB ...
            f.write(b"\nFrom big Sat May  8 08:00:00 2021\nFrom: a@b.org\nSubject: big\n\n" + b"x" * 1_200_000 + b"\n")
        (self.dir / "big.txt").write_bytes(b"y" * 1_500_000)  # ... and a normal file of the same size
        self.run_index(max_file_mb=1)
        prefix = self.dir.name
        self.assertEqual(len([d for d in self.es.docs if "#" in d]), 6)  # all mails are indexed (streamed)
        self.assertNotIn(f"{prefix}/big.txt", self.es.docs)  # the normal file is skipped by the limit

    def test_dry_run_reports_message_counts(self):
        with self.assertLogs("docsearch.indexing", level="INFO") as logs:
            indexing.run(indexing.Options(data_dir=str(self.dir), index="mails", dry_run=True))
        self.assertTrue(any("mailbox box.mbox: 5 messages" in line for line in logs.output))
        self.assertEqual(self.es.docs, {})


if __name__ == "__main__":
    unittest.main()
