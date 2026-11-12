"""Per-deck publish state for an open-slide workspace.

Layout of the data directory::

    <data>/stage/            build workspace holding only published decks
        slides/<id>/         snapshot of a deck taken when it was published
        themes/ assets/      copied from the live workspace on every build
        node_modules         symlink to the live workspace's node_modules
    <data>/releases/<ts>/    output of each successful build
    <data>/current           symlink to the release being served
    <data>/last-build.log    output of the most recent build
    <data>/published.json    per published deck: when it was last published
                             (UTC, ISO 8601) and its mode, "live" or "paused"

The live workspace is never modified. A deck is public only once its snapshot
is in ``stage/slides`` and a build containing it has been swapped in.

Published decks are *live* by default: ``sync_ready`` republishes them once
their editor copy has changed and then sat untouched for a while. A *paused*
deck keeps its public snapshot until it is published again or resumed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SLIDE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
TITLE_RE = re.compile(r"\btitle\s*:\s*(['\"`])(.+?)\1")

# Files copied from the live workspace into the stage before each build.
WORKSPACE_FILES = ("package.json", "open-slide.config.ts", "tsconfig.json")
# Directories shared by every deck, copied wholesale before each build.
SHARED_DIRS = ("themes", "assets")
KEEP_RELEASES = 3

DRAFT = "draft"  # exists in the workspace, never published
PUBLISHED = "published"  # public copy matches the workspace
MODIFIED = "modified"  # public copy is older than the workspace
ORPHANED = "orphaned"  # public, but deleted from the workspace

LIVE = "live"  # republished automatically after edits settle
PAUSED = "paused"  # public copy frozen until published or resumed


class PublishError(Exception):
    """A publish or unpublish failed; the public site was left unchanged."""

    def __init__(self, message: str, log: str = ""):
        super().__init__(message)
        self.log = log


@dataclass(frozen=True)
class Deck:
    id: str
    title: str
    status: str
    published_at: str | None = None
    mode: str | None = None  # LIVE or PAUSED once published, else None
    sync_error: str | None = None  # last automatic sync failure, if any


def tree_hash(root: Path) -> str:
    """Hash every file path and its content under ``root``."""
    h = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        try:
            data = path.read_bytes()
        except FileNotFoundError:  # deleted by the editor while we were looking
            continue
        h.update(path.relative_to(root).as_posix().encode())
        h.update(b"\0")
        h.update(hashlib.sha256(data).digest())
    return h.hexdigest()


def newest_mtime(root: Path) -> float:
    """Latest modification time of ``root`` or anything inside it."""
    newest = 0.0
    for path in (root, *root.rglob("*")):
        try:
            newest = max(newest, path.stat().st_mtime)
        except FileNotFoundError:
            continue
    return newest


def read_title(deck_dir: Path) -> str | None:
    try:
        source = (deck_dir / "index.tsx").read_text(encoding="utf-8")
    except OSError:
        return None
    match = TITLE_RE.search(source)
    return match.group(2) if match else None


def _deck_ids(slides_dir: Path) -> set[str]:
    if not slides_dir.is_dir():
        return set()
    return {
        p.name
        for p in slides_dir.iterdir()
        if p.is_dir() and SLIDE_ID_RE.match(p.name) and (p / "index.tsx").is_file()
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, workspace: Path, data_dir: Path, build_cmd: str):
        self.workspace = Path(workspace)
        self.data = Path(data_dir)
        self.build_cmd = build_cmd
        self.stage = self.data / "stage"
        self.releases = self.data / "releases"
        self.current = self.data / "current"
        self.log_file = self.data / "last-build.log"
        self.state_file = self.data / "published.json"
        # The web UI and the background sync both build; one at a time.
        self.lock = threading.RLock()
        # deck id -> (tree hash that failed to build, short message)
        self._sync_failures: dict[str, tuple[str, str]] = {}

    @property
    def live_slides(self) -> Path:
        return self.workspace / "slides"

    @property
    def stage_slides(self) -> Path:
        return self.stage / "slides"

    # ---- status ---------------------------------------------------------

    def decks(self) -> list[Deck]:
        # Builds briefly move snapshots aside, so read state under the lock.
        with self.lock:
            return self._decks()

    def _decks(self) -> list[Deck]:
        live = _deck_ids(self.live_slides)
        public = _deck_ids(self.stage_slides)
        state = self._state()
        result = []
        for deck_id in sorted(live | public):
            if deck_id not in public:
                status = DRAFT
            elif deck_id not in live:
                status = ORPHANED
            elif tree_hash(self.live_slides / deck_id) == tree_hash(self.stage_slides / deck_id):
                status = PUBLISHED
            else:
                status = MODIFIED
            source = self.live_slides if deck_id in live else self.stage_slides
            title = read_title(source / deck_id) or deck_id
            entry = state.get(deck_id, {}) if deck_id in public else {}
            mode = entry.get("mode", LIVE) if deck_id in public else None
            failure = self._sync_failures.get(deck_id) if status == MODIFIED else None
            result.append(
                Deck(deck_id, title, status, entry.get("published_at"), mode, failure and failure[1])
            )
        return result

    def is_live(self) -> bool:
        return self.current.exists()

    def last_log(self) -> str:
        try:
            return self.log_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    # ---- actions --------------------------------------------------------

    def publish(self, deck_id: str) -> None:
        """Publish a draft (as live) or push a published deck's latest edits."""
        self._check_id(deck_id)
        with self.lock:
            if deck_id not in _deck_ids(self.live_slides):
                raise PublishError(f"Deck '{deck_id}' does not exist in the workspace.")
            source = self.live_slides / deck_id
            mode = self._state().get(deck_id, {}).get("mode", LIVE)
            self._change_and_build(deck_id, lambda target: shutil.copytree(source, target))
            self._sync_failures.pop(deck_id, None)
            self._set_state(deck_id, {"published_at": _now_iso(), "mode": mode})

    def unpublish(self, deck_id: str) -> None:
        self._check_id(deck_id)
        with self.lock:
            if deck_id not in _deck_ids(self.stage_slides):
                raise PublishError(f"Deck '{deck_id}' is not published.")
            self._change_and_build(deck_id, lambda target: None)
            self._sync_failures.pop(deck_id, None)
            self._set_state(deck_id, None)

    def pause(self, deck_id: str) -> None:
        self._set_mode(deck_id, PAUSED)

    def resume(self, deck_id: str) -> None:
        self._set_mode(deck_id, LIVE)

    def rebuild(self) -> None:
        """Rebuild the public site from the current snapshots."""
        with self.lock:
            self._prepare_stage()
            self._build()

    def sync_ready(self, delay: float, now: float | None = None) -> list[str]:
        """Republish live decks whose edits have been quiet for ``delay`` seconds.

        A deck whose build fails is not retried until its content changes
        again. Returns the ids that were republished.
        """
        now = time.time() if now is None else now
        synced = []
        with self.lock:
            for deck in self.decks():
                if deck.mode != LIVE or deck.status != MODIFIED:
                    continue
                source = self.live_slides / deck.id
                if now - newest_mtime(source) < delay:
                    continue  # still being edited
                content = tree_hash(source)
                failed = self._sync_failures.get(deck.id)
                if failed and failed[0] == content:
                    continue
                try:
                    self.publish(deck.id)
                except PublishError as exc:
                    self._sync_failures[deck.id] = (content, str(exc))
                else:
                    synced.append(deck.id)
        return synced

    # ---- internals ------------------------------------------------------

    def _state(self) -> dict[str, dict]:
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        # v0.1 stored only a timestamp per deck; those decks become live.
        return {k: v if isinstance(v, dict) else {"published_at": v, "mode": LIVE} for k, v in raw.items()}

    def _set_state(self, deck_id: str, entry: dict | None) -> None:
        state = self._state()
        if entry is None:
            state.pop(deck_id, None)
        else:
            state[deck_id] = entry
        self.data.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.state_file)

    def _set_mode(self, deck_id: str, mode: str) -> None:
        self._check_id(deck_id)
        with self.lock:
            if deck_id not in _deck_ids(self.stage_slides):
                raise PublishError(f"Deck '{deck_id}' is not published.")
            entry = self._state().get(deck_id, {"published_at": None})
            self._set_state(deck_id, {**entry, "mode": mode})

    @staticmethod
    def _check_id(deck_id: str) -> None:
        if not SLIDE_ID_RE.match(deck_id or ""):
            raise PublishError("Invalid deck id.")

    def _change_and_build(self, deck_id: str, place) -> None:
        """Swap one deck's snapshot, build, and roll back if the build fails."""
        self._prepare_stage()
        target = self.stage_slides / deck_id
        backup = self.stage_slides / f".{deck_id}.previous"
        shutil.rmtree(backup, ignore_errors=True)
        if target.exists():
            target.rename(backup)
        try:
            place(target)
            self._build()
        except BaseException:
            shutil.rmtree(target, ignore_errors=True)
            if backup.exists():
                backup.rename(target)
            raise
        shutil.rmtree(backup, ignore_errors=True)

    def _prepare_stage(self) -> None:
        self.stage_slides.mkdir(parents=True, exist_ok=True)
        self.releases.mkdir(parents=True, exist_ok=True)
        for name in WORKSPACE_FILES:
            src = self.workspace / name
            if src.is_file():
                shutil.copy2(src, self.stage / name)
        for name in SHARED_DIRS:
            dst = self.stage / name
            shutil.rmtree(dst, ignore_errors=True)
            src = self.workspace / name
            if src.is_dir():
                shutil.copytree(src, dst)
        modules = self.stage / "node_modules"
        if not modules.is_symlink():
            shutil.rmtree(modules, ignore_errors=True)
            modules.symlink_to(self.workspace / "node_modules", target_is_directory=True)

    def _build(self) -> None:
        out = self.releases / time.strftime("%Y%m%d-%H%M%S")
        suffix = 1
        while out.exists():
            out = out.with_name(f"{out.name.split('.')[0]}.{suffix}")
            suffix += 1
        cmd = [part.replace("{out}", str(out)) for part in shlex.split(self.build_cmd)]
        try:
            proc = subprocess.run(
                cmd,
                cwd=self.stage,
                capture_output=True,
                text=True,
                timeout=600,
            )
            log = proc.stdout + proc.stderr
            ok = proc.returncode == 0 and (out / "index.html").is_file()
        except (OSError, subprocess.TimeoutExpired) as exc:
            log, ok = f"{type(exc).__name__}: {exc}", False
        self.log_file.write_text(log, encoding="utf-8")
        if not ok:
            shutil.rmtree(out, ignore_errors=True)
            raise PublishError("Build failed; the public site was not changed.", log)
        self._activate(out)

    def _activate(self, release: Path) -> None:
        tmp = self.data / ".current.tmp"
        if tmp.is_symlink() or tmp.exists():
            tmp.unlink()
        tmp.symlink_to(release, target_is_directory=True)
        os.replace(tmp, self.current)  # atomic: readers see old or new, never neither
        self._prune(keep=release)

    def _prune(self, keep: Path) -> None:
        old = sorted(p for p in self.releases.iterdir() if p.is_dir() and p != keep)
        for path in old[: max(0, len(old) - (KEEP_RELEASES - 1))]:
            shutil.rmtree(path, ignore_errors=True)
