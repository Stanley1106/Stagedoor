import http.client
import sys
import threading
import unittest
from http.server import HTTPServer
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from publisher.server import App, make_handler  # noqa: E402
from tests import test_store  # noqa: E402


class ServerTest(unittest.TestCase):
    def setUp(self):
        # Reuse the store fixture: a workspace with decks "alpha" and "beta".
        self.fixture = test_store.StoreTest("test_titles_come_from_meta")
        self.fixture.setUp()
        self.store = self.fixture.store
        app = App(self.store, "/publish", "https://slides.example.com/s/{id}")
        handler = make_handler(app)
        handler.log_message = lambda *args: None
        self.httpd = HTTPServer(("127.0.0.1", 0), handler)
        self.host = f"127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.fixture.tearDown()

    def request(self, method, path, form=None, origin="same"):
        conn = http.client.HTTPConnection(self.host, timeout=30)
        headers = {}
        body = None
        if form is not None:
            body = urlencode(form)
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if origin == "same":
            headers["Origin"] = f"http://{self.host}"
        elif origin:
            headers["Origin"] = origin
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read().decode("utf-8", "replace")
        conn.close()
        return resp, data

    def test_lists_decks_with_status(self):
        resp, page = self.request("GET", "/publish/")
        self.assertEqual(resp.status, 200)
        self.assertIn("Alpha deck", page)
        self.assertIn("Draft", page)
        self.assertIn(">02<", page)  # deck count folio

    def test_publish_redirects_and_updates_status(self):
        resp, _ = self.request("POST", "/publish/publish", {"id": "alpha"})
        self.assertEqual(resp.status, 303)
        self.assertIn("msg=Published", resp.getheader("Location"))
        _, page = self.request("GET", "/publish/")
        self.assertIn("View Public", page)
        self.assertIn("https://slides.example.com/s/alpha", page)

    def test_live_deck_offers_pause_then_resume(self):
        self.request("POST", "/publish/publish", {"id": "alpha"})
        _, page = self.request("GET", "/publish/")
        self.assertIn(">Live<", page)
        self.assertIn('action="/publish/pause"', page)
        resp, _ = self.request("POST", "/publish/pause", {"id": "alpha"})
        self.assertIn("msg=Paused", resp.getheader("Location"))
        _, page = self.request("GET", "/publish/")
        self.assertIn(">Paused<", page)
        self.assertIn('action="/publish/resume"', page)

    def test_pending_states(self):
        self.request("POST", "/publish/publish", {"id": "alpha"})
        self.request("POST", "/publish/publish", {"id": "beta"})
        self.request("POST", "/publish/pause", {"id": "beta"})
        self.fixture.add_deck("alpha", "Alpha deck", body="v2")
        self.fixture.add_deck("beta", "Beta deck", body="v2")
        _, page = self.request("GET", "/publish/")
        self.assertIn("Live · syncing", page)
        self.assertIn("Sync Now", page)
        self.assertIn("Paused · unpublished changes", page)
        self.assertIn("Publish Changes", page)

    def test_post_without_origin_is_refused(self):
        resp, _ = self.request("POST", "/publish/publish", {"id": "alpha"}, origin=None)
        self.assertEqual(resp.status, 403)
        self.assertFalse(self.store.is_live())

    def test_cross_origin_post_is_refused(self):
        resp, _ = self.request("POST", "/publish/publish", {"id": "alpha"}, origin="https://evil.example")
        self.assertEqual(resp.status, 403)
        self.assertFalse(self.store.is_live())

    def test_build_failure_shows_log(self):
        (self.fixture.ws / "slides" / "alpha" / "FAIL").write_text("")
        resp, page = self.request("POST", "/publish/publish", {"id": "alpha"})
        self.assertEqual(resp.status, 200)
        self.assertIn("Build failed", page)
        self.assertIn("boom: deck failed to compile", page)

    def test_titles_and_messages_are_escaped(self):
        self.fixture.add_deck("xss", "<script>alert(1)</script>")
        _, page = self.request("GET", "/publish/?msg=" + "%3Cimg%20src%3Dx%3E")
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn("<img src=x>", page)

    def test_unknown_paths_404(self):
        self.assertEqual(self.request("GET", "/publish/nope")[0].status, 404)
        self.assertEqual(self.request("POST", "/publish/delete", {"id": "alpha"})[0].status, 404)

    def test_healthz(self):
        resp, body = self.request("GET", "/publish/healthz")
        self.assertEqual((resp.status, body), (200, "ok\n"))


if __name__ == "__main__":
    unittest.main()
