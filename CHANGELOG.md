# Changelog

All notable changes. The version is in `docsearch/__init__.py` (`python web.py --version`).

## 0.4.0

- Answers are checked: sources quoted as `[file name]` that appear in no search result get a warning line.
- Added a version number and this changelog.

## 0.3.0

- Mailboxes (mbox) are imported, one document per mail; attachments are not indexed.
- The local services are addressed as `127.0.0.1` instead of `localhost` (on Windows `localhost` costs about 2 seconds per request).
  Existing `.env` files that still say `localhost` keep working but are slow: change them.
- Deleting an index from the window and the web interface (typed-name confirmation).

## 0.2.0

- Web interface on localhost next to the window.
- Cloud mode (Claude) with per-index approval and a warning for indices that hold keys or credentials.

## 0.1.0

- Tika to Elasticsearch indexing with resume, error log and `--limit`, question answering with a local model.
