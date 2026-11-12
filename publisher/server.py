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

from .history import AUTOSAVE, PUBLISH, RESTORE, REV_RE
from .store import DRAFT, LIVE, MODIFIED, ORPHANED, PAUSED, SLIDE_ID_RE, PublishError, Store

FONT_PATH = Path("node_modules/@fontsource-variable/geist/files/geist-latin-wght-normal.woff2")

ACTIONS = {
    # action: (verb for the confirmation notice, extra sentence)
    "publish": ("Published", ""),
    "unpublish": ("Unpublished", ""),
    "pause": ("Paused", " Its public copy stays as it is until you resume."),
    "resume": ("Resumed", " Edits will go public automatically again."),
}


def describe(deck, delay: int) -> tuple[str, str, str]:
    """(css state, label, explanation) for a deck's status line."""
    if deck.status == DRAFT:
        return "draft", "Draft", "Only visible in the editor."
    if deck.status == ORPHANED:
        return "orphaned", "Deleted in editor", "Gone from the editor but still public."
    if deck.mode == PAUSED:
        if deck.status == MODIFIED:
            return "pending", "Paused · unpublished changes", "The public copy is frozen. Publish to push these edits once."
        return "paused", "Paused", "The public copy is frozen until you resume."
    if deck.status == MODIFIED:
        if deck.sync_error:
            return "failed", "Sync failed", deck.sync_error + " Edit the deck to retry, or use Sync Now to see the log."
        return "syncing", "Live · syncing", f"Edits go public about {_duration(delay)} after you stop editing."
    return "live", "Live", "The public copy follows the editor."


def _duration(seconds: int) -> str:
    if seconds % 60 == 0 and seconds >= 60:
        minutes = seconds // 60
        return "a minute" if minutes == 1 else f"{minutes} minutes"
    return f"{seconds} seconds"

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
  --shadow-overlay: 0 0 0 0.5px oklch(0 0 0 / 0.1), 0 8px 28px -4px oklch(0 0 0 / 0.18),
    0 24px 64px -12px oklch(0 0 0 / 0.2);
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
.s-live .dot { background: var(--foreground); }
.s-syncing .dot { background: var(--foreground); animation: pulse 1.6s var(--ease-swift) infinite; }
.s-paused .dot { box-shadow: inset 0 0 0 1.5px var(--foreground); }
.s-pending { color: var(--brand); }
.s-pending .dot { background: var(--brand); box-shadow: 0 0 0 3px var(--brand-soft); }
.s-draft { color: var(--muted-foreground); }
.s-draft .dot { box-shadow: inset 0 0 0 1px var(--muted-foreground); }
.s-failed, .s-orphaned { color: var(--destructive); }
.s-failed .dot, .s-orphaned .dot { background: var(--destructive); }
@keyframes pulse { 50% { opacity: 0.25; } }

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

/* back link, badge, version rows: open-slide's ghost link + outline badge */
.back {
  display: inline-flex; align-items: center; gap: 4px; margin: -4px 0 14px -6px; padding: 2px 6px;
  border-radius: 6px; color: var(--muted-foreground); font-size: 12.5px; text-decoration: none;
  transition: background-color 100ms, color 100ms;
}
.back:hover { background: var(--muted); color: var(--foreground); }
.badge {
  display: inline-flex; align-items: center; height: 20px; padding: 0 8px; border-radius: 999px;
  border: 1px solid var(--border); font-size: 12px; font-weight: 500; color: var(--foreground);
  white-space: nowrap;
}
.badge-solid { background: var(--foreground); border-color: var(--foreground); color: var(--background); }
.day { list-style: none; padding: 22px 2px 8px; border-bottom: 1px solid var(--hairline); margin: 0; }
.list > .day:first-child { padding-top: 4px; }
.when { font-size: 14px; font-weight: 500; font-variant-numeric: tabular-nums; }
.kind { color: var(--muted-foreground); font-weight: 400; margin-left: 8px; }
.delta .add { color: var(--foreground); } .delta .del { color: var(--muted-foreground); }
.hint { margin: 0 0 16px; font-size: 12.5px; color: var(--muted-foreground); }
.notice .inline { display: inline; margin-left: 8px; }
.notice .inline .btn { height: 24px; padding: 0 8px; font-size: 12.5px; }
.btn-destructive {
  background: var(--destructive); color: #fff;
  box-shadow: inset 0 1px 0 oklch(1 0 0 / 0.16), 0 1px 0 oklch(0 0 0 / 0.12);
}
.btn-destructive:hover { filter: brightness(1.05); }

