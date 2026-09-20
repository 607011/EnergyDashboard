"""Tiny web form for entering the heat pump's electricity meter reading by hand.

The heat pump's meter (ORNO OR-WE-520) has only a pulse output, so until a meter with a
proper interface is installed, someone reads it off the display now and then. This form
writes the reading into RedisTimeSeries; weishaupt-poller derives the electricity use and
the seasonal performance factor (JAZ) from those readings.

There is no login of its own: it is meant to sit behind Caddy, which lets only logged-in
Grafana users through (see caddy/Caddyfile). All links are relative so the form works under
any path prefix.
"""

import html
import logging
import os
import re
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse
from zoneinfo import ZoneInfo

import redis

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("meter-form")

DEVICE = os.environ.get("WEISHAUPT_NAME", "wgb14")
TZ = ZoneInfo(os.environ.get("TIMEZONE", "UTC"))
PORT = int(os.environ.get("PORT", "8000"))
KEY = f"ts:weishaupt:{DEVICE}:electric_reading_kwh"
LATEST = f"weishaupt:{DEVICE}:latest"

r = redis.Redis(
    host=os.environ.get("REDIS_HOST", "redis"),
    port=int(os.environ.get("REDIS_PORT", "6379")),
    password=os.environ.get("REDIS_PASSWORD") or None,
    decode_responses=True,
)

PAGE = """<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Stromzähler ablesen</title>
<style>
 body{font:16px system-ui,sans-serif;margin:0;background:#f4f5f7;color:#1c1e21}
 main{max-width:34rem;margin:0 auto;padding:1rem 1rem 3rem}
 h1{font-size:1.4rem;margin:.5rem 0 1rem}
 form,section{background:#fff;border-radius:10px;padding:1rem;margin-bottom:1rem;box-shadow:0 1px 3px #0002}
 label{display:block;font-weight:600;margin:.8rem 0 .3rem}
 input[type=text],input[type=datetime-local]{width:100%;box-sizing:border-box;font-size:1.3rem;padding:.6rem;border:1px solid #bbb;border-radius:8px}
 button{margin-top:1rem;width:100%;font-size:1.1rem;padding:.8rem;border:0;border-radius:8px;background:#2f7d32;color:#fff}
 .msg{padding:.8rem;border-radius:8px;margin-bottom:1rem}.ok{background:#e3f4e4}.err{background:#fde3e3}
 .hint{color:#666;font-size:.9rem}table{width:100%;border-collapse:collapse}td,th{padding:.3rem .2rem;text-align:right;border-bottom:1px solid #eee}
 th:first-child,td:first-child{text-align:left}
 .kpi{display:flex;gap:1rem;flex-wrap:wrap}.kpi div{flex:1 1 8rem}.kpi b{display:block;font-size:1.6rem}
</style></head><body><main>
<h1>Stromzähler Wärmepumpe</h1>
@@MESSAGE@@
<form method="post" action="./reading" id="f">
 <label for="kwh">Zählerstand (kWh)</label>
 <input id="kwh" name="kwh" type="text" inputmode="decimal" autocomplete="off" placeholder="z. B. 12345,6" required autofocus>
 <label for="at">Zeitpunkt der Ablesung <span class="hint">(voreingestellt: jetzt, änderbar)</span></label>
 <input id="at" name="at" type="datetime-local" value="@@NOW@@">
 <button type="submit">Speichern</button>
</form>
@@SUMMARY@@
@@HISTORY@@
</main>
<script>
 // Left untouched, the field means "right now": drop it so the server stamps the exact second.
 document.getElementById("f").addEventListener("submit", function () {
   var at = document.getElementById("at");
   if (at.value === at.defaultValue) at.disabled = true;
 });
</script></body></html>"""


def fmt(value, digits=1, unit=""):
    try:
        return f"{float(value):,.{digits}f}".replace(",", " ").replace(".", ",") + unit
    except (TypeError, ValueError):
        return "–"


def local_time(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, TZ).strftime("%d.%m.%Y %H:%M:%S")


def summary_html() -> str:
    h = r.hgetall(LATEST)
    if "jaz_since_first" not in h and "jaz_last_interval" not in h:
        return '<section class="hint">Die Jahresarbeitszahl erscheint ab der zweiten Ablesung, sobald der Wärmezähler der Wärmepumpe dazu Werte hat.</section>'
    return (
        '<section><div class="kpi">'
        f'<div>JAZ seit Beginn<b>{fmt(h.get("jaz_since_first"), 2)}</b></div>'
        f'<div>JAZ letzter Zeitraum<b>{fmt(h.get("jaz_last_interval"), 2)}</b></div>'
        f'<div>Strom pro Tag<b>{fmt(h.get("electric_kwh_per_day_last"), 1, " kWh")}</b></div>'
        "</div></section>"
    )


