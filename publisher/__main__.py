"""Run the publisher.

  python3 -m publisher                 serve the web UI and keep live decks in sync
  python3 -m publisher status          list decks, their status and mode
  python3 -m publisher publish ID...   publish decks (new ones start live) or push edits now
  python3 -m publisher unpublish ID... remove decks from the public site
  python3 -m publisher pause ID...     freeze the public copy of decks
  python3 -m publisher resume ID...    let decks follow the editor again
  python3 -m publisher sync            push every live deck's pending edits now
  python3 -m publisher rebuild         rebuild the public site from snapshots

Configuration comes from environment variables:

  STAGEDOOR_WORKSPACE   path to the live open-slide workspace (required)
  STAGEDOOR_DATA_DIR    where snapshots and builds are kept (required)
  STAGEDOOR_LISTEN      host:port to bind (default 127.0.0.1:8090)
  STAGEDOOR_BASE_PATH   URL prefix of the UI (default /publish)
  STAGEDOOR_PUBLIC_URL  public deck URL template, e.g. https://slides.example.com/s/{id}
  STAGEDOOR_BUILD_CMD   build command run in the stage dir; {out} is the output dir
                        (default: npx --no-install open-slide build --out-dir {out})
  STAGEDOOR_SYNC_DELAY  seconds a live deck must sit unedited before it syncs (default 60)
"""

import os
import sys
from pathlib import Path

from . import sync
from .server import App, serve
from .store import PublishError, Store

DEFAULT_BUILD = "npx --no-install open-slide build --out-dir {out}"
USAGE = __doc__.split("\n\n")[1]
DONE = {"publish": "published", "unpublish": "unpublished", "pause": "paused", "resume": "resumed"}


def run_command(store: Store, args: list[str]) -> int:
    command, ids = args[0], args[1:]
    try:
        if command == "status" and not ids:
            for deck in store.decks():
                when = f"  (published {deck.published_at})" if deck.published_at else ""
                print(f"{deck.status:<10} {deck.mode or '-':<7} {deck.id}{when}")
        elif command in DONE and ids:
            for deck_id in ids:
                getattr(store, command)(deck_id)
                print(f"{DONE[command]} {deck_id}")
        elif command == "sync" and not ids:
            synced = store.sync_ready(delay=0)
            print("synced " + ", ".join(synced) if synced else "nothing to sync")
            failed = [d for d in store.decks() if d.sync_error]
            for deck in failed:
                print(f"error: {deck.id}: {deck.sync_error}", file=sys.stderr)
            if failed:
                return 1
        elif command == "rebuild" and not ids:
            store.rebuild()
            print("rebuilt")
        else:
            print(USAGE, file=sys.stderr)
            return 2
    except PublishError as exc:
        print(f"error: {exc}", file=sys.stderr)
        if exc.log:
            print(exc.log[-4000:], file=sys.stderr)
        return 1
    return 0


def main(argv: list[str]) -> int:
    try:
        workspace = Path(os.environ["STAGEDOOR_WORKSPACE"])
        data_dir = Path(os.environ["STAGEDOOR_DATA_DIR"])
    except KeyError as missing:
        print(f"missing required environment variable {missing}", file=sys.stderr)
        return 2
    if not (workspace / "slides").is_dir():
        print(f"{workspace} does not look like an open-slide workspace", file=sys.stderr)
        return 2
    store = Store(workspace, data_dir, os.environ.get("STAGEDOOR_BUILD_CMD", DEFAULT_BUILD))
    if argv:
        return run_command(store, argv)
    host, _, port = os.environ.get("STAGEDOOR_LISTEN", "127.0.0.1:8090").rpartition(":")
    delay = int(os.environ.get("STAGEDOOR_SYNC_DELAY", "60"))
    app = App(
        store,
        os.environ.get("STAGEDOOR_BASE_PATH", "/publish"),
        os.environ.get("STAGEDOOR_PUBLIC_URL", ""),
        sync_delay=delay,
    )
    sync.start(store, delay)
    serve(app, host or "127.0.0.1", int(port))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