/* confirmation dialog: open-slide's Dialog (overlay foreground/35 + 2px blur,
   10px card, shadow-overlay, scale-95 fade), centred so origin stays center */
dialog.confirm {
  width: min(420px, calc(100% - 2rem)); padding: 24px; border: 1px solid var(--border);
  border-radius: 10px; background: var(--card); color: var(--foreground); box-shadow: var(--shadow-overlay);
  opacity: 1; transform: scale(1);
  transition: opacity 150ms var(--ease-swift), transform 150ms var(--ease-swift),
    overlay 150ms allow-discrete, display 150ms allow-discrete;
}
@starting-style { dialog.confirm[open] { opacity: 0; transform: scale(0.95); } }
dialog.confirm::backdrop {
  background: color-mix(in oklch, var(--foreground) 35%, transparent); backdrop-filter: blur(2px);
}
dialog.confirm h2 { margin: 0 0 6px; font-size: 15px; font-weight: 600; letter-spacing: -0.01em; }
dialog.confirm p { margin: 0; font-size: 13px; color: var(--muted-foreground); text-wrap: pretty; }
dialog.confirm .footer { display: flex; justify-content: flex-end; gap: 8px; margin-top: 20px; }

@keyframes rise-in { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: none; } }
.rise-in { animation: rise-in 350ms var(--ease-swift) both; }
@media (prefers-reduced-motion: reduce) {
  .rise-in, .s-syncing .dot { animation: none; }
  .btn, .meta a, .back { transition: none; }
  dialog.confirm { transform: none; transition: opacity 150ms, overlay 150ms allow-discrete, display 150ms allow-discrete; }
  @starting-style { dialog.confirm[open] { transform: none; } }
}
"""

SCRIPT = """
var timeFmt = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" });
var longFmt = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });
var dayFmt = new Intl.DateTimeFormat(undefined, { weekday: "long", month: "long", day: "numeric" });
var relDay = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
function dayKey(d) { return d.getFullYear() + "-" + d.getMonth() + "-" + d.getDate(); }
var lastDay = null;
document.querySelectorAll("li[data-at]").forEach(function (row) {
  // Group versions under a local-time day heading.
  var at = new Date(row.getAttribute("data-at"));
  if (isNaN(at) || dayKey(at) === lastDay) return;
  lastDay = dayKey(at);
  var days = Math.round((new Date(at.toDateString()) - new Date(new Date().toDateString())) / 86400000);
  var label = days >= -1 ? relDay.format(days, "day") : dayFmt.format(at);
  var head = document.createElement("li");
  head.className = "day eyebrow";
  head.textContent = label;
  row.parentNode.insertBefore(head, row);
});
document.querySelectorAll("time[data-format=clock]").forEach(function (el) {
  var then = new Date(el.getAttribute("datetime"));
  if (isNaN(then)) return;
  el.textContent = timeFmt.format(then);
  el.title = longFmt.format(then);
});
document.querySelectorAll("time[datetime]:not([data-format])").forEach(function (el) {
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
  el.title = longFmt.format(then);
});
var submitting = false;
if (document.querySelector(".s-syncing")) {
  // A background sync is due; refresh to show it once it lands.
  setTimeout(function () { if (!submitting) location.replace(location.pathname); }, 15000);
}
var dialog = document.getElementById("confirm");
var pendingForm = null;
if (dialog) {
  dialog.addEventListener("close", function () {
    var form = pendingForm;
    pendingForm = null;
    if (!form) return;
    if (dialog.returnValue === "confirm") form.requestSubmit();
    else form.confirmed = false;  // cancelled, Escape or backdrop: ask again next time
  });
}
document.addEventListener("submit", function (event) {
  var form = event.target;
  if (form.method === "dialog") return;  // the dialog's own Cancel/Confirm buttons
  var title = form.getAttribute("data-confirm-title");
  if (title && dialog && !form.confirmed) {
    // Ask first, in open-slide's dialog rather than the browser's confirm().
    event.preventDefault();
    pendingForm = form;
    form.confirmed = true;
    dialog.querySelector("h2").textContent = title;
    dialog.querySelector("p").textContent = form.getAttribute("data-confirm-body") || "";
    var ok = dialog.querySelector("[value=confirm]");
    ok.textContent = form.getAttribute("data-confirm-action") || "Confirm";
    ok.className = "btn " + (form.getAttribute("data-confirm-variant") || "btn-default");
    dialog.returnValue = "";
    dialog.showModal();
    return;
  }
  submitting = true;
  document.querySelectorAll("form button").forEach(function (b) { b.disabled = true; });
  var button = form.querySelector("button");
  if (button && button.getAttribute("data-pending")) button.textContent = button.getAttribute("data-pending");
});

