"""Settings that can be changed in the window and the web interface (they are stored in .env).

Passwords and API keys are deliberately not editable here: they stay in .env and never pass through a page."""
import os
import re

from . import config

ANALYZERS = ["standard", "simple", "whitespace", "english", "german", "french", "spanish", "italian", "dutch",
             "portuguese", "russian"]
MODEL_NAME = re.compile(r"[\w./:\-]{1,100}")
OCR_LANGUAGE_LIST = re.compile(r"[a-z_]{3,12}(\+[a-z_]{3,12})*")

FIELDS = [
    {"key": "LLM_MODEL", "label": "Local model", "kind": "text",
     "help": "Name of the model in LM Studio (as shown in its model list)."},
    {"key": "LLM_THINK", "label": "Thinking mode (Qwen3)", "kind": "bool",
     "help": "More accurate, but much slower."},
    {"key": "EMBED_MODEL", "label": "Embedding model", "kind": "text",
     "help": "Embedding model in LM Studio for the semantic search. Changing it makes existing vectors useless: "
             "use the same model for creating and asking."},
    {"key": "ANTHROPIC_MODEL", "label": "Cloud model", "kind": "text", "help": "Claude model used in cloud mode."},
    {"key": "SAVE_CONVERSATIONS", "label": "Save conversations", "kind": "bool",
     "help": "Keeps questions and answers in the conversations folder on this computer (they can contain "
             "excerpts of your documents). Turn off to keep nothing."},
    {"key": "OCR_LANGUAGES", "label": "OCR languages", "kind": "text",
     "help": "Tesseract languages joined with +, for example deu+eng. Applies to files indexed from now on."},
    {"key": "TEXT_ANALYZER", "label": "Text analyzer", "kind": "choice", "choices": ANALYZERS,
     "help": "Stemming and stop words of NEW indices; existing indices keep theirs."},
    {"key": "INDEX_WORKERS", "label": "Parallel Tika requests", "kind": "int", "min": 1, "max": 16,
     "help": "More is faster but needs more Tika memory (4 is safe with a 4 GB limit)."},
    {"key": "INDEX_BATCH_SIZE", "label": "Files per batch", "kind": "int", "min": 10, "max": 500,
     "help": "Files written to the index at once."},
    {"key": "MAX_FILE_MB", "label": "Skip files larger than (MB)", "kind": "int", "min": 0, "max": 5000,
     "help": "0 = no limit."},
]
BY_KEY = {f["key"]: f for f in FIELDS}


def current():
    """The fields with their current values; `env_override` marks values an environment variable fixes."""
    result = []
    for f in FIELDS:
        value = getattr(config, f["key"])
        result.append({**f, "value": value, "env_override": f["key"] in os.environ})
    return result


def validate(changes):
    """{key: text to store}. Raises ValueError with a message for the user if something is not acceptable."""
    if not isinstance(changes, dict) or not changes:
        raise ValueError("Nothing to save.")
    clean = {}
    for key, value in changes.items():
        field = BY_KEY.get(key)
        if field is None:
            raise ValueError(f"'{key}' cannot be changed here.")
        label = field["label"]
        if field["kind"] == "bool":
            if not isinstance(value, bool):
                raise ValueError(f"{label}: yes or no.")
            clean[key] = "1" if value else "0"
        elif field["kind"] == "int":
            try:
                number = int(str(value).strip())
            except ValueError:
                raise ValueError(f"{label}: a whole number is needed.") from None
            if not field["min"] <= number <= field["max"]:
                raise ValueError(f"{label}: between {field['min']} and {field['max']}.")
            clean[key] = str(number)
        elif field["kind"] == "choice":
            if value not in field["choices"]:
                raise ValueError(f"{label}: one of {', '.join(field['choices'])}.")
            clean[key] = value
        else:
            text = str(value).strip()
            pattern = OCR_LANGUAGE_LIST if key == "OCR_LANGUAGES" else MODEL_NAME
            if not pattern.fullmatch(text):
                raise ValueError(f"{label}: '{text[:40]}' is not a valid value.")
            clean[key] = text
    return clean


def save(changes):
    """Validate, store in .env and apply right away. Returns the stored values."""
    clean = validate(changes)
    config.set_values(clean)
    return clean
