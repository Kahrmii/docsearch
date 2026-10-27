"""Configuration from environment variables and .env. Only local addresses are allowed for services."""
import logging
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"
PASSWORD_PLACEHOLDER = "PUT_A_LONG_RANDOM_PASSWORD_HERE"
LOG_DIR = ROOT / "logs"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

_file_cache = None


def _env_file():
    """Values of .env (read once; reset by ensure_env)."""
    global _file_cache
    if _file_cache is None:
        _file_cache = {}
        if ENV_FILE.exists():
            for line in ENV_FILE.read_text(encoding="utf-8-sig").splitlines():  # -sig: tolerate a BOM from Notepad
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    _file_cache[key.strip()] = value.strip().strip('"').strip("'")
    return _file_cache


def get(name, default=None):
    """Environment variable first, then .env, then the default."""
    return os.environ.get(name) or _env_file().get(name) or default


def ensure_env():
    """First run: create .env from .env.example with a freshly generated random password.

    Returns True if the file was created. An existing .env is never touched."""
    global _file_cache
    if ENV_FILE.exists():
        return False
    text = ENV_EXAMPLE.read_text(encoding="utf-8").replace(PASSWORD_PLACEHOLDER, secrets.token_hex(24))
    ENV_FILE.write_text(text, encoding="utf-8")
    _file_cache = None
    return True


def _local_url(name, default):
    url = get(name, default).rstrip("/")
    if urlparse(url).hostname not in LOCAL_HOSTS:
        raise SystemExit(f"{name}={url}: only local addresses (localhost) are allowed.")
    return url


ES_URL = _local_url("ES_URL", "http://localhost:9200")
TIKA_URL = _local_url("TIKA_URL", "http://localhost:9998")
LLM_BASE_URL = _local_url("LLM_BASE_URL", "http://localhost:1234")
LLM_MODEL = get("LLM_MODEL", "qwen/qwen3-14b")
LLM_THINK = get("LLM_THINK", "0") == "1"  # Qwen3 thinking mode: more accurate but much slower
LLM_PROVIDER = get("LLM_PROVIDER", "local")  # "local" (LM Studio, ...) or "anthropic" (cloud)
ANTHROPIC_MODEL = get("ANTHROPIC_MODEL", "claude-sonnet-5-5")
OCR_LANGUAGES = get("OCR_LANGUAGES", "eng")  # Tesseract languages for scans, e.g. "deu+eng"
TEXT_ANALYZER = get("TEXT_ANALYZER", "standard")  # Elasticsearch analyzer for new indices, e.g. "german", "english"


def elastic_password():
    password = get("ELASTIC_PASSWORD")
    if not password or password == PASSWORD_PLACEHOLDER:
        raise SystemExit(f"ELASTIC_PASSWORD is missing in {ENV_FILE.name}. Delete {ENV_FILE.name} to have a new one "
                         "generated, or set a long random password.")
    return password


def anthropic_key():
    key = get("ANTHROPIC_API_KEY")
    if not key:
        raise SystemExit(f"ANTHROPIC_API_KEY is missing. Add it to {ENV_FILE.name} (see .env.example).")
    return key


def setup_logging(name, console=True):
    """Log to logs/<name>_<time>.log and optionally to the console. Use console=False under pythonw."""
    LOG_DIR.mkdir(exist_ok=True)
    handlers = [logging.FileHandler(LOG_DIR / f"{name}_{time.strftime('%Y%m%d_%H%M%S')}.log", encoding="utf-8")]
    if console:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
