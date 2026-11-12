"""Per-deck publish state for an open-slide workspace.

Layout of the data directory::

    <data>/stage/            build workspace holding only published decks
        slides/<id>/         snapshot of a deck taken when it was published
        themes/ assets/      copied from the live workspace on every build
        node_modules         symlink to the live workspace's node_modules
    <data>/releases/<ts>/    output of each successful build
    <data>/current           symlink to the release being served
    <data>/last-build.log    output of the most recent build

The live workspace is never modified. A deck is public only once its snapshot
is in ``stage/slides`` and a build containing it has been swapped in.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
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


def tree_hash(root: Path) -> str:
    """Hash every file path and its content under ``root``."""
    h = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        h.update(path.relative_to(root).as_posix().encode())
        h.update(b"\0")
        h.update(hashlib.sha256(path.read_bytes()).digest())
    return h.hexdigest()


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


class Store:
    def __init__(self, workspace: Path, data_dir: Path, build_cmd: str):
        self.workspace = Path(workspace)
        self.data = Path(data_dir)
        self.build_cmd = build_cmd
        self.stage = self.data / "stage"
        self.releases = self.data / "releases"
        self.current = self.data / "current"
        self.log_file = self.data / "last-build.log"

    @property
    def live_slides(self) -> Path:
        return self.workspace / "slides"

    @property
    def stage_slides(self) -> Path:
        return self.stage / "slides"

    # ---- status ---------------------------------------------------------

    def decks(self) -> list[Deck]:
        live = _deck_ids(self.live_slides)
        public = _deck_ids(self.stage_slides)
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
            result.append(Deck(deck_id, title, status))
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
        self._check_id(deck_id)
        source = self.live_slides / deck_id
        if deck_id not in _deck_ids(self.live_slides):
            raise PublishError(f"Deck '{deck_id}' does not exist in the workspace.")
        self._change_and_build(deck_id, lambda target: shutil.copytree(source, target))

    def unpublish(self, deck_id: str) -> None:
        self._check_id(deck_id)
        if deck_id not in _deck_ids(self.stage_slides):
            raise PublishError(f"Deck '{deck_id}' is not published.")
        self._change_and_build(deck_id, lambda target: None)

    def rebuild(self) -> None:
        """Rebuild the public site from the current snapshots."""
        self._prepare_stage()
        self._build()

    # ---- internals ------------------------------------------------------

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
