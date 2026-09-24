"""Browser push notifications (Web Push) for the load management's switching events.

compute-controller appends every switching it actually carries out to the Redis stream "notify"
(title, body, tag, url). This service delivers each entry to every browser that subscribed on
its page -- the home-screen web app on iPhone/iPad (iOS 16.4+ only offers Web Push there),
Safari/Chrome/Firefox on a Mac or PC.

Like the meter form it has no login of its own: Caddy only lets logged-in Grafana users through
to /push/ (see caddy/Caddyfile). The one exception is the service worker script, which the browser
re-fetches on its own and which contains nothing private. The VAPID key pair (it identifies this
sender to the browsers' push services) is generated on first start and kept in Redis, next to
the subscriptions it belongs to.

Routes (behind Caddy's /push/ prefix, which it strips):
  GET  /           page to turn notifications on/off and send a test
  GET  /sw.js      service worker (shows the notification, opens the dashboard on a tap)
  GET  /key        VAPID public key (applicationServerKey)
  POST /subscribe  {subscription}   POST /unsubscribe {endpoint}   POST /test
"""

import base64
import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import redis
from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid01
from pywebpush import WebPushException, webpush

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("push")

PORT = int(os.environ.get("PORT", "8000"))
# VAPID "sub" claim: how a push service can reach the sender. A URL is allowed, so no e-mail
# address has to be handed to Apple/Google/Mozilla.
VAPID_SUBJECT = os.environ.get("PUSH_VAPID_SUBJECT") or (
    f"https://{os.environ['GRAFANA_DOMAIN']}" if os.environ.get("GRAFANA_DOMAIN") else "https://localhost")
STREAM = "notify"
SUBSCRIPTIONS = "push:subscriptions"   # hash endpoint -> subscription JSON
VAPID_KEY = "push:vapid"               # hash private_pem, public_b64
LAST_ID = "push:last_id"               # last stream entry delivered
TTL_S = 6 * 3600                       # a push service may hold a message this long for an offline device

r = redis.Redis(
    host=os.environ.get("REDIS_HOST", "redis"),
    port=int(os.environ.get("REDIS_PORT", "6379")),
    password=os.environ.get("REDIS_PASSWORD") or None,
    decode_responses=True,
)


def load_vapid() -> tuple[Vapid01, str]:
    """(signing key, public key as base64url for applicationServerKey); generated once."""
    stored = r.hgetall(VAPID_KEY)
    if not stored:
        v = Vapid01()
        v.generate_keys()
        pem = v.private_pem().decode()
        raw = v.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        public = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
        # hsetnx: two instances starting at once must not end up with different keys
        if r.hsetnx(VAPID_KEY, "private_pem", pem):
            r.hset(VAPID_KEY, "public_b64", public)
            log.info("Generated a new VAPID key pair")
        stored = r.hgetall(VAPID_KEY)
    return Vapid01.from_pem(stored["private_pem"].encode()), stored["public_b64"]


VAPID, PUBLIC_KEY = load_vapid()


def send_to_all(payload: dict) -> int:
    """Delivers one notification to every subscription; drops those the push service reports gone."""
    sent = 0
    for endpoint, sub in r.hgetall(SUBSCRIPTIONS).items():
        try:
            webpush(json.loads(sub), data=json.dumps(payload), vapid_private_key=VAPID,
                    vapid_claims={"sub": VAPID_SUBJECT}, ttl=TTL_S, timeout=10)
            sent += 1
        except WebPushException as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status in (404, 410):  # unsubscribed, or the browser/app was removed
                r.hdel(SUBSCRIPTIONS, endpoint)
                log.info("Dropped expired subscription %s...", endpoint[:60])
            else:
                log.warning("Push to %s... failed: %s", endpoint[:60], exc)
        except Exception as exc:  # network trouble etc. -- never let it stop the sender loop
            log.warning("Push to %s... failed: %s", endpoint[:60], exc)
    return sent


def sender_loop() -> None:
    """Follows the stream and pushes every new entry. Only entries added while running are sent:
    after a long outage, a pile of stale "switched on/off" messages would only confuse."""
    last = "$"
    while True:
        try:
            for _, entries in r.xread({STREAM: last}, block=30_000) or []:
                for entry_id, fields in entries:
                    last = entry_id
                    n = send_to_all(fields)
                    log.info("Sent \"%s\" to %d device(s)", fields.get("title", ""), n)
                    r.set(LAST_ID, entry_id)
        except redis.RedisError as exc:
            log.warning("Redis error in sender loop (%s), retrying", exc)
            time.sleep(5)


