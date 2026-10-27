"""Per-index permission: only approved indices may send text excerpts to a cloud provider.

The default is "not approved". The list is stored in cloud_indices.json (local, not part of the repository)."""
import json

from . import config

POLICY_FILE = config.ROOT / "cloud_indices.json"


def allowed_indices():
    try:
        return set(json.loads(POLICY_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return set()


def is_allowed(index):
    return index in allowed_indices()


def set_allowed(index, allowed):
    indices = allowed_indices()
    if allowed:
        indices.add(index)
    else:
        indices.discard(index)
    POLICY_FILE.write_text(json.dumps(sorted(indices)), encoding="utf-8")
