"""Index a folder (command line). The window (start.bat) does the same with a click."""
import argparse
import logging
import signal
import sys
import threading

from . import __version__, config, indexing, services
from .es import EsError


def main():
    d = indexing.Options(data_dir="")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--version", action="version", version="docsearch " + __version__)
    ap.add_argument("data_dir", nargs="?", default=config.get("DATA_DIR"), help="folder containing the documents")
    ap.add_argument("--index", default=d.index, help="index name (default: documents)")
    ap.add_argument("--recursive", action="store_true", help="include subfolders")
    ap.add_argument("--limit", type=int, help="only process N files that are not indexed yet")
    ap.add_argument("--random", action="store_true", help="random sample instead of the first N")
    ap.add_argument("--seed", type=int, default=d.seed)
    ap.add_argument("--workers", type=int, default=d.workers, help="parallel Tika requests")
    ap.add_argument("--batch-size", type=int, default=d.batch_size)
    ap.add_argument("--timeout", type=int, default=d.timeout, help="seconds per file")
    ap.add_argument("--max-chars", type=int, default=d.max_chars, help="truncate text per document")
    ap.add_argument("--keep-duplicates", dest="skip_duplicates", action="store_false",
                    help="also index files and mails whose content is already in the index")
    ap.add_argument("--max-file-mb", type=int, default=d.max_file_mb, help="skip larger files (0 = no limit)")
    ap.add_argument("--skip-failed", action="store_true", help="do not retry files that failed before")
    ap.add_argument("--dry-run", action="store_true", help="only count, write nothing")
    args = ap.parse_args()
    if not args.data_dir:
        ap.error("specify a folder (or set DATA_DIR in .env)")

    config.setup_logging("index")
    log = logging.getLogger("docsearch")
    if config.ensure_env():
        log.info("First start: %s was created with a random Elasticsearch password.", config.ENV_FILE.name)
    stop = threading.Event()
    for sig in (signal.SIGINT, getattr(signal, "SIGBREAK", None)):
        if sig is not None:
            signal.signal(sig, lambda *_: stop.set())  # finish cleanly after the running batch

    opts = indexing.Options(**{k: v for k, v in vars(args).items() if k in indexing.Options.__dataclass_fields__})
    try:
        if not opts.dry_run and not services.services_up():
            log.info("Starting the Docker services (the first start downloads several GB) ...")
            services.start_containers()
        indexing.run(opts, should_stop=stop.is_set)
    except (FileNotFoundError, RuntimeError, EsError) as e:
        log.error("%s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