"""

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#f8f8f8" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#0b0b0b" media="(prefers-color-scheme: dark)">
<title>{title} · Stagedoor</title>
<link rel="preload" href="{base}/geist.woff2" as="font" type="font/woff2" crossorigin>
<style>{css}</style>
</head>
<body>
<div class="frame"><div class="panel">
<main>
<header>
{back}<p class="eyebrow">Stagedoor</p>
<div class="heading"><h1>{heading}</h1><span class="folio">{count}</span></div>
<p class="lede">{lede}</p>
</header>
<div aria-live="polite">{notice}</div>
{body}
</main>
</div></div>
<dialog id="confirm" class="confirm" aria-labelledby="confirm-title">
<form method="dialog">
<h2 id="confirm-title"></h2>
<p></p>
<div class="footer">
<button class="btn btn-outline" value="cancel" autofocus>Cancel</button>
<button class="btn btn-default" value="confirm"></button>
</div>
</form>
</dialog>
<script>{script}</script>
</body>
</html>
"""


def _e(value: str) -> str:
    return html.escape(value, quote=True)


class App:
    def __init__(self, store: Store, base_path: str = "/publish", public_url: str = "", sync_delay: int = 60):
        self.store = store
        self.base = "/" + base_path.strip("/") if base_path.strip("/") else ""
        self.public_url = public_url  # e.g. "https://slides.example.com/s/{id}"
        self.sync_delay = sync_delay

    def font(self) -> bytes | None:
        try:
            return (self.store.workspace / FONT_PATH).read_bytes()
        except OSError:
            return None

    def has_history(self) -> bool:
        return bool(self.store.history and self.store.history.available())

    # ---- pages ----------------------------------------------------------

    def render(self, notice: str = "", kind: str = "ok", log: str = "") -> str:
        decks = self.store.decks()
        if decks:
            rows = "\n".join(self._row(d, i) for i, d in enumerate(decks))
            body = f'<ul class="list">{rows}</ul>'
        else:
            body = '<div class="empty">No decks yet. Create one in the editor, then come back here to publish it.</div>'
        return self._page(
            title="Publish",
            heading="Publish",
            count=len(decks),
            lede=(
                "Drafts stay in the editor. Once published, a deck follows your edits about "
                f"{_duration(self.sync_delay)} after you stop editing; pause it to freeze the public copy."
            ),
            notice=self._notice(notice, kind, log),
            body=body,
        )

    def render_history(self, deck_id: str, notice: str = "", kind: str = "ok", undo: str = "") -> str | None:
        deck = next((d for d in self.store.decks() if d.id == deck_id), None)
        history = self.store.history
        versions = history.versions(deck_id) if self.has_history() else []
        if deck is None and not versions:
            return None
        title = deck.title if deck else deck_id
        public = self.store.public_rev(deck_id)
        unsaved = self.has_history() and history.has_unsaved(deck_id)
        current = versions[0].rev if versions and not unsaved else None

        notice_html = self._notice(notice, kind)
        if notice and kind == "ok" and undo and any(v.rev.startswith(undo) for v in versions):
            undo_form = self._form(
                "restore", deck_id, "Undo", "Undoing…", "btn-outline", extra={"rev": undo}, inline=True
            )
            notice_html = notice_html.replace("</div></div>", f"{undo_form}</div></div>", 1)

        if not self.has_history():
            body = (
                '<div class="empty">Version history needs the workspace to be a git repository. '
                "Run <code>git init</code> in it and enable the autosave service.</div>"
            )
        elif not versions:
            body = '<div class="empty">No versions yet. A version is saved shortly after you edit this deck.</div>'
        else:
            rows = "\n".join(
                self._version_row(deck, deck_id, title, v, current, public, i) for i, v in enumerate(versions)
            )
            body = f'<ul class="list">{rows}</ul>'
        if unsaved:
            body = (
                '<p class="hint">Your latest edits aren’t in history yet; they’re saved about 30 seconds '
                "after you stop editing.</p>" + body
            )
        back = f'<a class="back" href="{self.base}/">← Publish</a>'
        return self._page(
            title=f"History · {title}",
            heading=_e(title),
            count=len(versions),
            lede=(
                "Every edit session, publish and restore is kept here. Restoring puts an earlier version "
                "back in the editor and keeps the current one, so you can always undo."
            ),
            notice=notice_html,
            body=body,
            back=back,
        )

    # ---- pieces ---------------------------------------------------------

    def _page(self, title, heading, count, lede, notice, body, back="") -> str:
        return PAGE.format(
            base=self.base,
            css=CSS.replace("{base}", self.base),
            script=SCRIPT,
            title=_e(title),
            heading=heading,
            count=f"{count:02d}",
            lede=_e(lede),
            notice=notice,
            body=body,
            back=back,
        )

    @staticmethod
    def _notice(notice: str, kind: str = "ok", log: str = "") -> str:
        if not notice:
            return ""
        body = f"<div>{_e(notice)}</div>"
        if log:
            body += f'<pre class="mono">{_e(log[-6000:])}</pre>'
        return f'<div class="notice {kind}" role="status"><div class="body">{body}</div></div>'

    def _row(self, deck, index: int) -> str:
        state, label, hint = describe(deck, self.sync_delay)
        meta = [
            f'<span class="status s-{state}" title="{_e(hint)}">'
            f'<span class="dot" aria-hidden="true"></span>{_e(label)}</span>',
            f'<span class="mono" translate="no">{_e(deck.id)}</span>',
        ]
        if deck.published_at:
            meta.append(f'<span>Published <time datetime="{_e(deck.published_at)}">{_e(deck.published_at)}</time></span>')
        if self.public_url and deck.status != DRAFT:
            meta.append(f'<a href="{_e(self.public_url.format(id=quote(deck.id)))}">View Public ↗</a>')
        if self.has_history():
            meta.append(f'<a href="{self.base}/history/{quote(deck.id)}">History</a>')

        buttons = []
        if deck.status == DRAFT:
            buttons.append(self._form("publish", deck.id, "Publish", "Publishing…", "btn-default"))
        elif deck.mode == PAUSED:
            if deck.status == MODIFIED:
                buttons.append(self._form("publish", deck.id, "Publish Changes", "Publishing…", "btn-brand"))
            buttons.append(self._form("resume", deck.id, "Resume", "Resuming…", "btn-outline"))
        elif deck.mode == LIVE and deck.status != ORPHANED:
            if deck.status == MODIFIED:
                buttons.append(self._form("publish", deck.id, "Sync Now", "Syncing…", "btn-default"))
            buttons.append(self._form("pause", deck.id, "Pause", "Pausing…", "btn-outline"))
        if deck.status != DRAFT:
            buttons.append(
                self._form(
                    "unpublish", deck.id, "Unpublish", "Unpublishing…", "btn-outline",
                    confirm=(
                        f"Unpublish “{deck.title}”?",
                        "It disappears from the public site. The deck and its history stay in the editor.",
                        "Unpublish",
                        "btn-destructive",
                    ),
                )
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

    def _version_row(self, deck, deck_id, title, version, current, public, index) -> str:
        kind = {
            AUTOSAVE: "Auto-saved",
            PUBLISH: "Published",
            RESTORE: f"Restored from {version.restored_from}" if version.restored_from else "Restored",
        }.get(version.kind, version.subject[:80])
        badges = []
        if version.rev == current:
            badges.append('<span class="badge">Current</span>')
        if public and version.rev == public:
            badges.append('<span class="badge badge-solid">Public</span>')
        delta = (
            f'<span class="mono delta" title="Lines added and removed">'
            f'<span class="add">+{version.added}</span> <span class="del">−{version.removed}</span></span>'
        )
        meta = [f'<span class="mono" translate="no">{version.short}</span>', delta, *badges]
        actions = ""
        if version.rev != current:
            if deck is None:
                public_note = ""
            elif deck.mode == LIVE:
                public_note = f" The deck is live, so the public site follows about {_duration(self.sync_delay)} later."
            elif deck.mode == PAUSED:
                public_note = " The deck is paused, so the public site stays as it is until you publish."
            else:
                public_note = ""
            actions = self._form(
                "restore", deck_id, "Restore", "Restoring…", "btn-outline", extra={"rev": version.rev},
                confirm=(
                    "Restore this version?",
                    f"“{title}” goes back to how it was at this point. What’s in the editor now stays in "
                    f"history, so you can undo this.{public_note}",
                    "Restore",
                    "btn-default",
                ),
            )
        delay = min(index, 8) * 30
        return (
            f'<li class="row version rise-in" data-at="{_e(version.at)}" style="animation-delay:{delay}ms">'
            f'<div style="min-width:0"><p class="title">'
            f'<time class="when" datetime="{_e(version.at)}" data-format="clock">{_e(version.at[:16].replace("T", " "))}</time>'
            f'<span class="kind">{_e(kind)}</span></p>'
            f'<div class="meta">{"".join(meta)}</div></div>'
            f'<div class="actions">{actions}</div></li>'
        )

    def _form(self, action, deck_id, text, pending, variant, extra=None, confirm=None, inline=False) -> str:
        attrs = ""
        if confirm:
            c_title, c_body, c_action, c_variant = confirm
            attrs = (
                f' data-confirm-title="{_e(c_title)}" data-confirm-body="{_e(c_body)}"'
                f' data-confirm-action="{_e(c_action)}" data-confirm-variant="{_e(c_variant)}"'
            )
        if inline:
            attrs += ' class="inline"'
        hidden = f'<input type="hidden" name="id" value="{_e(deck_id)}">'
        for name, value in (extra or {}).items():
            hidden += f'<input type="hidden" name="{_e(name)}" value="{_e(value)}">'
        return (
            f'<form method="post" action="{self.base}/{action}"{attrs}>{hidden}'
            f'<button class="btn {variant}" data-pending="{_e(pending)}">{_e(text)}</button></form>'
        )


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "stagedoor"

        def do_GET(self):
            url = urlsplit(self.path)
            path = url.path.rstrip("/")
            query = parse_qs(url.query)
            notice = query.get("msg", [""])[0][:200]
            if path == f"{app.base}/healthz":
                return self._send(HTTPStatus.OK, b"ok\n", "text/plain; charset=utf-8")
            if path == f"{app.base}/geist.woff2":
                font = app.font()
                if font is None:
                    return self._send(HTTPStatus.NOT_FOUND, b"", "text/plain")
                return self._send(HTTPStatus.OK, font, "font/woff2", cache="public, max-age=86400")
            prefix = f"{app.base}/history/"
            if path.startswith(prefix) and SLIDE_ID_RE.match(path[len(prefix):]):
                undo = query.get("undo", [""])[0]
                page = app.render_history(path[len(prefix):], notice, undo=undo if REV_RE.match(undo) else "")
                if page is None:
                    return self._send(HTTPStatus.NOT_FOUND, b"no such deck\n", "text/plain; charset=utf-8")
                return self._html(HTTPStatus.OK, page)
            if path != app.base:
                return self._send(HTTPStatus.NOT_FOUND, b"not found\n", "text/plain; charset=utf-8")
            self._html(HTTPStatus.OK, app.render(notice))

        def do_POST(self):
            path = urlsplit(self.path).path
            action = path[len(app.base) + 1 :] if path.startswith(app.base + "/") else ""
            if action not in ACTIONS and action != "restore":
                return self._send(HTTPStatus.NOT_FOUND, b"not found\n", "text/plain; charset=utf-8")
            if not self._same_origin():
                return self._send(HTTPStatus.FORBIDDEN, b"cross-origin request refused\n", "text/plain; charset=utf-8")
            length = min(int(self.headers.get("Content-Length") or 0), 4096)
            form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
            deck_id = form.get("id", [""])[0]
            if action == "restore":
                return self._restore(deck_id, form.get("rev", [""])[0])
            try:
                getattr(app.store, action)(deck_id)
            except PublishError as exc:
                hint = " Fix the error below in the editor, then try again." if exc.log else ""
                return self._html(HTTPStatus.OK, app.render(str(exc) + hint, "err", exc.log))
            verb, extra = ACTIONS[action]
            self._redirect(f"{app.base}/?msg={quote(f'{verb} {deck_id}.{extra}')}")

        def _restore(self, deck_id: str, rev: str):
            try:
                _, undo = app.store.restore(deck_id, rev)
            except PublishError as exc:
                page = app.render_history(deck_id, f"{exc} Nothing was changed.", "err")
                if page is None:
                    return self._send(HTTPStatus.NOT_FOUND, b"no such deck\n", "text/plain; charset=utf-8")
                return self._html(HTTPStatus.OK, page)
            msg = quote(f"Restored {deck_id} to {rev[:7]}.")
            undo_q = f"&undo={undo[:12]}" if undo else ""
            self._redirect(f"{app.base}/history/{quote(deck_id)}?msg={msg}{undo_q}")

        def _redirect(self, location: str):
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", location)
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
