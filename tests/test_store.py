import shlex
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from publisher.store import DRAFT, MODIFIED, ORPHANED, PUBLISHED, PublishError, Store  # noqa: E402

# Stands in for `open-slide build --out-dir {out}`: writes the published deck
# ids into index.html, and fails if any deck contains a file named FAIL.
FAKE_BUILD = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    out = Path(sys.argv[1])
    slides = sorted(p.name for p in Path("slides").iterdir() if not p.name.startswith("."))
    if any((Path("slides") / s / "FAIL").exists() for s in slides):
        print("boom: deck failed to compile")
        sys.exit(1)
    out.mkdir(parents=True)
    (out / "index.html").write_text(",".join(slides))
    """
)


class StoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.ws = root / "workspace"
        (self.ws / "node_modules").mkdir(parents=True)
        (self.ws / "package.json").write_text("{}")
        (self.ws / "open-slide.config.ts").write_text("export default {};")
        (self.ws / "themes").mkdir()
        script = root / "fake_build.py"
        script.write_text(FAKE_BUILD)
        cmd = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))} {{out}}"
        self.store = Store(self.ws, root / "data", cmd)
        self.add_deck("alpha", "Alpha deck")
        self.add_deck("beta", "Beta deck")

    def tearDown(self):
        self._tmp.cleanup()

    def add_deck(self, deck_id, title, body="pages"):
        deck = self.ws / "slides" / deck_id
        deck.mkdir(parents=True, exist_ok=True)
        (deck / "index.tsx").write_text(
            f"export const meta = {{ title: '{title}' }};\n// {body}\n", encoding="utf-8"
        )
        return deck

    def status(self):
        return {d.id: d.status for d in self.store.decks()}

    def served(self):
        return (self.store.current / "index.html").read_text()

    def test_new_decks_are_drafts_and_nothing_is_served(self):
        self.assertEqual(self.status(), {"alpha": DRAFT, "beta": DRAFT})
        self.assertFalse(self.store.is_live())

    def test_titles_come_from_meta(self):
        titles = {d.id: d.title for d in self.store.decks()}
        self.assertEqual(titles, {"alpha": "Alpha deck", "beta": "Beta deck"})

    def test_publish_serves_only_that_deck(self):
        self.store.publish("alpha")
        self.assertEqual(self.status(), {"alpha": PUBLISHED, "beta": DRAFT})
        self.assertEqual(self.served(), "alpha")

    def test_editing_after_publish_does_not_leak(self):
        self.store.publish("alpha")
        self.add_deck("alpha", "Alpha deck", body="secret draft edit")
        self.assertEqual(self.status()["alpha"], MODIFIED)
        published = (self.store.stage_slides / "alpha" / "index.tsx").read_text()
        self.assertNotIn("secret draft edit", published)

    def test_republish_picks_up_edits(self):
        self.store.publish("alpha")
        self.add_deck("alpha", "Alpha deck", body="v2")
        self.store.publish("alpha")
        self.assertEqual(self.status()["alpha"], PUBLISHED)
        self.assertIn("v2", (self.store.stage_slides / "alpha" / "index.tsx").read_text())

    def test_unpublish_removes_deck_from_site(self):
        self.store.publish("alpha")
        self.store.publish("beta")
        self.assertEqual(self.served(), "alpha,beta")
        self.store.unpublish("alpha")
        self.assertEqual(self.status(), {"alpha": DRAFT, "beta": PUBLISHED})
        self.assertEqual(self.served(), "beta")

    def test_deleted_deck_shows_as_orphaned_and_can_be_unpublished(self):
        self.store.publish("alpha")
        for f in (self.ws / "slides" / "alpha").iterdir():
            f.unlink()
        (self.ws / "slides" / "alpha").rmdir()
        self.assertEqual(self.status()["alpha"], ORPHANED)
        self.store.unpublish("alpha")
        self.assertNotIn("alpha", self.status())

    def test_failed_first_publish_rolls_back(self):
        self.store.publish("beta")
        (self.ws / "slides" / "alpha" / "FAIL").write_text("")
        with self.assertRaises(PublishError) as ctx:
            self.store.publish("alpha")
        self.assertIn("boom", ctx.exception.log)
        self.assertEqual(self.status(), {"alpha": DRAFT, "beta": PUBLISHED})
        self.assertEqual(self.served(), "beta")

    def test_failed_republish_keeps_previous_snapshot(self):
        self.store.publish("alpha")
        self.add_deck("alpha", "Alpha deck", body="broken v2")
        (self.ws / "slides" / "alpha" / "FAIL").write_text("")
        with self.assertRaises(PublishError):
            self.store.publish("alpha")
        published = (self.store.stage_slides / "alpha" / "index.tsx").read_text()
        self.assertNotIn("broken v2", published)
        self.assertEqual(self.served(), "alpha")

    def test_rejects_bad_ids(self):
        for bad in ("../etc", "a/b", "", ".hidden", "x y"):
            with self.assertRaises(PublishError):
                self.store.publish(bad)
        with self.assertRaises(PublishError):
            self.store.publish("missing")
        with self.assertRaises(PublishError):
            self.store.unpublish("alpha")

    def test_old_releases_are_pruned(self):
        for _ in range(5):
            self.store.rebuild()
        releases = [p for p in self.store.releases.iterdir() if p.is_dir()]
        self.assertLessEqual(len(releases), 3)
        self.assertIn(self.store.current.resolve(), [p.resolve() for p in releases])

    def test_publish_time_is_recorded_and_cleared(self):
        self.store.publish("alpha")
        when = {d.id: d.published_at for d in self.store.decks()}
        self.assertIsNotNone(when["alpha"])
        self.assertIsNone(when["beta"])
        self.store.unpublish("alpha")
        self.assertIsNone({d.id: d.published_at for d in self.store.decks()}["alpha"])

    def test_failed_publish_keeps_previous_time(self):
        self.store.publish("alpha")
        before = self.store.decks()[0].published_at
        (self.ws / "slides" / "alpha" / "FAIL").write_text("")
        with self.assertRaises(PublishError):
            self.store.publish("alpha")
        self.assertEqual(self.store.decks()[0].published_at, before)

    def test_stage_links_node_modules_and_copies_config(self):
        self.store.publish("alpha")
        self.assertTrue((self.store.stage / "node_modules").is_symlink())
        self.assertTrue((self.store.stage / "open-slide.config.ts").is_file())
        self.assertTrue((self.store.stage / "themes").is_dir())


if __name__ == "__main__":
    unittest.main()
