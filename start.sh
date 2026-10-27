#!/bin/sh
# Linux/macOS: open the docsearch window (creates .env and starts the Docker containers on the first run).
# "sh start.sh web" starts the web interface on localhost instead.
cd "$(dirname "$0")" || exit 1
if [ "$1" = "web" ]; then exec python3 web.py; fi
exec python3 app.py
