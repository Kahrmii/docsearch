"""Helpers shared by the window tabs."""
import json
import logging

from . import config

SETTINGS_FILE = config.ROOT / "gui_settings.json"


class Settings(dict):
    """Last used inputs (folder, index, ...), stored in gui_settings.json."""

    def __init__(self):
        super().__init__()
        try:
            self.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass

    def save(self):
        try:
            SETTINGS_FILE.write_text(json.dumps(self), encoding="utf-8")
        except OSError:
            pass


class QueueHandler(logging.Handler):
    """Forwards log lines into a queue so the window can display them."""

    def __init__(self, q):
        super().__init__()
        self.q = q
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))

    def emit(self, record):
        self.q.put(("log", self.format(record)))
