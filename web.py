#!/usr/bin/env python3
"""Web interface on localhost: the same functions as the window (app.py), in the browser.

  python web.py                 start the server and open the page
  python web.py --port 9000     other port        python web.py --no-browser   do not open the browser

On the first start it creates .env with a random password and starts the Docker containers.
The address contains a random token; only this address works. Stop with Ctrl+C."""
import argparse
import webbrowser

from docsearch import config, webserver


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765, help="port on 127.0.0.1 (default 8765, next free one if busy)")
    ap.add_argument("--no-browser", action="store_true", help="only print the address")
    args = ap.parse_args()

    config.setup_logging("web")
    state = webserver.WebState()
    webserver.start_services_in_background(state)
    server = webserver.make_server(state, args.port)
    url = f"http://127.0.0.1:{server.server_address[1]}/?token={state.token}"
    print(f"docsearch web interface: {url}\n(only reachable from this computer; stop with Ctrl+C)")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
