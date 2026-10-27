#!/bin/sh
# Linux/macOS: open the docsearch window (creates .env and starts the Docker containers on the first run)
cd "$(dirname "$0")" && exec python3 app.py
