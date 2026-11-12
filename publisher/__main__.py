"""Run the publisher: ``python3 -m publisher``.

Configuration comes from environment variables:

  STAGEDOOR_WORKSPACE   path to the live open-slide workspace (required)
  STAGEDOOR_DATA_DIR    where snapshots and builds are kept (required)
  STAGEDOOR_LISTEN      host:port to bind (default 127.0.0.1:8090)
  STAGEDOOR_BASE_PATH   URL prefix of the UI (default /publish)
  STAGEDOOR_PUBLIC_URL  public deck URL template, e.g. https://slides.example.com/s/{id}
  STAGEDOOR_BUILD_CMD   build command run in the stage dir; {out} is the output dir
                        (default: npx --no-install open-slide build --out-dir {out})
"""

import os
import sys
from pathlib import Path

from .server import App, serve
from .store import Store

DEFAULT_BUILD = "npx --no-install open-slide build --out-dir {out}"


def main() -> int:
    try:
        workspace = Path(os.environ["STAGEDOOR_WORKSPACE"])
        data_dir = Path(os.environ["STAGEDOOR_DATA_DIR"])
    except KeyError as missing:
        print(f"missing required environment variable {missing}", file=sys.stderr)
        return 2
    if not (workspace / "slides").is_dir():
        print(f"{workspace} does not look like an open-slide workspace", file=sys.stderr)
        return 2
    host, _, port = os.environ.get("STAGEDOOR_LISTEN", "127.0.0.1:8090").rpartition(":")
    store = Store(workspace, data_dir, os.environ.get("STAGEDOOR_BUILD_CMD", DEFAULT_BUILD))
    app = App(store, os.environ.get("STAGEDOOR_BASE_PATH", "/publish"), os.environ.get("STAGEDOOR_PUBLIC_URL", ""))
    serve(app, host or "127.0.0.1", int(port))
    return 0


if __name__ == "__main__":
    sys.exit(main())
