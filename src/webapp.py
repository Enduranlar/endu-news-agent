"""Minimal web admin for reviewing source suggestions (run: `python -m src.main web`).

Lists pending Instagram + website suggestions — each clickable to inspect the
profile/site in a new tab — with **Ekle** (approve) and **Yoksay** (dismiss)
buttons, plus forms to manually add an IG handle or a website. Approvals append
to config/igaccounts.md or config/websites.md (websites get RSS auto-detection),
exactly like the CLI `approve` / `add-site` flows.

No external dependencies (stdlib http.server), single-threaded — fine for one
operator. It has NO authentication: bind it to your Tailscale IP via
WEB_LISTEN_HOST so it's only reachable over your tailnet. Never bind 0.0.0.0 on a
public VPS.
"""

from __future__ import annotations

import html
import logging
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlparse

from . import settings
from .config_loader import _normalize_handle, append_ig_account
from .feed_detect import add_website
from .store import Store

log = logging.getLogger("agent.web")


# --- HTML rendering ----------------------------------------------------------

_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body {
  margin: 0; padding: 16px;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica,
    Arial, sans-serif;
  background: #f4f5f7; color: #1a1a1a; line-height: 1.45;
}
.wrap { max-width: 760px; margin: 0 auto; }
h1 { font-size: 20px; margin: 4px 0 2px; }
.sub { color: #777; font-size: 13px; margin: 0 0 16px; }
h2 { font-size: 15px; margin: 24px 0 10px; color: #333;
  border-bottom: 1px solid #e3e3e3; padding-bottom: 6px; }
.flash { background: #e7f6ec; border: 1px solid #b6e0c2; color: #1c6b3a;
  padding: 10px 12px; border-radius: 8px; margin-bottom: 16px; font-size: 14px; }
.flash.err { background: #fdeaea; border-color: #f3b6b6; color: #9b1c1c; }
.card { background: #fff; border: 1px solid #e3e3e3; border-radius: 10px;
  padding: 14px; margin-bottom: 12px; }
.card .title { font-size: 16px; font-weight: 600; word-break: break-all; }
.card a.title { color: #0b66c3; text-decoration: none; }
.card a.title:hover { text-decoration: underline; }
.badge { display: inline-block; font-size: 11px; font-weight: 600; color: #fff;
  background: #6b7280; border-radius: 999px; padding: 1px 8px; margin-left: 6px;
  vertical-align: middle; }
.badge.ig { background: #c13584; }
.badge.site, .badge.rss { background: #2563eb; }
.reason { margin: 8px 0 4px; font-size: 14px; }
.meta { color: #888; font-size: 12px; margin-bottom: 10px; }
.actions { display: flex; gap: 8px; flex-wrap: wrap; }
.btn { display: inline-block; border: 0; border-radius: 8px; padding: 10px 16px;
  font-size: 14px; font-weight: 600; cursor: pointer; text-align: center; }
.btn-add { background: #16a34a; color: #fff; }
.btn-dismiss { background: #e5e7eb; color: #374151; }
.btn:active { transform: translateY(1px); }
form.inline { margin: 0; }
.empty { color: #888; font-style: italic; padding: 8px 0; }
.add-grid { display: grid; gap: 10px; }
.add-card label { display:block; font-size: 13px; color:#555; margin-bottom:4px; }
.add-card input[type=text] { width: 100%; padding: 10px; font-size: 15px;
  border: 1px solid #cfd4da; border-radius: 8px; background: #fff; color: #111; }
.add-card .row { margin-bottom: 10px; }
@media (min-width: 620px) {
  .actions .btn { min-width: 110px; }
}
"""


def _esc(s: object) -> str:
    return html.escape(str(s if s is not None else ""))


def _suggestion_link_title(s) -> tuple[str, str]:
    if s["kind"] == "ig":
        return f"https://www.instagram.com/{s['key']}/", f"@{s['key']}"
    return s["key"], s["key"]


def render_page(suggestions, message: str = "", is_error: bool = False) -> str:
    flash = ""
    if message:
        cls = "flash err" if is_error else "flash"
        flash = f'<div class="{cls}">{_esc(message)}</div>'

    cards = []
    for s in suggestions:
        link, title = _suggestion_link_title(s)
        badge = s["kind"]
        meta_bits = []
        if s["signal"]:
            meta_bits.append(_esc(s["signal"]))
        if s["discovered_via"]:
            meta_bits.append("kaynak: " + _esc(s["discovered_via"]))
        if s["created_at"]:
            meta_bits.append(_esc(str(s["created_at"])[:10]))
        meta = " · ".join(meta_bits)
        cards.append(
            f"""
        <div class="card">
          <div>
            <a class="title" href="{_esc(link)}" target="_blank" rel="noopener">{_esc(title)}</a>
            <span class="badge {badge}">{_esc(badge)}</span>
          </div>
          <div class="reason">{_esc(s['reason'])}</div>
          <div class="meta">{meta}</div>
          <div class="actions">
            <form class="inline" method="post" action="/approve">
              <input type="hidden" name="id" value="{_esc(s['id'])}">
              <input type="hidden" name="kind" value="{_esc(s['kind'])}">
              <input type="hidden" name="key" value="{_esc(s['key'])}">
              <button class="btn btn-add" type="submit">Ekle</button>
            </form>
            <form class="inline" method="post" action="/dismiss">
              <input type="hidden" name="id" value="{_esc(s['id'])}">
              <button class="btn btn-dismiss" type="submit">Yoksay</button>
            </form>
          </div>
        </div>"""
        )

    suggestions_html = (
        "\n".join(cards)
        if cards
        else '<div class="empty">Bekleyen öneri yok.</div>'
    )

    return f"""<!DOCTYPE html>
<html lang="tr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Endu — Kaynak Önerileri</title>
  <style>{_CSS}</style>
</head>
<body>
  <div class="wrap">
    <h1>Kaynak Önerileri</h1>
    <p class="sub">Bekleyen öneriler: {len(suggestions)} · ekle/yoksay ya da manuel ekle</p>
    {flash}

    <h2>Bekleyen öneriler</h2>
    {suggestions_html}

    <h2>Manuel ekle</h2>
    <div class="add-grid">
      <div class="card add-card">
        <form method="post" action="/add-ig">
          <div class="row">
            <label>Instagram hesabı (kullanıcı adı veya profil linki)</label>
            <input type="text" name="handle" placeholder="ornek_hesap" required>
          </div>
          <div class="row">
            <label>Not (opsiyonel)</label>
            <input type="text" name="note" placeholder="ör. trail haberleri">
          </div>
          <button class="btn btn-add" type="submit">Instagram ekle</button>
        </form>
      </div>
      <div class="card add-card">
        <form method="post" action="/add-site">
          <div class="row">
            <label>Web sitesi (RSS otomatik algılanır)</label>
            <input type="text" name="url" placeholder="https://ornek.com" required>
          </div>
          <div class="row">
            <label>Not (opsiyonel)</label>
            <input type="text" name="note" placeholder="ör. koşu haberleri">
          </div>
          <button class="btn btn-add" type="submit">Web sitesi ekle</button>
        </form>
      </div>
    </div>
  </div>
</body>
</html>"""


# --- Server ------------------------------------------------------------------


class AdminServer(HTTPServer):
    def __init__(self, addr, handler, proxy: Optional[str]):
        super().__init__(addr, handler)
        self.proxy = proxy


class Handler(BaseHTTPRequestHandler):
    server_version = "EnduAdmin/1.0"

    # Quieter logging — route through our logger at debug level.
    def log_message(self, fmt, *args):
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _redirect(self, message: str = "", is_error: bool = False) -> None:
        params = {}
        if message:
            params["msg"] = message
            if is_error:
                params["err"] = "1"
        location = "/" + (("?" + urlencode(params)) if params else "")
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def _html(self, body: str, status: int = 200) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path not in ("/", ""):
            self._html("<h1>404</h1>", status=404)
            return
        q = parse_qs(parsed.query)
        message = q.get("msg", [""])[0]
        is_error = q.get("err", [""])[0] == "1"
        with Store() as store:
            suggestions = store.pending_suggestions()
            self._html(render_page(suggestions, message, is_error))

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length).decode("utf-8") if length else ""
        fields = parse_qs(body)

        def f(name: str, default: str = "") -> str:
            return fields.get(name, [default])[0].strip()

        try:
            with Store() as store:
                msg = self._dispatch(self.path, f, store)
            self._redirect(msg)
        except Exception as exc:  # noqa: BLE001 — surface as a flash, never 500-crash
            log.error("admin action failed:\n%s", traceback.format_exc())
            self._redirect(f"Hata: {exc}", is_error=True)

    def _dispatch(self, path: str, f, store: Store) -> str:
        proxy = self.server.proxy  # type: ignore[attr-defined]
        path = urlparse(path).path

        if path == "/dismiss":
            sid = int(f("id"))
            store.set_suggestion_status(sid, "dismissed")
            return "Öneri yoksayıldı."

        if path == "/approve":
            kind = f("kind")
            key = f("key")
            if kind == "ig":
                handle = _normalize_handle(key)
                added = append_ig_account(handle)
                store.set_suggestion_status_by_key("ig", handle, "approved")
                return (
                    f"@{handle} eklendi (igaccounts.md)."
                    if added
                    else f"@{handle} zaten ekli; öneri onaylandı."
                )
            # website suggestion → RSS auto-detection
            res = add_website(key, proxy=proxy)
            store.set_suggestion_status_by_key("site", key, "approved")
            store.set_suggestion_status_by_key("rss", key, "approved")
            kind_label = "RSS" if res.detection.kind == "rss" else "site"
            return f"{key} eklendi ({kind_label})."

        if path == "/add-ig":
            handle = _normalize_handle(f("handle"))
            if not handle:
                return "Geçersiz Instagram kullanıcı adı."
            added = append_ig_account(handle, f("note"))
            store.set_suggestion_status_by_key("ig", handle, "approved")
            return (
                f"@{handle} eklendi (igaccounts.md)."
                if added
                else f"@{handle} zaten ekli."
            )

        if path == "/add-site":
            url = f("url")
            if not url:
                return "Geçersiz URL."
            res = add_website(url, note=f("note"), proxy=proxy)
            host = (urlparse(res.detection.site_url).hostname or "").lstrip("www.")
            if host:
                store.set_suggestion_status_by_key("site", f"https://{host}", "approved")
            kind_label = "RSS" if res.detection.kind == "rss" else "site"
            verb = "eklendi" if res.added else "zaten ekli"
            return f"{res.detection.site_url} {verb} ({kind_label})."

        return "Bilinmeyen işlem."


def serve(host: str, port: int, proxy: Optional[str] = None) -> None:
    settings.ensure_dirs()
    server = AdminServer((host, port), Handler, proxy)
    url = f"http://{host}:{port}"
    log.info("web admin listening on %s", url)
    print(f"Kaynak önerileri arayüzü: {url}  (durdurmak için Ctrl-C)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDurduruldu.")
    finally:
        server.server_close()
