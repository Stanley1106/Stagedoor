import io
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from publisher.__main__ import run_command  # noqa: E402
from tests import test_store  # noqa: E402


class CliTest(unittest.TestCase):
    def setUp(self):
        self.fixture = test_store.StoreTest("test_titles_come_from_meta")
        self.fixture.setUp()
        self.store = self.fixture.store

    def tearDown(self):
        self.fixture.tearDown()

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = run_command(self.store, list(args))
        return code, out.getvalue(), err.getvalue()

    def test_publish_then_status(self):
        self.assertEqual(self.run_cli("publish", "alpha", "beta")[0], 0)
        code, out, _ = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertRegex(out, r"published\s+alpha")
        self.assertRegex(out, r"published\s+beta")

    def test_failure_returns_1_with_log(self):
        (self.fixture.ws / "slides" / "alpha" / "FAIL").write_text("")
        code, _, err = self.run_cli("publish", "alpha")
        self.assertEqual(code, 1)
        self.assertIn("boom", err)

    def test_bad_usage_returns_2(self):
        self.assertEqual(self.run_cli("publish")[0], 2)
        self.assertEqual(self.run_cli("explode")[0], 2)


if __name__ == "__main__":
    unittest.main()
