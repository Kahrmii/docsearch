# Changelog

All notable changes. The version is in `docsearch/__init__.py` (`python web.py --version`).

## 0.8.1

- Web: index overview in the Status tab and a job progress bar on every tab (also after a reload).
- Source preview: "Show full text" in the window and the web page (needed to read a single mail of an mbox file).
- README brought up to date (semantic search, order of indexing and embeddings, new options and fields).

## 0.8.0

- Semantic search (optional, per index): "Create embeddings" in the window and the web page, or
  `python index.py --index NAME --embed`. Documents are cut into chunks, embedded with a local model in LM Studio
  and stored in a nested `dense_vector` field; indices with vectors give the agent the tool `semantic_search`.
  Existing indices are extended in place (no re-indexing). The Status tab shows whether the embedding model is loaded.

## 0.7.0

- Installable with pip: `pip install git+https://github.com/Kahrmii/docsearch.git` gives the commands `docsearch`
  (window), `docsearch-web`, `docsearch-index`, `docsearch-ask` and `docsearch-health`. An installed copy keeps
  `.env`, logs, settings and conversations in `~/.docsearch` (change with `DOCSEARCH_HOME`) and copies
  `docker-compose.yml` there on the first start. The scripts in the repository (`app.py`, `web.py`, `index.py`,
  `ask.py`, `start.bat`, `start.sh`) work as before.

## 0.6.0

- Source preview: clicking a source in an answer shows the passages around the searched words (marked) in the
  window or the page; "Open file" opens the whole file as before.
- Saved conversations: every answer is stored in `conversations/` (ignored by git). Open, rename and delete them in
  the web page ("Saved conversations") or the window ("History ..."), and export the current one as Markdown with
  the cited file names. The setting "Save conversations" turns this off.

## 0.5.0

- Duplicates: a file whose content (SHA-256) is already in the index, and a mail whose Message-ID is already
  there (typical for Gmail exports: label folder plus "All Mail"), is skipped and counted.
  `python index.py --keep-duplicates` indexes them anyway. Indices created before 0.5.0 get the new
  `content_hash` field at the next run; their old documents have no hash, so only new content is compared.
- The number of pages Tika had to OCR is stored per document (`ocr_pages`) and the run log says for how many
  files OCR was needed. Tika already OCRs only pages without text; a switch for this is not possible in Tika 4.1.
- New tab "Status" (window and web) and `python -m docsearch.health`: Elasticsearch, Tika, Docker memory, indices,
  local model (is it loaded, how large is the context), cloud key (set or not, never shown).
- New tab "Settings" (window and web): model names, thinking mode, OCR languages, text analyzer, Tika workers,
  batch size and file size limit are edited there, stored in `.env` and applied at once.
  Passwords and API keys cannot be edited there.

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
