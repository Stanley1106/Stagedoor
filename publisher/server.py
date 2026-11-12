"""Small web UI for publishing and unpublishing decks.

Meant to run on localhost behind an authenticating proxy (for example
Cloudflare Access) on the same hostname as the open-slide dev server.
It is single-threaded on purpose: one request, and so one build, at a time.

The look follows open-slide's own app chrome (neutral ramp, vermillion
accent, Geist, 6px radius, hairlines) so the page reads as part of it.
"""

from __future__ import annotations

import html
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from .store import DRAFT, MODIFIED, ORPHANED, PUBLISHED, PublishError, Store

FONT_PATH = Path("node_modules/@fontsource-variable/geist/files/geist-latin-wght-normal.woff2")

STATUS = {
    # status: (label, explanation)
    PUBLISHED: ("Published", "Public copy matches the editor."),
    MODIFIED: ("Unpublished changes", "The editor has changes the public site doesn’t."),
    DRAFT: ("Draft", "Only visible in the editor."),
    ORPHANED: ("Deleted in editor", "Gone from the editor but still public."),
}

CSS = """
@font-face {
  font-family: "Geist Variable"; font-style: normal; font-display: swap;
  font-weight: 100 900; src: url("{base}/geist.woff2") format("woff2-variations");
}
:root {
  color-scheme: light dark;
  --background: oklch(0.99 0 0); --foreground: oklch(0.145 0 0); --canvas: oklch(0.975 0 0);
  --card: oklch(1 0 0); --muted: oklch(0.955 0 0); --muted-foreground: oklch(0.5 0 0);
  --border: oklch(0.92 0 0); --hairline: oklch(0.885 0 0);
  --brand: oklch(0.6 0.2 25); --brand-soft: oklch(0.6 0.2 25 / 0.1);
  --destructive: oklch(0.58 0.22 27); --ring: oklch(0.6 0.2 25);
  --ease-swift: cubic-bezier(0.22, 1, 0.36, 1);
  --shadow-edge: 0 0 0 0.5px oklch(0 0 0 / 0.06), 0 1px 0 oklch(0 0 0 / 0.025);
}
@media (prefers-color-scheme: dark) {
  :root {
    --background: oklch(0.175 0 0); --foreground: oklch(0.93 0 0); --canvas: oklch(0.1 0 0);
    --card: oklch(0.19 0 0); --muted: oklch(0.23 0 0); --muted-foreground: oklch(0.68 0 0);
    --border: oklch(1 0 0 / 0.1); --hairline: oklch(1 0 0 / 0.07);
    --brand: oklch(0.63 0.2 25); --brand-soft: oklch(0.63 0.2 25 / 0.16);
    --destructive: oklch(0.66 0.2 25); --ring: oklch(0.63 0.19 25);
  }
}
* { box-sizing: border-box; }
html {
  font-family: "Geist Variable", -apple-system, BlinkMacSystemFont, "Noto Sans TC", sans-serif;
  -webkit-font-smoothing: antialiased; text-rendering: optimizeLegibility;
  font-feature-settings: "ss01" on, "cv11" on, "calt" on;
  -webkit-tap-highlight-color: transparent;
}
body {
  margin: 0; min-height: 100dvh; background: var(--canvas); color: var(--foreground);
  font-size: 14px; line-height: 1.5; letter-spacing: -0.005em;
}
::selection { background: var(--brand-soft); color: var(--foreground); }
a { color: inherit; }
:focus-visible { outline: 2px solid color-mix(in oklch, var(--ring) 45%, transparent); outline-offset: 2px; }

.frame { min-height: 100dvh; padding: 0; }
.panel { min-height: 100dvh; background: var(--background); }
@media (min-width: 768px) {
  .frame { padding: 8px; }
  .panel {
    min-height: calc(100dvh - 16px); border-radius: 10px;
    box-shadow: var(--shadow-edge), 0 0 0 1px color-mix(in oklch, var(--foreground) 6%, transparent);
  }
}
main { max-width: 880px; margin: 0 auto; padding: 28px 20px 64px; }
@media (min-width: 768px) { main { padding: 40px 40px 80px; } }

.eyebrow {
  font-size: 11px; font-weight: 500; text-transform: uppercase; letter-spacing: 0.08em;
  color: var(--muted-foreground); margin: 0 0 10px;
}
.folio, .mono {
  font-family: ui-monospace, "JetBrains Mono", "SF Mono", Menlo, "Cascadia Code", monospace;
  font-variant-numeric: tabular-nums; font-feature-settings: "tnum" on;
}
.folio { font-size: 10.5px; letter-spacing: 0.08em; color: var(--muted-foreground); }

header { margin-bottom: 28px; }
.heading { display: flex; align-items: baseline; gap: 10px; }
h1 {
  margin: 0; font-size: 21px; font-weight: 600; line-height: 1; letter-spacing: -0.015em;
  text-wrap: balance;
}
.lede { margin: 10px 0 0; max-width: 56ch; color: var(--muted-foreground); font-size: 13px; text-wrap: pretty; }

.notice {
  display: flex; gap: 10px; align-items: flex-start; margin: 0 0 20px; padding: 10px 12px;
  border: 1px solid var(--border); border-radius: 6px; background: var(--card); font-size: 13px;
}
.notice::before {
  content: ""; flex: none; width: 6px; height: 6px; margin-top: 7px; border-radius: 999px;
  background: var(--foreground);
}
.notice.err::before { background: var(--destructive); }
.notice pre {
  margin: 8px 0 0; max-height: 280px; overflow: auto; padding: 10px; border-radius: 6px;
  background: var(--muted); font-size: 11.5px; line-height: 1.55; white-space: pre-wrap; word-break: break-word;
}
.notice .body { min-width: 0; flex: 1; }

.list { list-style: none; margin: 0; padding: 0; border-top: 1px solid var(--hairline); }
.row {
  display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 6px 24px; align-items: center;
  padding: 14px 2px; border-bottom: 1px solid var(--hairline);
}
@media (max-width: 639px) {
  .row { grid-template-columns: minmax(0, 1fr); }
  .row .actions { justify-content: flex-start; }
}
.title {
  margin: 0; font-size: 14px; font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.title a { text-decoration: none; }
.title a:hover { text-decoration: underline; text-decoration-color: var(--hairline); text-underline-offset: 3px; }
.meta {
  display: flex; flex-wrap: wrap; align-items: center; gap: 4px 12px; margin-top: 3px;
  font-size: 12.5px; color: var(--muted-foreground);
}
.meta a { text-decoration: none; transition: color 100ms; }
.meta a:hover { color: var(--foreground); text-decoration: underline; text-underline-offset: 3px; }

.status { display: inline-flex; align-items: center; gap: 6px; color: var(--foreground); }
.dot { width: 6px; height: 6px; border-radius: 999px; flex: none; }
.s-published .dot { background: var(--foreground); }
.s-modified { color: var(--brand); }
.s-modified .dot { background: var(--brand); box-shadow: 0 0 0 3px var(--brand-soft); }
.s-draft { color: var(--muted-foreground); }
.s-draft .dot { box-shadow: inset 0 0 0 1px var(--muted-foreground); }
.s-orphaned { color: var(--destructive); }
.s-orphaned .dot { background: var(--destructive); }

.actions { display: flex; gap: 6px; justify-content: flex-end; }
form { margin: 0; }
.btn {
  position: relative; display: inline-flex; align-items: center; justify-content: center;
  height: 28px; padding: 0 10px; border-radius: 6px; border: 1px solid transparent;
  font: inherit; font-size: 13px; font-weight: 500; white-space: nowrap; cursor: pointer;
  user-select: none; touch-action: manipulation;
  transition: background-color 100ms, color 100ms, border-color 100ms, transform 100ms, filter 100ms;
}
.btn:active { transform: translateY(1px); }
.btn:disabled { opacity: 0.45; pointer-events: none; }
.btn-default {
  background: var(--foreground); color: var(--background);
  box-shadow: inset 0 1px 0 oklch(1 0 0 / 0.12), 0 1px 0 oklch(0 0 0 / 0.12);
}
.btn-default:hover { background: color-mix(in oklch, var(--foreground) 90%, transparent); }
.btn-brand {
  background: var(--brand); color: oklch(0.985 0 0);
  box-shadow: inset 0 1px 0 oklch(1 0 0 / 0.18), 0 1px 0 oklch(0 0 0 / 0.16);
}
.btn-brand:hover { filter: brightness(1.05); }
.btn-outline { background: var(--card); color: var(--foreground); border-color: var(--border); }
.btn-outline:hover {
  background: color-mix(in oklch, var(--muted) 60%, transparent);
  border-color: color-mix(in oklch, var(--foreground) 20%, transparent);
}

.empty {
  margin-top: 8px; padding: 64px 24px; border: 1px dashed var(--border); border-radius: 8px;
  text-align: center; color: var(--muted-foreground); font-size: 13px;
}

@keyframes rise-in { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: none; } }
.rise-in { animation: rise-in 350ms var(--ease-swift) both; }
@media (prefers-reduced-motion: reduce) {
  .rise-in { animation: none; }
  .btn, .meta a { transition: none; }
}
"""

