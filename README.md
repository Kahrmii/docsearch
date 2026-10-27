# docsearch – index documents, ask questions (local model or Claude in the cloud)

Any documents (PDF, Office, ODF, RTF, emails, images with text, …) are converted to text by **Apache Tika** – scans via OCR –, indexed in **Elasticsearch**, and then queried in a chat window with a **language model acting as an agent**. The model decides on its own which tools it needs (search, count, analyse patterns, group, find files, read a document); the numbers come from Elasticsearch, not from the model.

**Two modes of operation:**
- **Local (default):** a local model (e.g. LM Studio) answers the questions. Nothing leaves your machine: all services listen on `localhost` only and the program rejects other addresses.
- **Cloud (optional):** alternatively **Claude (Anthropic)** answers the questions. In that case the **tool results (text excerpts from your documents) are sent to the Anthropic API**. This works only for indices you explicitly approve; by default every index is blocked. See [Cloud mode](#cloud-mode).

**The answer comes in the language of the question:** ask in German, get a German answer; ask in English, get an English answer – independent of the language of the documents.

Indexing, searching and counting always run locally; there is no telemetry. Python standard library only, no `pip install`.

> **Important:** Only index documents you are *allowed* to process. If they contain personal data, data protection law (e.g. GDPR) and your duty of care apply. See [Security and privacy](#security-and-privacy).

## Contents
1. [Features](#features)
2. [Cloud mode](#cloud-mode)
3. [Architecture](#architecture)
4. [Requirements](#requirements)
5. [Quick start](#quick-start)
6. [The window](#the-window)
7. [Command line](#command-line)
8. [How the agent works](#how-the-agent-works)
9. [Example questions](#example-questions)
10. [The indexer in detail](#the-indexer-in-detail)
11. [Index: fields and queries](#index-fields-and-queries)
12. [Configuration](#configuration)
13. [Performance and storage](#performance-and-storage)
14. [Operation](#operation)
15. [Security and privacy](#security-and-privacy)
16. [Troubleshooting](#troubleshooting)
17. [Project structure and tests](#project-structure-and-tests)
18. [Limitations](#limitations)
19. [License](#license)

## Features
- **Agent instead of fixed question types:** the model chooses and combines tools itself, also over several steps.
- **One window** with the tabs "Index" and "Ask": history, follow-up questions, clickable sources.
- **Resumable indexing** with progress, errors that do not stop the run, and a file size limit.
- **Local or cloud model**, switchable in the window; cloud only for indices you approved.
- **Exact numbers:** counting and ranking are done by Elasticsearch or plain code.
- **Configurable** OCR languages and text analyzer; compatible with indices created by earlier versions.

## Cloud mode

Cloud mode is **optional and off by default**. If you use it:

1. **Add the API key:** put the line `ANTHROPIC_API_KEY=…` into `.env` (key from your Anthropic account, see `.env.example`). The key lives only in `.env`, is never logged, and `.env` is listed in `.gitignore`.
2. **Approve the index:** in the window (tab "Ask") tick **"Allow cloud for this index"**, or on the command line run `python ask.py --index NAME --allow-cloud` (revoke with `--deny-cloud`). Approvals are stored in `cloud_indices.json` (local, not in the repository).
3. **Choose the model:** in the window set "Model" to **Claude (cloud)**, or run `python ask.py --provider anthropic --index NAME "question"`. The model is set by `ANTHROPIC_MODEL` (default `claude-sonnet-5-5`).

**What is sent:** your question, the conversation so far and the **tool results** (filenames, text excerpts, rankings, numbers). The documents themselves, the whole index and `.env` are not sent; searching, counting and analysing continue to run locally in Elasticsearch. Data is sent only to `https://api.anthropic.com` (hard-coded, not changeable through configuration).

**Protection against sending by accident:**
- Without an approval for the index the program aborts **before every call**; nothing is sent, and the message explains how to approve.
- Switching the index or the model ends the conversation (the history is dropped) so that excerpts of a local index cannot end up in a cloud request.
- A missing API key also aborts before anything is sent.

**What you have to decide yourself:** before approving an index, check whether you may pass the content to a service provider (data protection, confidentiality, the provider's terms). If an index contains credentials, keys or particularly sensitive data, do **not** approve it – load such collections into their own index (window: tab "Index", field "Index") that stays blocked. Providers may process and retain requests according to their terms; check them with the provider. Cloud mode incurs **costs** per token; questions that need many tool steps cost more.

## Architecture

```
 Folder with documents                         Docker (localhost only)
 ┌─────────────────┐   PUT /rmeta/text      ┌────────────────┐
 │ a.pdf  b.docx … │ ─────────────────────► │  Apache Tika   │ :9998
 └─────────────────┘   (text, OCR, meta)    │  + Tesseract   │
         ▲                                  └────────────────┘
         │ read only
 ┌─────────────────┐   _bulk / _mget        ┌────────────────┐
 │ indexing.py     │ ─────────────────────► │ Elasticsearch  │ :9200
 └─────────────────┘                        │  index(es)     │
                                            └────────────────┘
 ┌─────────────────┐   tool calls                  ▲
 │ agent.py        │ ── tools.py ──────────────────┘
 │  question→plan  │ ◄───────────────────► ┌────────────────┐
 │  → answer       │   chat + tool calling │ LLM: local     │ :1234
 └─────────────────┘                       │ or cloud       │ (optional, approved indices only)
                                           └────────────────┘
```

The indexer runs directly on the host and sends every file to Tika over HTTP. The documents therefore do **not** have to be mounted into a container and can live on any drive.

## Requirements

| What | Note |
|---|---|
| Docker Desktop (or Docker Engine + Compose v2) | for Tika and Elasticsearch |
| Python 3.9 or newer | standard library only; `tkinter` for the window (included with Windows Python) |
| RAM | at least 16 GB recommended (Elasticsearch about 6 GB, Tika about 4 GB) |
| Language model for the questions (one of the two) | **local:** OpenAI-compatible server **with tool calling**, e.g. LM Studio or Ollama, with a model that can call tools (e.g. Qwen3-14B) – **or cloud:** an Anthropic API key, see [Cloud mode](#cloud-mode) |

## Quick start

You need **Docker Desktop** (running) and **Python 3.9+**. Everything else is set up for you:

```bash
git clone https://github.com/Kahrmii/docsearch.git docsearch
cd docsearch

start.bat                   # Windows        (Linux/macOS: sh start.sh)
```

On the **first start** the program automatically
1. creates `.env` with a freshly generated **random Elasticsearch password** (an existing `.env` is never touched),
2. starts **Elasticsearch and Tika** as Docker containers with `docker compose up -d` (the first start downloads the images, several GB, and can take a few minutes – the status line at the bottom of the window shows the progress),
3. opens the window. On later starts the containers are only started if they are not running.

In the window: tab **Index** → choose a folder → **Start**. Then tab **Ask**.
For the questions a model must be loaded in your local LLM server and its local server started – or cloud mode must be set up (the only parts that are not automatic: a language model is not part of the repository).

Prefer the command line? `python index.py "D:\Archive" --recursive` and `python ask.py "…"` do the same first-time setup. If you want to start the containers yourself: `docker compose up -d` (create `.env` first with `python -c "from docsearch import config; config.ensure_env()"`).

If your documents are not English, set `OCR_LANGUAGES` (e.g. `deu+eng`) and `TEXT_ANALYZER` (e.g. `german`) in `.env` **before** the first indexing; see [Configuration](#configuration).

## The window

**Tab "Index"**
- Choose **Folder** and **Index** (default `documents`; another name creates a separate index, useful to keep collections apart). Checkbox for subfolders.
- **Start** starts the Docker containers if needed (Docker Desktop must be running) and shows a progress bar, errors, speed and remaining time.
- **Stop** ends cleanly after the running batch. **Count only** lists the number of files and their types.
- Starting the same folder with the same index again continues where it stopped.

**Tab "Ask"**
- Pick the index from the list ("Refresh" re-reads the existing indices) and type a question.
- Every **tool call** of the agent is shown in grey (`-> find_patterns(kind="email")`), the answer below it. Follow-up questions refer to the conversation so far; "New conversation" clears the history.
- **Model:** "Local (LM Studio)" or "Claude (cloud)". Cloud works only if **"Allow cloud for this index"** is ticked; otherwise nothing is sent. Switching index or model automatically starts a new conversation. The status line names the model in use.
- **Sources** in square brackets are **clickable**: a click opens the file (known document types) or shows it in the file explorer.
- **Stop** cancels a running analysis.

## Command line

```bash
# index
python index.py "D:\Archive" --recursive                       # index "documents"
python index.py "D:\Archive" --index project_a --recursive
python index.py "D:\Archive" --limit 200 --random              # sample for testing
python index.py "D:\Archive" --dry-run                         # only count

# ask
python ask.py "How many documents are there per file type?"
python ask.py --index project_a                                # interactive

# cloud (Claude): approve the index once, then ask with --provider anthropic
python ask.py --index project_a --allow-cloud                  # set approval (--deny-cloud withdraws it)
python ask.py --provider anthropic --index project_a "Which domains occur most often?"
```

Parameters of `index.py`:

| Parameter | Meaning | Default |
|---|---|---|
| `folder` | document folder (otherwise `DATA_DIR` from `.env`) | – |
| `--index NAME` | Elasticsearch index | `documents` |
| `--recursive` | include subfolders | off |
| `--limit N`, `--random`, `--seed S` | only N files not indexed yet, optionally random | all |
| `--workers N` | parallel Tika requests (mind Tika's memory) | `4` |
| `--batch-size N` | files per bulk write and resume check | `50` |
| `--timeout S` | Tika timeout per file | `300` |
| `--max-chars N` | truncate text per document (`content_truncated=true`) | `10000000` |
| `--max-file-mb N` | skip larger files and name them in the log (`0` = no limit) | `100` |
| `--skip-failed` | do not retry files that failed before | off |
| `--dry-run` | only count | off |

## How the agent works

The model receives a question and a list of tools and calls tools until it can answer (at most 8 steps). Every result is shortened to the available space budget, and the history automatically stays within the model's context.

| Tool | Purpose |
|---|---|
| `search_documents` | full-text search with hit count and text excerpts; with several phrases the places where they stand **close together** (e.g. "What did A write to B?") |
| `count_documents` | count documents matching search words, phrases or file type |
| `count_term` | "How often does X occur?": document count, literal occurrences, documents with the most hits |
| `find_patterns` | count email addresses, web addresses, domains or dates across all (matching) documents; the result is cached within the conversation |
| `group_by` | distribution by file type, author, content type, language or modification year |
| `find_files` | find documents by **filename** (or part of it); shows size and text length and reports when a file is not in the index |
| `get_document` | read a single document (excerpts around a term or from a position) |

Good to know:
- **Numbers are exact** because Elasticsearch or plain code does the counting. The model only reports them; still check important numbers in the tool call shown.
- **Answers are only as good as the excerpts found.** The model should name sources and say openly when nothing was found. Check important statements against the sources.
- The agent is instructed not to guess or invent addresses, names or numbers and to cite only filenames that appear in tool results.
- A 14-billion-parameter model occasionally picks an unsuitable tool or phrases a name too narrowly. It helps to ask more concretely, give spellings, and ask follow-up questions.
- Analysing whole archives (`find_patterns`, `count_term`) reads all matching documents and takes several minutes for tens of thousands of documents.

## Example questions

- "How many documents are there per file type?" → `group_by`
- "How often does *Jane Doe* occur?" → `count_term`
- "Which email address is mentioned most often?" → `find_patterns`
- "Which domains occur most often in Excel files?" → `find_patterns` with a file type
- "What did *Anna Example* write to *Bernd Sample*?" → `search_documents` with both names
- "Which documents mention *budget consolidation*, and what do they say?" → `search_documents`, possibly `get_document`
- "Is there a file called report_2023?" → `find_files`
- "And how many of them are PDFs?" (follow-up) → `count_documents`
- Ask in any language – the answer follows the language of your question.

## The indexer in detail

1. **File list:** `os.walk` (optionally recursive); lock files (`~$…`), `Thumbs.db`, `desktop.ini` are left out; files above `--max-file-mb` as well (noted in the log).
2. **Batches:** only `--batch-size` files are touched at a time, then written and released. Memory use stays small, even with very many files.
3. **Resume:** document ID = `<folder name>/<relative path>`. Before each batch one `_mget` checks which IDs are already in the index. An interruption costs at most one batch. The same folder name in the same index would mix; use a separate index for that.
4. **Extraction:** every file is streamed to Tika with `PUT /rmeta/text` (header `X-Tika-OCRLanguage` from `OCR_LANGUAGES`). Result: plain text and all metadata.
5. **Deliberately low parallelism (4):** Tika with OCR is memory-hungry; too many parallel jobs make the container die. More workers only make sense if Tika gets more RAM.
6. **Errors do not stop the run:** on "503" (Tika restarting after a crash) the indexer waits 15 s and 30 s and tries three times. What still fails is written to `logs/errors.jsonl` and the run log; such files are retried on the next start (unless `--skip-failed`).
7. **Progress:** `File 1234/48725 (2.5%) | ok … | errors … | skipped … | 1.0 files/s | ETA …`, also in `logs/index_<time>.log` or `logs/app_<time>.log`.

## Index: fields and queries

On the first run the index is created with 1 shard, 0 replicas and `refresh_interval: 30s`.

| Field | Type | Content |
|---|---|---|
| `filename`, `path`, `extension` | keyword | filename, full path of the original file, extension |
| `size_bytes`, `mtime` | long, date | size, modification time |
| `content` | text (analyzer from `TEXT_ANALYZER`) | extracted text |
| `content_length`, `content_truncated` | long, boolean | length, truncated or not |
| `content_type`, `author`, `created`, `page_count`, `language` | keyword/integer | selected Tika metadata |
| `indexed_at` | date | time of indexing |
| `metadata` | object (not indexed) | all Tika metadata, stored in `_source` only |

The **text** is in the index, **the original files are not** – the field `path` leads you to the file for every hit.

```bash
curl -u elastic:$ELASTIC_PASSWORD "http://localhost:9200/_cat/indices?v&bytes=mb"
```

## Configuration

Order: environment variable before `.env` before default. All address variables accept only `localhost`, `127.0.0.1` or `::1`.

| Variable | Meaning | Default |
|---|---|---|
| `ELASTIC_PASSWORD` | password of the user `elastic` (required) | – |
| `ES_URL`, `TIKA_URL` | addresses of the services | `http://localhost:9200`, `http://localhost:9998` |
| `OCR_LANGUAGES` | Tesseract languages for scans, joined with `+`, e.g. `deu+eng` | `eng` |
| `TEXT_ANALYZER` | Elasticsearch analyzer for the text of **new** indices (e.g. `standard`, `english`, `german`) | `standard` |
| `LLM_BASE_URL`, `LLM_MODEL` | local LLM server and model name | `http://localhost:1234`, `qwen/qwen3-14b` |
| `LLM_THINK` | `1` enables the thinking mode of Qwen3 (more accurate, much slower) | `0` |
| `LLM_PROVIDER` | default provider for `ask.py`: `local` or `anthropic` | `local` |
| `ANTHROPIC_API_KEY` | API key for cloud mode (only in `.env`, never in the repository) | – |
| `ANTHROPIC_MODEL` | Claude model for cloud mode | `claude-sonnet-5-5` |
| `DATA_DIR` | default folder for `python index.py` without a folder argument | – |
| `ES_HEAP`, `ES_MEM_LIMIT`, `TIKA_MEM_LIMIT`, `ES_VERSION`, `TIKA_VERSION` | for `docker compose` only | see `.env.example` |
| `ES_DATA_VOLUME`, `ES_CLUSTER_NAME` | for `docker compose` only: name of the data volume / cluster, e.g. to reuse the index of an earlier setup (use that setup's password as `ELASTIC_PASSWORD`) | `docsearch_es_data`, `docsearch-local` |

`OCR_LANGUAGES` takes effect on the next indexing run. `TEXT_ANALYZER` is applied when an index is **created**; an existing index keeps its analyzer.

## Performance and storage

A real-world example (desktop PC with 6 cores, Tika with 4 GB, 4 workers):

| | |
|---|---|
| Amount of data | about 48,700 mixed files, 26 GB (mostly PDF) |
| Duration | about 13 hours (about 1 file/s; OCR is the bottleneck) |
| Index size | about 0.7 GB (roughly 14 KB per document) |
| Errors | 5 of 48,700 files unreadable |

The index is much smaller than the source because it contains only **text and metadata**. To extrapolate: index a sample first (`--limit 200 --random`), then `store.size / docs.count × total number of files` (+ about 30 % reserve).

**Speed of the local LLM:** the model must fit **completely into video memory**. If it runs partly in system RAM the speed drops to a few tokens per second. Other GPU programs (e.g. the driver's AI chat, virtual monitors) can occupy gigabytes; in the LLM server set "Parallel" to 1, enable flash attention and set the KV cache to Q8 to save more memory. A larger context lets the agent read more text per step; the program reads the loaded context length automatically (LM Studio API).

## Operation

- **Where is the index?** In the Docker volume `docsearch_es_data` (Docker Desktop/WSL 2: in `…\AppData\Local\Docker\wsl\disk\docker_data.vhdx` on the system drive). It is not an ordinary folder.
- `docker compose stop` / `start`: the index is kept. `docker compose down`: containers are removed, the index is kept.
- ⚠ **Delete the index permanently:** `docker compose down -v` or `docker volume rm docsearch_es_data`.
- **Delete one index:** `curl -X DELETE -u elastic:$ELASTIC_PASSWORD http://localhost:9200/NAME`
- **Backup:** Elasticsearch snapshots or a volume backup. Alternatively the index can be rebuilt from the originals at any time.

## Security and privacy

- Elasticsearch runs with authentication (`xpack.security`); the password comes only from the local `.env`.
- Both container ports are bound to `127.0.0.1` in `docker-compose.yml`. **Do not change this** without putting access protection (TLS, firewall, proxy with authentication) in front; Tika has no authentication.
- The document folders are only **read**. Elasticsearch, Tika and the local language model are reached over `localhost` only; other addresses in the configuration are rejected. The **only** exception is the optional [cloud mode](#cloud-mode) with the fixed address `api.anthropic.com`, and only for approved indices.
- Clickable sources open only known document types directly; everything else is only highlighted in the explorer, never executed.
- In local mode text excerpts go only to the **local** language model. In cloud mode they leave your machine (see [Cloud mode](#cloud-mode)): approve only indices whose content you may pass to a service provider, and keep credentials, keys and particularly sensitive collections in a separate, blocked index.
- The API key belongs only in `.env`. `.env` and `cloud_indices.json` are listed in `.gitignore`; check `git status` before the first push. If a key was published by accident: revoke it in the provider account immediately and create a new one.
- Before publishing a fork: `logs/` and `gui_settings.json` are in `.gitignore` as well; logs contain filenames (possibly names of people) and do not belong in issues.
- If the documents contain personal or confidential data you are responsible for lawfulness, access protection and retention periods.

## Troubleshooting

| Symptom | Cause / solution |
|---|---|
| "Elasticsearch is not reachable" | containers are not running: `docker compose up -d`; the "Index" tab starts them itself on Start |
| "LLM server not reachable" | load a model in LM Studio/Ollama and start the local server; check `LLM_BASE_URL`/`LLM_MODEL` |
| "Index … is not approved for the cloud" | intended: tick "Allow cloud for this index" or use `--allow-cloud`; nothing was sent |
| "ANTHROPIC_API_KEY is missing" | put the key into `.env` (see `.env.example`) |
| Cloud: `LLM server 401` | key invalid or revoked |
| Cloud: `LLM server 404`/"model" | check `ANTHROPIC_MODEL`: the model must be available in your account |
| Cloud: `LLM server 429`/`529` | rate limit or overload; the program waits and tries three times, afterwards ask again a bit later |
| The agent never calls tools | the model does not support tool calling; choose another model (e.g. Qwen3) |
| `HTTP 401` from Elasticsearch | the password is set only when the data volume is *first created*. Typical causes: you changed `ELASTIC_PASSWORD` in `.env` later, or another Elasticsearch (e.g. from an earlier setup) is already running on port 9200. Enter the password used back then in `.env`, stop the other container, or recreate the volume (deletes the index) |
| `docker compose up` fails with "port is already allocated" | another container uses port 9200 or 9998 (e.g. an earlier setup): stop/remove it with `docker rm -f <name>` – the data lives in the volume and is kept, see `ES_DATA_VOLUME` |
| The first start seems to hang | the images are downloaded (several GB); watch `docker compose pull` / Docker Desktop. Docker Desktop must be running |
| `Tika HTTP 503` on single files | Tika restarts after a crashed file; the indexer waits and tries three times. If the same file keeps failing it is broken or too heavy: `--workers 2`, more `TIKA_MEM_LIMIT` or a smaller `--max-file-mb` |
| `Tika HTTP 413 Payload Too Large` | file too large for Tika; with `--max-file-mb` it is not even sent |
| Documents in the index but `content` empty | Tika 4.x delivers the text under `tk:content` (older versions `X-TIKA:content`); the indexer reads both |
| Answers extremely slow (about 5 tokens/s) | the model does not fit completely into video memory, see [Performance](#performance-and-storage) |
| Elasticsearch does not start | Linux: raise `vm.max_map_count`; Windows: check `ES_HEAP`/`ES_MEM_LIMIT` and the WSL memory limit |
| No hit although the name occurs | the spelling is searched as a phrase; "Doe, Jane" and "Jane Doe" are different. Name the variant |
| OCR finds little text | scan quality; set `OCR_LANGUAGES` to the language of the documents (the language pack must exist in the Tika image) |

## Project structure and tests

```
docsearch/
├── app.py                  main window (tabs Index / Ask)
├── start.bat / start.sh    starts app.py (Windows / Linux, macOS)
├── index.py                indexing from the command line
├── ask.py                  questions from the command line
├── docker-compose.yml      Elasticsearch + Tika (localhost only)
├── .env.example            template of the configuration (no secrets)
├── docsearch/
│   ├── config.py           .env + environment variables, local addresses only
│   ├── es.py               Elasticsearch client and index mapping
│   ├── llm.py              LLM clients: local (OpenAI-compatible) and cloud (Anthropic)
│   ├── cloud_policy.py     per-index approval for the cloud (cloud_indices.json)
│   ├── services.py         check and start the Docker services (first-run setup)
│   ├── indexing.py         pipeline file -> Tika -> Elasticsearch
│   ├── tools.py            tools of the agent (+ description for the model)
│   ├── agent.py            agent loop
│   ├── ui_common.py        settings, log forwarding
│   ├── ui_index.py         tab Index
│   └── ui_chat.py          tab Ask
└── tests/                  unit tests (no running services, cloud against a mock server)
```

```bash
python -m unittest discover -s tests -t .
```

## Limitations

- **Keyword search, not semantic search:** "termination" does not automatically find "ending of the contract". An extension with embeddings is possible but enlarges the index and the compute time a lot.
- **Attachments and archives:** embedded files (email attachments, ZIP contents) are not stored as documents of their own; only the text of the main file goes into the index.
- **Quality of the source:** scans without good OCR hits, handwriting, tables inside images, protected or damaged files yield little or no text.
- **Statements about very many documents:** "Summarise everything about X" works only approximately; the agent evaluates the best hits, not every document.
- **One Elasticsearch node** without replicas: fine for local use, no failover.
- Tested on **Windows 11** with Docker Desktop (WSL 2). On Linux/macOS `index.py`, `ask.py` and `app.py` run unchanged; `start.bat` and opening sources by click are Windows-specific.
- The date pattern of `find_patterns` recognises day.month.year (e.g. 31.12.2024) as common in European documents.

## License
MIT, see [LICENSE](LICENSE). Apache Tika, Elasticsearch and Tesseract are under their own licenses; this project does not contain their code but uses the official Docker images.