PAGE = """<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Benachrichtigungen</title>
<style>
 :root { color-scheme: light dark; font-family: -apple-system, system-ui, sans-serif; }
 body { max-width: 34rem; margin: 2rem auto; padding: 0 1rem; line-height: 1.5; }
 button { font-size: 1rem; padding: .6rem 1rem; margin: .3rem .3rem .3rem 0; border-radius: .5rem;
          border: 1px solid #888; background: #2d7d46; color: #fff; }
 button.secondary { background: transparent; color: inherit; }
 #status { padding: .8rem; border-radius: .5rem; background: rgba(128,128,128,.15); }
 a { color: inherit; }
</style></head><body>
<h1>Benachrichtigungen</h1>
<p>Push-Nachricht auf diesem Gerät, sobald das Lastmanagement ein Gerät hoch- oder
herunterfährt, ein- oder ausschaltet (Rechner, Entfeuchter, Warmwasser-Boost).</p>
<p id="status">Prüfe …</p>
<p><button id="on" hidden>Benachrichtigungen aktivieren</button>
<button id="test" class="secondary" hidden>Test senden</button>
<button id="off" class="secondary" hidden>Auf diesem Gerät abschalten</button></p>
<p><a href="../d/compute-controller">Zum Lastmanagement</a></p>
<script>
const $ = id => document.getElementById(id);
const say = t => $("status").textContent = t;
function b64ToBytes(s) {
  const pad = "=".repeat((4 - s.length % 4) % 4);
  const raw = atob((s + pad).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(raw, c => c.charCodeAt(0));
}
async function post(path, body) {
  const res = await fetch(path, {method: "POST", headers: {"Content-Type": "application/json"},
                                 body: JSON.stringify(body || {})});
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}
async function registration() {
  // scope "/": the notification belongs to the whole web app, not just this page
  return navigator.serviceWorker.register("sw.js", {scope: "/"});
}
async function refresh() {
  if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) {
    const ios = /iPhone|iPad/.test(navigator.userAgent) || (navigator.maxTouchPoints > 1 && /Mac/.test(navigator.userAgent));
    say(ios ? "Auf iPhone/iPad gehen Benachrichtigungen nur aus der Web-App auf dem Home-Bildschirm " +
              "(iOS 16.4 oder neuer): diese Seite dort öffnen, nicht in Safari."
            : "Dieser Browser unterstützt keine Push-Benachrichtigungen.");
    return;
  }
  const sub = await (await registration()).pushManager.getSubscription();
  if (Notification.permission === "denied") {
    say("Benachrichtigungen sind für diese Seite blockiert – in den Einstellungen des Browsers bzw. der App erlauben.");
  } else if (sub) {
    say("Aktiv auf diesem Gerät.");
  } else {
    say("Auf diesem Gerät nicht aktiv.");
  }
  $("on").hidden = !!sub || Notification.permission === "denied";
  $("test").hidden = $("off").hidden = !sub;
}
$("on").onclick = async () => {
  try {
    if (await Notification.requestPermission() !== "granted") { await refresh(); return; }
    const key = (await (await fetch("key")).json()).key;
    const sub = await (await registration()).pushManager.subscribe({userVisibleOnly: true, applicationServerKey: b64ToBytes(key)});
    await post("subscribe", {subscription: sub.toJSON()});
    await refresh();
  } catch (e) { say("Fehler: " + e.message); }
};
$("test").onclick = async () => {
  try { const r = await post("test"); say("Test an " + r.sent + " Gerät(e) geschickt."); }
  catch (e) { say("Fehler: " + e.message); }
};
$("off").onclick = async () => {
  try {
    const sub = await (await registration()).pushManager.getSubscription();
    if (sub) { await post("unsubscribe", {endpoint: sub.endpoint}); await sub.unsubscribe(); }
    await refresh();
  } catch (e) { say("Fehler: " + e.message); }
};
refresh().catch(e => say("Fehler: " + e.message));
</script>
</body></html>
"""

SERVICE_WORKER = """// Shows the load management's push messages; a tap opens (or focuses) the web app there.
self.addEventListener("push", event => {
  let d = {};
  try { d = event.data ? event.data.json() : {}; } catch (e) { d = {body: event.data && event.data.text()}; }
  event.waitUntil(self.registration.showNotification(d.title || "Energie", {
    body: d.body || "", tag: d.tag || undefined, renotify: !!d.tag,
    icon: "/pwa/icon-192.png", badge: "/pwa/icon-192.png", data: {url: d.url || "/"},
  }));
});
self.addEventListener("notificationclick", event => {
  event.notification.close();
  const url = new URL(event.notification.data.url, self.location.origin).href;
  event.waitUntil(clients.matchAll({type: "window", includeUncontrolled: true}).then(list => {
    for (const c of list) { if ("focus" in c) { c.navigate(url); return c.focus(); } }
    return clients.openWindow(url);
  }));
});
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", event => event.waitUntil(clients.claim()));
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.debug(fmt, *args)

    def reply(self, status: int, body: str, ctype: str, extra: dict | None = None) -> None:
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def json(self, status: int, obj: dict) -> None:
        self.reply(status, json.dumps(obj), "application/json")

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", ""):
            self.reply(200, PAGE, "text/html; charset=utf-8")
        elif path == "/sw.js":
            # served from /push/ but registered with scope "/": the browser needs this header for that
            self.reply(200, SERVICE_WORKER, "text/javascript; charset=utf-8", {"Service-Worker-Allowed": "/"})
        elif path == "/key":
            self.json(200, {"key": PUBLIC_KEY})
        else:
            self.json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        # JSON only: a cross-site form can't send that without a CORS preflight, which we don't answer
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            self.json(415, {"error": "application/json expected"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(min(length, 16_384)) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self.json(400, {"error": "invalid JSON"})
            return
        if path == "/subscribe":
            sub = body.get("subscription") or {}
            endpoint = sub.get("endpoint", "")
            if not endpoint.startswith("https://") or not (sub.get("keys") or {}).get("p256dh"):
                self.json(400, {"error": "invalid subscription"})
                return
            r.hset(SUBSCRIPTIONS, endpoint, json.dumps(sub))
            log.info("New subscription %s... (%d in total)", endpoint[:60], r.hlen(SUBSCRIPTIONS))
            self.json(200, {"ok": True})
        elif path == "/unsubscribe":
            r.hdel(SUBSCRIPTIONS, body.get("endpoint", ""))
            self.json(200, {"ok": True})
        elif path == "/test":
            n = send_to_all({"title": "Energie: Test", "body": "Benachrichtigungen funktionieren.",
                             "tag": "test", "url": "/d/compute-controller"})
            self.json(200, {"sent": n})
        else:
            self.json(404, {"error": "not found"})


def main() -> None:
    threading.Thread(target=sender_loop, daemon=True, name="sender").start()
    log.info("Listening on :%d, %d subscription(s)", PORT, r.hlen(SUBSCRIPTIONS))
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