SCRIPT = """
document.querySelectorAll("time[datetime]").forEach(function (el) {
  var then = new Date(el.getAttribute("datetime"));
  if (isNaN(then)) return;
  var secs = (then - Date.now()) / 1000, abs = Math.abs(secs);
  var units = [["year", 31536000], ["month", 2592000], ["day", 86400], ["hour", 3600], ["minute", 60]];
  var rtf = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  var text = rtf.format(0, "minute");
  for (var i = 0; i < units.length; i++) {
    if (abs >= units[i][1]) { text = rtf.format(Math.round(secs / units[i][1]), units[i][0]); break; }
  }
  el.textContent = text;
  el.title = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(then);
});
document.addEventListener("submit", function (event) {
  var form = event.target;
  var message = form.getAttribute("data-confirm");
  if (message && !window.confirm(message)) { event.preventDefault(); return; }
  document.querySelectorAll("form button").forEach(function (b) { b.disabled = true; });
  var button = form.querySelector("button");
  if (button) button.textContent = button.getAttribute("data-pending");
});
"""

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#f8f8f8" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#0b0b0b" media="(prefers-color-scheme: dark)">
<title>Publish · Stagedoor</title>
<link rel="preload" href="{base}/geist.woff2" as="font" type="font/woff2" crossorigin>
<style>{css}</style>
</head>
<body>
<div class="frame"><div class="panel">
<main>
<header>
<p class="eyebrow">Stagedoor</p>
<div class="heading"><h1>Publish</h1><span class="folio">{count}</span></div>
<p class="lede">Edits stay in the editor until you publish. Publishing snapshots a deck and rebuilds the public site.</p>
</header>
<div aria-live="polite">{notice}</div>
{decks}
</main>
</div></div>
<script>{script}</script>
</body>
</html>
"""


def _e(value: str) -> str:
    return html.escape(value, quote=True)


class App:
    def __init__(self, store: Store, base_path: str = "/publish", public_url: str = ""):
        self.store = store
        self.base = "/" + base_path.strip("/") if base_path.strip("/") else ""
        self.public_url = public_url  # e.g. "https://slides.example.com/s/{id}"

    def font(self) -> bytes | None:
        try:
            return (self.store.workspace / FONT_PATH).read_bytes()
        except OSError:
            return None

    def render(self, notice: str = "", kind: str = "ok", log: str = "") -> str:
        decks = self.store.decks()
        notice_html = ""
        if notice:
            body = f"<div>{_e(notice)}</div>"
            if log:
                body += f'<pre class="mono">{_e(log[-6000:])}</pre>'
            notice_html = f'<div class="notice {kind}" role="status"><div class="body">{body}</div></div>'
        if decks:
            rows = "\n".join(self._row(d, i) for i, d in enumerate(decks))
            decks_html = f'<ul class="list">{rows}</ul>'
        else:
            decks_html = (
                '<div class="empty">No decks yet. Create one in the editor, then come back here to publish it.</div>'
            )
        return PAGE.format(
            base=self.base,
            css=CSS.replace("{base}", self.base),
            script=SCRIPT,
            count=f"{len(decks):02d}",
            notice=notice_html,
            decks=decks_html,
        )

    def _row(self, deck, index: int) -> str:
        label, hint = STATUS[deck.status]
        meta = [
            f'<span class="status s-{deck.status}" title="{_e(hint)}">'
            f'<span class="dot" aria-hidden="true"></span>{_e(label)}</span>',
            f'<span class="mono" translate="no">{_e(deck.id)}</span>',
        ]
        if deck.published_at:
            meta.append(f'<span>Published <time datetime="{_e(deck.published_at)}">{_e(deck.published_at)}</time></span>')
        if self.public_url and deck.status != DRAFT:
            meta.append(f'<a href="{_e(self.public_url.format(id=quote(deck.id)))}">View Public ↗</a>')

        buttons = []
        if deck.status == DRAFT:
            buttons.append(self._button("publish", deck.id, "Publish", "Publishing…", "btn-default"))
        elif deck.status == MODIFIED:
            buttons.append(self._button("publish", deck.id, "Publish Changes", "Publishing…", "btn-brand"))
        if deck.status != DRAFT:
            confirm = f"Unpublish “{deck.title}”? It will disappear from the public site."
            buttons.append(
                self._button("unpublish", deck.id, "Unpublish", "Unpublishing…", "btn-outline", confirm)
            )
        title = _e(deck.title)
        if deck.status != ORPHANED:  # the title opens the deck in the editor
            title = f'<a href="/s/{quote(deck.id)}" title="Open in Editor">{title}</a>'
        delay = min(index, 8) * 30
        return (
            f'<li class="row rise-in" style="animation-delay:{delay}ms">'
            f'<div style="min-width:0"><p class="title">{title}</p>'
            f'<div class="meta">{"".join(meta)}</div></div>'
            f'<div class="actions">{"".join(buttons)}</div></li>'
        )

    def _button(self, action, deck_id, text, pending, variant, confirm="") -> str:
        confirm_attr = f' data-confirm="{_e(confirm)}"' if confirm else ""
        return (
            f'<form method="post" action="{self.base}/{action}"{confirm_attr}>'
            f'<input type="hidden" name="id" value="{_e(deck_id)}">'
            f'<button class="btn {variant}" data-pending="{_e(pending)}">{_e(text)}</button></form>'
        )


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "stagedoor"

        def do_GET(self):
            url = urlsplit(self.path)
            path = url.path.rstrip("/")
            if path == f"{app.base}/healthz":
                return self._send(HTTPStatus.OK, b"ok\n", "text/plain; charset=utf-8")
            if path == f"{app.base}/geist.woff2":
                font = app.font()
                if font is None:
                    return self._send(HTTPStatus.NOT_FOUND, b"", "text/plain")
                return self._send(HTTPStatus.OK, font, "font/woff2", cache="public, max-age=86400")
            if path != app.base:
                return self._send(HTTPStatus.NOT_FOUND, b"not found\n", "text/plain; charset=utf-8")
            notice = parse_qs(url.query).get("msg", [""])[0][:200]
            self._html(HTTPStatus.OK, app.render(notice))

        def do_POST(self):
            path = urlsplit(self.path).path
            action = path[len(app.base) + 1 :] if path.startswith(app.base + "/") else ""
            if action not in ("publish", "unpublish"):
                return self._send(HTTPStatus.NOT_FOUND, b"not found\n", "text/plain; charset=utf-8")
            if not self._same_origin():
                return self._send(HTTPStatus.FORBIDDEN, b"cross-origin request refused\n", "text/plain; charset=utf-8")
            length = min(int(self.headers.get("Content-Length") or 0), 4096)
            form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
            deck_id = form.get("id", [""])[0]
            try:
                getattr(app.store, action)(deck_id)
            except PublishError as exc:
                hint = " Fix the error below in the editor, then try again." if exc.log else ""
                return self._html(HTTPStatus.OK, app.render(str(exc) + hint, "err", exc.log))
            verb = "Published" if action == "publish" else "Unpublished"
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", f"{app.base}/?msg={quote(f'{verb} {deck_id}.')}")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _same_origin(self) -> bool:
            # Browsers always send Origin on form POSTs; refuse anything else
            # so a third-party page cannot publish through the user's session.
            origin = self.headers.get("Origin")
            host = self.headers.get("Host")
            if not origin or not host:
                return False
            return urlsplit(origin).netloc == host

        def _html(self, status, body: str):
            self._send(status, body.encode("utf-8"), "text/html; charset=utf-8")

        def _send(self, status, data: bytes, ctype: str, cache: str = "no-store"):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt, *args):  # quieter journal: method, path, status
            if args and isinstance(args[0], str) and "geist.woff2" in args[0]:
                return
            super().log_message(fmt, *args)

    return Handler


def serve(app: App, host: str, port: int) -> None:
    httpd = HTTPServer((host, port), make_handler(app))
    print(f"publisher listening on http://{host}:{port}{app.base}/", flush=True)
    httpd.serve_forever()
