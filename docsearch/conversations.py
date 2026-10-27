"""Saved conversations: one JSON file each in conversations/ (ignored by git), so they survive closing the program
and can be exported as Markdown. They hold the questions, the answers and the names of the cited files."""
import datetime as dt
import json
import os
import re
import uuid

from . import config

CONV_DIR = config.ROOT / "conversations"
ID_PATTERN = re.compile(r"[0-9a-f]{32}")
TITLE_LENGTH = 70


def new_id():
    return uuid.uuid4().hex


def default_title(question):
    one_line = " ".join(question.split())
    return one_line if len(one_line) <= TITLE_LENGTH else one_line[:TITLE_LENGTH - 1] + "…"


def _path(conv_id):
    if not isinstance(conv_id, str) or not ID_PATTERN.fullmatch(conv_id):
        raise ValueError("Invalid conversation id.")
    return CONV_DIR / f"{conv_id}.json"


def save(conv):
    """Write the conversation (atomically, so a crash cannot leave half a file)."""
    path = _path(conv["id"])
    conv["updated"] = dt.datetime.now().isoformat(timespec="seconds")
    CONV_DIR.mkdir(exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(conv, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, path)


def load(conv_id):
    """The conversation, or None if it does not exist or is unreadable."""
    try:
        conv = json.loads(_path(conv_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return conv if isinstance(conv, dict) and isinstance(conv.get("turns"), list) else None


def listing():
    """[{id, title, index, provider, updated, questions}], newest first."""
    result = []
    for path in CONV_DIR.glob("*.json") if CONV_DIR.is_dir() else []:
        conv = load(path.stem)
        if conv:
            result.append({"id": conv.get("id", path.stem), "title": conv.get("title") or "(untitled)",
                           "index": conv.get("index"), "provider": conv.get("provider"),
                           "updated": conv.get("updated", ""), "questions": len(conv["turns"])})
    return sorted(result, key=lambda c: c["updated"], reverse=True)


def rename(conv_id, title):
    conv = load(conv_id)
    title = " ".join(str(title).split())[:200]
    if conv is None or not title:
        return False
    conv["title"] = title
    save(conv)
    return True


def delete(conv_id):
    try:
        _path(conv_id).unlink()
        return True
    except (OSError, ValueError):  # missing file or an id that is not one of ours
        return False


def to_markdown(conv):
    """The conversation as Markdown: questions, answers and the file names they cite (no paths)."""
    lines = [f"# {conv.get('title') or 'Conversation'}", "",
             f"Index: {conv.get('index')} | Model: {conv.get('provider')} | Saved: {conv.get('updated', '')}", ""]
    for n, turn in enumerate(conv["turns"], 1):
        lines += [f"## {n}. {turn['question']}", "", turn["answer"], ""]
        if turn.get("sources"):
            lines += ["Sources: " + ", ".join(f"`{name}`" for name in turn["sources"]), ""]
    return "\n".join(lines)