def history_html() -> str:
    try:
        rows = r.ts().revrange(KEY, "-", "+", count=10)
    except redis.ResponseError:
        rows = []
    if not rows:
        return ""
    body = []
    for i, (ts, value) in enumerate(rows):
        delta = fmt(value - rows[i + 1][1], 1) if i + 1 < len(rows) else "–"
        body.append(f"<tr><td>{local_time(ts)}</td><td>{fmt(value, 1)}</td><td>{delta}</td></tr>")
    return (
        "<section><table><tr><th>Ablesung</th><th>kWh</th><th>Δ kWh</th></tr>"
        + "".join(body)
        + "</table></section>"
    )


def page(message: str = "", ok: bool = True) -> bytes:
    msg = f'<div class="msg {"ok" if ok else "err"}">{html.escape(message)}</div>' if message else ""
    return (PAGE.replace("@@MESSAGE@@", msg)
            .replace("@@SUMMARY@@", summary_html())
            .replace("@@HISTORY@@", history_html())
            .replace("@@NOW@@", datetime.now(TZ).strftime("%Y-%m-%dT%H:%M"))).encode()


def save_reading(kwh_text: str, at_text: str) -> tuple[bool, str]:
    text = kwh_text.strip().replace(",", ".").replace(" ", "").replace(" ", "")
    if not re.fullmatch(r"\d{1,9}(\.\d{1,3})?", text):
        return False, "Bitte den Zählerstand als Zahl eingeben, z. B. 12345,6."
    kwh = float(text)

    now_ms = int(time.time() * 1000)
    if at_text:
        try:
            ts = int(datetime.strptime(at_text, "%Y-%m-%dT%H:%M").replace(tzinfo=TZ).timestamp() * 1000)
        except ValueError:
            return False, "Der Zeitpunkt ist ungültig."
        if ts > now_ms + 60_000:
            return False, "Der Zeitpunkt liegt in der Zukunft."
    else:
        ts = now_ms

    # A meter only counts up: the value must fit between its neighbours in time.
    try:
        before = r.ts().revrange(KEY, 0, ts, count=1)
        after = r.ts().range(KEY, ts + 1, "+", count=1)
    except redis.ResponseError:
        before, after = [], []
    if before and kwh < before[0][1]:
        return False, (f"{fmt(kwh)} kWh ist weniger als die Ablesung vom {local_time(before[0][0])} "
                       f"({fmt(before[0][1])} kWh). Zahlendreher?")
    if after and kwh > after[0][1]:
        return False, (f"{fmt(kwh)} kWh ist mehr als die spätere Ablesung vom {local_time(after[0][0])} "
                       f"({fmt(after[0][1])} kWh). Zahlendreher?")

    # Retention 0 = keep forever; readings are few and precious.
    r.ts().add(KEY, ts, kwh, retention_msecs=0, labels={"device": DEVICE, "field": "electric_reading_kwh"},
               duplicate_policy="last")
    log.info("Recorded reading %s kWh at %s", kwh, local_time(ts))
    return True, f"Gespeichert: {fmt(kwh)} kWh ({local_time(ts)})."


class Handler(BaseHTTPRequestHandler):
    server_version = "meter-form"

    def _send(self, status: int, body: bytes = b"", ctype: str = "text/html; charset=utf-8", headers=()):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/healthz":
            return self._send(200, b"ok", "text/plain")
        if url.path not in ("/", ""):
            return self._send(404, b"not found", "text/plain")
        q = parse_qs(url.query)
        self._send(200, page(q.get("m", [""])[0], q.get("ok", ["1"])[0] == "1"))

    def do_POST(self):
        if urlparse(self.path).path != "/reading":
            return self._send(404, b"not found", "text/plain")
        length = min(int(self.headers.get("Content-Length") or 0), 4096)
        form = parse_qs(self.rfile.read(length).decode())
        ok, message = save_reading(form.get("kwh", [""])[0], form.get("at", [""])[0])
        status = 200 if ok else 422
        # Post/Redirect/Get after success so a reload doesn't submit the reading twice.
        if ok:
            return self._send(303, headers=[("Location", f"./?ok=1&m={quote(message)}")])
        self._send(status, page(message, False))

    def log_message(self, fmt_, *args):
        log.debug("%s " + fmt_, self.address_string(), *args)


if __name__ == "__main__":
    r.ping()
    log.info("Listening on :%d (Redis ok, device %s)", PORT, DEVICE)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
