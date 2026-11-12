"""Background loop that keeps live decks in sync with the editor."""

from __future__ import annotations

import sys
import threading

from .store import Store


def start(store: Store, delay: float, interval: float = 5.0) -> threading.Event:
    """Check every ``interval`` seconds; returns an event that stops the loop."""
    stop = threading.Event()

    def loop() -> None:
        while not stop.wait(interval):
            try:
                for deck_id in store.sync_ready(delay):
                    print(f"synced {deck_id}", flush=True)
            except Exception as exc:  # keep the loop alive; the UI shows deck errors
                print(f"sync pass failed: {exc!r}", file=sys.stderr, flush=True)

    threading.Thread(target=loop, name="stagedoor-sync", daemon=True).start()
    return stop
