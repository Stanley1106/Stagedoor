"""Per-deck version history, kept as git commits in the open-slide workspace.

Three kinds of commits touch ``slides/<id>``:

  autosave: <ids>        written by deploy/autosave once edits settle
  publish: <id>          written by Stagedoor right before a deck is published
  restore: <id> to <rev> written by Stagedoor when a deck is restored

Any other commit (made by hand, by an agent, by ``open-slide init``) is listed
too. Restoring never rewrites history: it commits the old content on top, so a
restore can itself be undone by restoring the version before it.
"""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

REV_RE = re.compile(r"^[0-9a-f]{7,40}$")
# Editor swap/backup/partial files are never part of a version. They go in
# .git/info/exclude (local to the repo, never committed) rather than pathspec
# excludes: git skips new files when an exclude pathspec contains "~".
JUNK = ("*.swp", "*.swx", "*~", "*.tmp", "*.part")

AUTOSAVE, PUBLISH, RESTORE, OTHER = "autosave", "publish", "restore", "other"


class HistoryError(Exception):
    pass


@dataclass(frozen=True)
class Version:
    rev: str
    at: str  # ISO 8601 with offset
    kind: str
    subject: str
    added: int
    removed: int

    @property
    def short(self) -> str:
        return self.rev[:7]

    @property
    def restored_from(self) -> str | None:
        if self.kind != RESTORE:
            return None
        match = re.search(r" to ([0-9a-f]{7,40})$", self.subject)
        return match.group(1) if match else None


def _kind(subject: str) -> str:
    for kind in (AUTOSAVE, PUBLISH, RESTORE):
        if subject.startswith(kind + ":"):
            return kind
    return OTHER


class History:
    def __init__(self, workspace: Path):
        self.workspace = Path(workspace)

    def available(self) -> bool:
        return (self.workspace / ".git").exists() and self._run("rev-parse", "--git-dir", check=False) is not None

    def versions(self, deck_id: str, limit: int = 200) -> list[Version]:
        out = self._run(
            "log", f"-{limit}", "--format=%x1e%H%x1f%aI%x1f%s", "--numstat", "--", self._path(deck_id),
            check=False,
        )
        versions = []
        for record in (out or "").split("\x1e")[1:]:
            header, _, stats = record.partition("\n")
            rev, at, subject = header.split("\x1f", 2)
            added = removed = 0
            for line in stats.splitlines():
                parts = line.split("\t")
                if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
                    added, removed = added + int(parts[0]), removed + int(parts[1])
            versions.append(Version(rev, at, _kind(subject), subject, added, removed))
        return versions

    def has_unsaved(self, deck_id: str) -> bool:
        """Edits in the editor that no commit holds yet."""
        self._ignore_junk()
        out = self._run("status", "--porcelain", "--", self._path(deck_id), check=False)
        return bool(out and out.strip())

    def commit(self, deck_id: str, message: str) -> str | None:
        """Commit the deck if it changed; return the rev that holds its content."""
        path = self._path(deck_id)
        self._ignore_junk()
        self._retry("add", "-A", "--", path)
        if self._run("diff", "--cached", "--quiet", "--", path, check=False) is None:
            self._retry("commit", "-q", "-m", message, "--", path)
        out = self._run("log", "-1", "--format=%H", "--", path, check=False)
        return out.strip() or None if out else None

    def restore(self, deck_id: str, rev: str) -> tuple[str, str | None]:
        """Bring a deck back to ``rev``. Returns (new rev, rev to undo to)."""
        if not REV_RE.match(rev or ""):
            raise HistoryError("Invalid version.")
        known = {v.rev for v in self.versions(deck_id, limit=10_000)}
        full = next((r for r in known if r.startswith(rev)), None)
        if full is None:
            raise HistoryError("That version is not in this deck's history.")
        # Keep whatever is in the editor right now, so the restore can be undone.
        before = self.commit(deck_id, f"autosave: {deck_id}")
        path = self._path(deck_id)
        self._retry("restore", f"--source={full}", "--staged", "--worktree", "--", path)
        after = self.commit(deck_id, f"restore: {deck_id} to {full[:7]}")
        return after or full, before

    # ---- internals ------------------------------------------------------

    @staticmethod
    def _path(deck_id: str) -> str:
        return f"slides/{deck_id}"

    def _ignore_junk(self) -> None:
        exclude = self.workspace / ".git" / "info" / "exclude"
        try:
            lines = exclude.read_text(encoding="utf-8").splitlines() if exclude.exists() else []
            missing = [pattern for pattern in JUNK if pattern not in lines]
            if missing:
                exclude.parent.mkdir(parents=True, exist_ok=True)
                with exclude.open("a", encoding="utf-8") as fh:
                    fh.write("".join(f"{pattern}\n" for pattern in missing))
        except OSError:
            pass  # worst case a swap file gets committed

    def _retry(self, *args: str) -> str:
        # The autosave service commits to the same repo; wait out its lock.
        last = ""
        for attempt in range(10):
            try:
                return self._run(*args)
            except HistoryError as exc:
                last = str(exc)
                if "index.lock" not in last:
                    raise
                time.sleep(0.3 * (attempt + 1))
        raise HistoryError(last)

    def _run(self, *args: str, check: bool = True) -> str | None:
        try:
            proc = subprocess.run(
                ["git", *args], cwd=self.workspace, capture_output=True, text=True, timeout=60
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            if check:
                raise HistoryError(f"git {args[0]} failed: {exc}") from exc
            return None
        if proc.returncode != 0:
            if check:
                raise HistoryError(f"git {args[0]} failed: {(proc.stderr or proc.stdout).strip()}")
            return None
        return proc.stdout
