"""Check and start the Docker services (Tika, Elasticsearch)."""
import subprocess
import time
import urllib.error
import urllib.request

from . import config
from .es import Es

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def tika_version():
    try:
        with urllib.request.urlopen(config.TIKA_URL + "/version", timeout=5) as response:
            return response.read().decode().strip()
    except Exception:
        return None


def es_up():
    try:
        urllib.request.urlopen(config.ES_URL, timeout=5)
        return True
    except urllib.error.HTTPError as e:
        return e.code == 401  # without login Elasticsearch answers 401: it is running
    except Exception:
        return False


def services_up():
    return tika_version() is not None and es_up()


def ensure_running(on_status):
    """Start the Docker services if they are not running. Progress and problems go to on_status(text); never raises.

    Returns True if the services are running afterwards."""
    try:
        if services_up():
            on_status("Docker services: running.")
            return True
        on_status("Starting the Docker services (the first start downloads several GB) ...")
        start_containers()
        on_status("Docker services: running.")
        return True
    except (Exception, SystemExit) as ex:
        on_status(f"Docker services: {ex}")
        return False


def start_containers(wait_s=240):
    """Make sure .env exists, run `docker compose up -d` and wait until both services are ready.

    The first start downloads the images (several GB) and can take a while."""
    config.ensure_env()  # docker compose reads the password from .env
    try:
        result = subprocess.run(["docker", "compose", "up", "-d"], cwd=config.ROOT, capture_output=True,
                                text=True, creationflags=NO_WINDOW)
    except FileNotFoundError as e:
        raise RuntimeError("Docker was not found. Please install and start Docker Desktop.") from e
    if result.returncode != 0:
        raise RuntimeError("docker compose up -d failed: " + (result.stderr or "").strip()[-500:])
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if services_up():
            # also checks the password and waits until Elasticsearch can serve requests
            Es().call("GET", "/_cluster/health?wait_for_status=yellow&timeout=60s", timeout=90)
            return
        time.sleep(3)
    raise RuntimeError("Tika/Elasticsearch are not responding.")
