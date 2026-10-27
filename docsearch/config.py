"""Configuration from environment variables and .env. Only local addresses are allowed for services."""
import logging
import os
import secrets
import shutil
import time
from pathlib import Path
from urllib.parse import urlparse

PACKAGE_DIR = Path(__file__).resolve().parent
PACKAGE_DATA = PACKAGE_DIR / "data"  # copies of docker-compose.yml and .env.example (see HOME_FILES)


def _find_root():
    """Where .env, logs, settings and saved conversations live: the repository folder when running from a
    checkout, otherwise ~/.docsearch (installed with pip). DOCSEARCH_HOME overrides both."""
    override = os.environ.get("DOCSEARCH_HOME")
    if override:
        root = Path(override).expanduser().resolve()
    elif (PACKAGE_DIR.parent / "docker-compose.yml").exists():
        return PACKAGE_DIR.parent
    else:
        root = Path.home() / ".docsearch"
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return root


ROOT = _find_root()
ENV_FILE = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"
PASSWORD_PLACEHOLDER = "PUT_A_LONG_RANDOM_PASSWORD_HERE"
LOG_DIR = ROOT / "logs"
HOME_FILES = {"docker-compose.yml": "docker-compose.yml", ".env.example": "env.example"}  # target -> packaged copy
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


def ensure_home():
    """Make sure docker-compose.yml and .env.example exist in ROOT (installed with pip: copied from the package)."""
    for target, packaged in HOME_FILES.items():
        if not (ROOT / target).exists() and (PACKAGE_DATA / packaged).exists():
            shutil.copyfile(PACKAGE_DATA / packaged, ROOT / target)


def ensure_env():
    """First run: create .env from .env.example with a freshly generated random password.

    Returns True if the file was created. An existing .env is never touched."""
    global _file_cache
    ensure_home()
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


# 127.0.0.1 and not "localhost": on Windows "localhost" first tries IPv6 (::1) and waits about 2 seconds for the
# timeout before it falls back to IPv4, which made EVERY request take 2 seconds longer.
ES_URL = _local_url("ES_URL", "http://127.0.0.1:9200")
TIKA_URL = _local_url("TIKA_URL", "http://127.0.0.1:9998")
LLM_BASE_URL = _local_url("LLM_BASE_URL", "http://127.0.0.1:1234")
LLM_MODEL = get("LLM_MODEL", "qwen/qwen3-14b")
LLM_THINK = get("LLM_THINK", "0") == "1"  # Qwen3 thinking mode: more accurate but much slower
LLM_PROVIDER = get("LLM_PROVIDER", "local")  # "local" (LM Studio, ...) or "anthropic" (cloud)
ANTHROPIC_MODEL = get("ANTHROPIC_MODEL", "claude-sonnet-5-5")
OCR_LANGUAGES = get("OCR_LANGUAGES", "eng")  # Tesseract languages for scans, e.g. "deu+eng"
TEXT_ANALYZER = get("TEXT_ANALYZER", "standard")  # Elasticsearch analyzer for new indices, e.g. "german", "english"


def _int(name, default):
    try:
        return int(get(name, default))
    except ValueError:
        return default


INDEX_WORKERS = _int("INDEX_WORKERS", 4)          # parallel Tika requests (mind Tika's memory)
INDEX_BATCH_SIZE = _int("INDEX_BATCH_SIZE", 50)   # files per Elasticsearch write and resume check
SAVE_CONVERSATIONS = get("SAVE_CONVERSATIONS", "1") != "0"  # keep conversations in conversations/
MAX_FILE_MB = _int("MAX_FILE_MB", 100)            # larger files are skipped (0 = no limit)

# settings that can be changed while the program runs (see preferences.py): name -> conversion of the text value
_CONVERTERS = {"LLM_THINK": lambda v: v == "1", "SAVE_CONVERSATIONS": lambda v: v == "1", "INDEX_WORKERS": int, "INDEX_BATCH_SIZE": int, "MAX_FILE_MB": int}


def write_env_values(values):
    """Set KEY=value lines in .env (replace the line if there is one, otherwise append). Other lines stay as they are."""
    ensure_env()
    global _file_cache
    lines = ENV_FILE.read_text(encoding="utf-8-sig").splitlines()
    for key, value in values.items():
        new = f"{key}={value}"
        for i, line in enumerate(lines):
            if line.split("=", 1)[0].strip() == key and not line.lstrip().startswith("#") and "=" in line:
                lines[i] = new
                break
        else:
            lines.append(new)
    temp = ENV_FILE.with_name(ENV_FILE.name + ".tmp")
    temp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(temp, ENV_FILE)
    _file_cache = None


def set_values(values):
    """Save settings to .env and apply them to the running program. Values are text, already validated."""
    write_env_values(values)
    for key, value in values.items():
        globals()[key] = _CONVERTERS.get(key, str)(value)


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
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handlers = [logging.FileHandler(LOG_DIR / f"{name}_{time.strftime('%Y%m%d_%H%M%S')}.log", encoding="utf-8")]
    if console:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
