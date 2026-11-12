import http.client
import sys
import threading
import unittest
from http.server import HTTPServer
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from publisher.server import App, make_handler  # noqa: E402
from tests import test_history  # noqa: E402


class HistoryServerTest(unittest.TestCase):
    def setUp(self):
        self.fixture = test_history.HistoryTest("test_versions_list_newest_first_with_kinds_and_line_counts")
        self.fixture.setUp()
        self.store, self.history = self.fixture.store, self.fixture.history
        handler = make_handler(App(self.store, "/publish"))
        handler.log_message = lambda *args: None
        self.httpd = HTTPServer(("127.0.0.1", 0), handler)
        self.host = f"127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.fixture.tearDown()

    def request(self, method, path, form=None, origin=True):
        conn = http.client.HTTPConnection(self.host, timeout=30)
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if origin:
            headers["Origin"] = f"http://{self.host}"
        conn.request(method, path, body=urlencode(form) if form else None, headers=headers)
        resp = conn.getresponse()
        data = resp.read().decode()
        conn.close()
        return resp, data

    def test_index_links_to_history_and_unpublish_uses_the_dialog(self):
        self.store.publish("alpha")
        _, page = self.request("GET", "/publish/")
        self.assertIn('href="/publish/history/alpha"', page)
        self.assertIn('data-confirm-title="Unpublish “Alpha deck”?"', page)
        self.assertIn('<dialog id="confirm"', page)

    def test_history_page_marks_current_and_public(self):
        self.fixture.edit("v2")
        self.store.publish("alpha")
        self.fixture.edit("v3")
        self.history.commit("alpha", "autosave: alpha")
        resp, page = self.request("GET", "/publish/history/alpha")
        self.assertEqual(resp.status, 200)
        self.assertIn("Auto-saved", page)
        self.assertIn("Published", page)
        self.assertEqual(page.count(">Current<"), 1)
        self.assertEqual(page.count(">Public<"), 1)
        self.assertEqual(page.count('action="/publish/restore"'), 2)  # all but the current one

    def test_unsaved_edits_hide_current_and_show_a_hint(self):
        self.fixture.edit("not yet saved")
        _, page = self.request("GET", "/publish/history/alpha")
        self.assertIn("aren’t in history yet", page)
        self.assertNotIn(">Current<", page)

    def test_restore_redirects_with_undo_and_undo_works(self):
        self.fixture.edit("good")
        good = self.history.commit("alpha", "autosave: alpha")
        self.fixture.edit("broken")
        self.history.commit("alpha", "autosave: alpha")
        resp, _ = self.request("POST", "/publish/restore", {"id": "alpha", "rev": good})
        self.assertEqual(resp.status, 303)
        location = resp.getheader("Location")
        self.assertIn("/publish/history/alpha?msg=Restored", location)
        self.assertIn("&undo=", location)
        self.assertIn("good", self.fixture.source())
        _, page = self.request("GET", location)
        self.assertIn(">Undo<", page)
        undo = location.split("undo=")[1]
        self.request("POST", "/publish/restore", {"id": "alpha", "rev": undo})
        self.assertIn("broken", self.fixture.source())

    def test_bad_restores_change_nothing(self):
        before = self.fixture.source()
        resp, page = self.request("POST", "/publish/restore", {"id": "alpha", "rev": "0000000"})
        self.assertEqual(resp.status, 200)
        self.assertIn("Nothing was changed", page)
        resp, _ = self.request("POST", "/publish/restore", {"id": "alpha", "rev": "0000000"}, origin=False)
        self.assertEqual(resp.status, 403)
        self.assertEqual(self.fixture.source(), before)

    def test_unknown_deck_is_404(self):
        self.assertEqual(self.request("GET", "/publish/history/nope")[0].status, 404)
        self.assertEqual(self.request("GET", "/publish/history/..%2Fetc")[0].status, 404)


if __name__ == "__main__":
    unittest.main()
