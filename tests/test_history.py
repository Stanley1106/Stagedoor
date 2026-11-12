import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from publisher.history import AUTOSAVE, OTHER, PUBLISH, RESTORE, History  # noqa: E402
from publisher.store import PublishError  # noqa: E402
from tests import test_store  # noqa: E402


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


class HistoryTest(unittest.TestCase):
    def setUp(self):
        self.fixture = test_store.StoreTest("test_titles_come_from_meta")
        self.fixture.setUp()
        self.ws = self.fixture.ws
        git(self.ws, "init", "-q")
        git(self.ws, "config", "user.name", "t")
        git(self.ws, "config", "user.email", "t@example.com")
        git(self.ws, "add", "-A")
        git(self.ws, "commit", "-qm", "initial workspace")
        self.history = History(self.ws)
        self.store = self.fixture.store
        self.store.history = self.history

    def tearDown(self):
        self.fixture.tearDown()

    def source(self, deck="alpha"):
        return (self.ws / "slides" / deck / "index.tsx").read_text()

    def edit(self, body, deck="alpha"):
        self.fixture.add_deck(deck, f"{deck.title()} deck", body=body)

    def test_versions_list_newest_first_with_kinds_and_line_counts(self):
        self.edit("v2")
        self.history.commit("alpha", "autosave: alpha")
        versions = self.history.versions("alpha")
        self.assertEqual([v.kind for v in versions], [AUTOSAVE, OTHER])
        self.assertEqual((versions[0].added, versions[0].removed), (1, 1))
        self.assertEqual(self.history.versions("beta")[0].subject, "initial workspace")

    def test_publish_records_the_public_version(self):
        self.edit("v2")
        self.store.publish("alpha")
        latest = self.history.versions("alpha")[0]
        self.assertEqual((latest.kind, latest.subject), (PUBLISH, "publish: alpha"))
        self.assertEqual(self.store.public_rev("alpha"), latest.rev)

    def test_publishing_unchanged_content_adds_no_commit(self):
        self.store.publish("alpha")
        before = len(self.history.versions("alpha"))
        self.store.publish("alpha")
        self.assertEqual(len(self.history.versions("alpha")), before)
        self.assertEqual(self.store.public_rev("alpha"), self.history.versions("alpha")[0].rev)

    def test_restore_brings_content_back_and_can_be_undone(self):
        self.edit("good")
        good = self.history.commit("alpha", "autosave: alpha")
        self.edit("broken by an agent")
        new_rev, undo = self.store.restore("alpha", good[:7])
        self.assertIn("good", self.source())
        latest = self.history.versions("alpha")[0]
        self.assertEqual((latest.kind, latest.rev, latest.restored_from), (RESTORE, new_rev, good[:7]))
        # the broken state was saved first, so the restore is reversible
        self.store.restore("alpha", undo)
        self.assertIn("broken by an agent", self.source())

    def test_restore_brings_back_a_deleted_file(self):
        extra = self.ws / "slides" / "alpha" / "notes.md"
        extra.write_text("keep me")
        keep = self.history.commit("alpha", "autosave: alpha")
        extra.unlink()
        self.history.commit("alpha", "autosave: alpha")
        self.store.restore("alpha", keep)
        self.assertEqual(extra.read_text(), "keep me")

    def test_restore_rejects_unknown_versions(self):
        other = self.history.commit("beta", "autosave: beta")  # no-op: returns beta's last commit
        git(self.ws, "commit", "-q", "--allow-empty", "-m", "unrelated")
        unrelated = git(self.ws, "rev-parse", "HEAD").strip()
        for bad in ("nothex!", "", "0000000", unrelated):
            with self.assertRaises(PublishError):
                self.store.restore("alpha", bad)
        self.assertTrue(other)

    def test_unsaved_edits_and_junk_files(self):
        self.assertFalse(self.history.has_unsaved("alpha"))
        (self.ws / "slides" / "alpha" / "index.tsx.swp").write_text("swap")
        self.assertFalse(self.history.has_unsaved("alpha"))
        self.edit("v2")
        self.assertTrue(self.history.has_unsaved("alpha"))
        self.history.commit("alpha", "autosave: alpha")
        self.assertFalse(self.history.has_unsaved("alpha"))
        self.assertEqual(git(self.ws, "ls-files", "*.swp"), "")

    def test_new_files_are_saved_next_to_tilde_backups(self):
        # Regression: an exclude pathspec containing "~" made git skip new files.
        deck = self.ws / "slides" / "alpha"
        (deck / "index.tsx~").write_text("editor backup")
        (deck / "cover.svg").write_text("<svg/>")
        self.history.commit("alpha", "autosave: alpha")
        tracked = git(self.ws, "ls-files", "slides/alpha").split()
        self.assertIn("slides/alpha/cover.svg", tracked)
        self.assertNotIn("slides/alpha/index.tsx~", tracked)

    def test_publish_still_works_without_git(self):
        self.store.history = History(self.ws / "slides")  # not a repo root
        self.store.publish("alpha")
        self.assertIsNone(self.store.public_rev("alpha"))
        with self.assertRaises(PublishError):
            self.store.restore("alpha", "abcdef1")


if __name__ == "__main__":
    unittest.main()


class HistoryCliTest(unittest.TestCase):
    def setUp(self):
        self.fixture = HistoryTest("test_versions_list_newest_first_with_kinds_and_line_counts")
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def test_history_and_restore_commands(self):
        import io
        from contextlib import redirect_stdout

        from publisher.__main__ import run_command

        self.fixture.edit("good")
        good = self.fixture.history.commit("alpha", "autosave: alpha")
        self.fixture.edit("broken")
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(run_command(self.fixture.store, ["history", "alpha"]), 0)
            self.assertEqual(run_command(self.fixture.store, ["restore", "alpha", good[:7]]), 0)
        self.assertIn("autosave: alpha", out.getvalue())
        self.assertIn("undo with: restore alpha", out.getvalue())
        self.assertIn("good", self.fixture.source())
