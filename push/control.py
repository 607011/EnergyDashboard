"""Control page for the load management: switch devices by hand, with an expiry and a reason.

A button doesn't switch anything itself -- it stores a manual override in Redis
(compute:override:<name>: mode on/off/pause, until_ms, reason) and wakes compute-controller for
an immediate cycle (compute:wake). The controller gives the override precedence over every rule
and delay until it expires, then goes back to automatic; "pause" means it doesn't switch the
device at all. Every override is also logged with its reason and the situation at that moment
(stream compute:overrides) -- the raw material for learning where the automatic rules are off.

Served under /control/ behind the Grafana login (see caddy/Caddyfile), by the push service.
"""

import json
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.environ.get("TIMEZONE", "UTC"))
OVERRIDE_LOG = "compute:overrides"

# name -> kind, in priority order, from the same setting the controller reads
MACHINES = []
for _entry in os.environ.get("COMPUTE_MACHINES", "").split(","):
    _parts = [p.strip() for p in _entry.split(":")]
    if len(_parts) >= 2 and _parts[0]:
        MACHINES.append((_parts[0], _parts[2] if len(_parts) > 2 else "pc"))

# reason code -> text; the codes are what an evaluation later groups by
REASONS = {
    "forecast_optimistic": "Prognose zu optimistisch, Batterie droht leerzulaufen",
    "forecast_pessimistic": "Prognose zu pessimistisch, Überschuss bleibt ungenutzt",
    "need_device": "Ich brauche das Gerät / arbeite daran",
    "rest": "Gerät soll ruhen (Lärm, Wärme, Wartung)",
    "test": "Test",
    "other": "Sonstiges",
}
MODES = ("on", "off", "pause", "auto")


def until_ms(duration: str, now: datetime) -> int | None:
    """"1".."24" hours, or "morning" = next 07:00 local time."""
    if duration == "morning":
        t = now.replace(hour=7, minute=0, second=0, microsecond=0)
        if t <= now:
            t += timedelta(days=1)
        return int(t.timestamp() * 1000)
    try:
        hours = float(duration)
    except (TypeError, ValueError):
        return None
    if not 0 < hours <= 24:
        return None
    return int((now + timedelta(hours=hours)).timestamp() * 1000)


def state(r) -> dict:
    now_ms = int(time.time() * 1000)
    machines = []
    for name, kind in MACHINES:
        h = r.hgetall(f"compute:{name}:latest")
        o = r.hgetall(f"compute:override:{name}")
        if o and int(o.get("until_ms", 0)) <= now_ms:
            o = {}
        machines.append({
            "name": name, "kind": kind, "desired": h.get("desired") == "1", "reason": h.get("reason", ""),
            "power_w": h.get("power_w"),
            "override": {"mode": o["mode"], "until": datetime.fromtimestamp(int(o["until_ms"]) / 1000, TZ).strftime("%H:%M"),
                         "reason": o.get("reason", "")} if o else None,
        })
    latest = r.hgetall("compute:latest")
    return {"machines": machines, "reasons": REASONS, "enabled": not r.exists("compute:disabled"),
            "situation": {k: latest.get(k, "") for k in ("headroom_w", "forecast_headroom_w", "soc", "battery_budget")}}


def set_override(r, body: dict) -> tuple[int, dict]:
    names = body.get("names") or []
    if names == "all":
        names = [n for n, _ in MACHINES]
    known = {n for n, _ in MACHINES}
    mode = body.get("mode")
    if not names or not set(names) <= known or mode not in MODES:
        return 400, {"error": "unknown device or mode"}
    now = datetime.now(TZ)
    code = body.get("reason") or ""
    text = (body.get("text") or "").strip()[:200]
    if mode != "auto":
        until = until_ms(str(body.get("duration", "")), now)
        if until is None:
            return 400, {"error": "invalid duration"}
        if code not in REASONS:
            return 400, {"error": "please give a reason"}
        reason = REASONS[code] if code != "other" or not text else text
        if text and code != "other":
            reason += f" ({text})"
    latest = r.hgetall("compute:latest")
    pipe = r.pipeline()
    for name in names:
        key = f"compute:override:{name}"
        machine = r.hgetall(f"compute:{name}:latest")
        if mode == "auto":
            pipe.delete(key)
        else:
            pipe.hset(key, mapping={"mode": mode, "until_ms": until, "reason": reason, "reason_code": code,
                                    "set_at": int(now.timestamp() * 1000)})
        # the log entry: what was asked, why, and what the controller itself would have done
        pipe.xadd(OVERRIDE_LOG, {
            "machine": name, "mode": mode, "until_ms": until if mode != "auto" else "",
            "reason_code": code, "text": text,
            "controller_desired": machine.get("desired", ""), "controller_reason": machine.get("reason", ""),
            "situation": json.dumps({k: latest.get(k, "") for k in (
                "headroom_w", "forecast_headroom_w", "forecast_headroom_long_w", "production_w", "house_w",
                "soc", "battery_budget", "evening_discharge")}),
        }, maxlen=10_000, approximate=True)
    pipe.rpush("compute:wake", 1)  # the controller runs a cycle right away instead of within a minute
    pipe.execute()
    return 200, {"ok": True}


def set_enabled(r, body: dict) -> tuple[int, dict]:
    """Main switch: load management on (automatic) or off (the controller switches nothing)."""
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        return 400, {"error": "enabled must be true or false"}
    now = datetime.now(TZ)
    pipe = r.pipeline()
    if enabled:
        pipe.delete("compute:disabled")
    else:
        pipe.set("compute:disabled", int(now.timestamp() * 1000))
    text = "Lastmanagement eingeschaltet (Automatik)" if enabled else "Lastmanagement ausgeschaltet (von Hand)"
    pipe.lpush("compute:events", f"{now:%d.%m. %H:%M}  {text}")
    pipe.xadd(OVERRIDE_LOG, {"machine": "*", "mode": "enable" if enabled else "disable", "until_ms": "",
                             "reason_code": "", "text": (body.get("text") or "")[:200],
                             "controller_desired": "", "controller_reason": "", "situation": "{}"},
              maxlen=10_000, approximate=True)
    pipe.rpush("compute:wake", 1)
    pipe.execute()
    return 200, {"ok": True, "enabled": enabled}


PAGE = """<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Geräte steuern</title>
<style>
 :root { color-scheme: light dark; font-family: -apple-system, system-ui, sans-serif; --line: rgba(128,128,128,.3); }
 body { max-width: 40rem; margin: 1.5rem auto; padding: 0 1rem; line-height: 1.4; }
 h1 { font-size: 1.4rem; margin-bottom: .3rem; }
 .sit { font-size: .9rem; opacity: .75; margin-bottom: 1rem; }
 .dev { border: 1px solid var(--line); border-radius: .7rem; padding: .7rem .8rem; margin: .6rem 0; }
 .dev.all { border-style: dashed; }
 .head { display: flex; justify-content: space-between; gap: .5rem; align-items: baseline; flex-wrap: wrap; }
 .name { font-weight: 600; }
 .st { font-size: .85rem; }
 .on { color: #2d9d56; } .off { opacity: .7; }
 .why { font-size: .8rem; opacity: .75; margin: .2rem 0 .5rem; }
 .ovr { font-size: .8rem; background: rgba(230,150,30,.2); border-radius: .4rem; padding: .15rem .4rem; }
 .btns { display: flex; gap: .35rem; flex-wrap: wrap; }
 button { font-size: .95rem; padding: .45rem .8rem; border-radius: .5rem; border: 1px solid #888;
          background: transparent; color: inherit; }
 button.primary { background: #2d7d46; color: #fff; border-color: #2d7d46; }
 dialog { border: 1px solid var(--line); border-radius: .8rem; max-width: 26rem; width: calc(100% - 2rem); }
 dialog label { display: block; margin: .3rem 0; }
 select, input[type=text] { font-size: 1rem; width: 100%; box-sizing: border-box; padding: .35rem; margin-top: .2rem; }
 fieldset { border: none; padding: 0; margin: .6rem 0; }
 #msg { min-height: 1.2rem; font-size: .9rem; }
 .dev.main { display: flex; justify-content: space-between; align-items: center; gap: .6rem; flex-wrap: wrap; }
 .dev.main.off { border-color: #c0392b; background: rgba(192,57,43,.08); }
 button.danger { background: #c0392b; color: #fff; border-color: #c0392b; }
</style></head><body>
<h1>Geräte steuern</h1>
<div class="dev main" id="main"></div>
<div class="sit" id="sit"></div>
<div id="msg"></div>
<div id="list"></div>
<p><a href="../d/compute-controller">Zum Lastmanagement</a></p>

<dialog id="dlg"><form method="dialog" id="form">
 <p><strong id="dtitle"></strong></p>
 <label>Wie lange?
  <select id="duration">
   <option value="1">1 Stunde</option><option value="2" selected>2 Stunden</option>
   <option value="4">4 Stunden</option><option value="8">8 Stunden</option>
   <option value="morning">bis morgen 7 Uhr</option>
  </select></label>
 <fieldset id="reasons"><legend>Warum?</legend></fieldset>
 <label>Anmerkung (optional)<input type="text" id="text" maxlength="200"></label>
 <p class="btns"><button value="cancel" formnovalidate>Abbrechen</button>
 <button value="ok" class="primary" id="ok">Übernehmen</button></p>
</form></dialog>

<script>
const LABEL = {on: "An", off: "Aus", pause: "Pausieren"};
const TITLE = {on: "einschalten", off: "ausschalten", pause: "pausieren (Regler schaltet nicht)"};
let reasons = {}, pending = null;
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

async function load() {
  const s = await (await fetch("state")).json();
  reasons = s.reasons;
  $("main").className = "dev main" + (s.enabled ? "" : " off");
  $("main").innerHTML = s.enabled
    ? `<span><strong>Lastmanagement: EIN</strong> – schaltet automatisch</span>
       <button class="danger" onclick="mainSwitch(false)">Ausschalten</button>`
    : `<span><strong>Lastmanagement: AUS</strong> – der Regler schaltet nichts, alles bleibt, wie es ist</span>
       <button class="primary" onclick="mainSwitch(true)">Einschalten</button>`;
  const t = s.situation;
  $("sit").textContent = `Überschuss ${t.headroom_w || "–"} W · Prognose nächste Stunde ${t.forecast_headroom_w || "–"} W · ${t.battery_budget || ""}`;
  $("list").innerHTML = s.machines.map(m => `
   <div class="dev">
    <div class="head"><span class="name">${esc(m.name)}</span>
     <span class="st ${m.desired ? "on" : "off"}">${m.desired ? "an" : "aus"}${m.power_w ? " · " + Math.round(m.power_w) + " W" : ""}</span></div>
    ${m.override ? `<div class="why"><span class="ovr">${esc(LABEL[m.override.mode] || m.override.mode)} bis ${esc(m.override.until)}</span> ${esc(m.override.reason)}</div>`
                 : `<div class="why">Automatik: ${esc(m.reason)}</div>`}
    <div class="btns">${btns(`["${esc(m.name)}"]`, !!m.override)}</div>
   </div>`).join("") + `
   <div class="dev all"><div class="head"><span class="name">Alle Geräte</span></div>
    <div class="btns">${btns('"all"', true)}</div></div>`;
}
function btns(names, showAuto) {
  return ["on", "off", "pause"].map(mode => `<button onclick='ask(${names}, "${mode}")'>${LABEL[mode]}</button>`).join("")
       + (showAuto ? `<button onclick='send(${names}, "auto")'>Automatik</button>` : "");
}
function ask(names, mode) {
  pending = {names, mode};
  $("dtitle").textContent = (names === "all" ? "Alle Geräte" : names.join(", ")) + " " + TITLE[mode];
  $("reasons").innerHTML = "<legend>Warum?</legend>" + Object.entries(reasons).map(([k, v], i) =>
    `<label><input type="radio" name="reason" value="${k}" required> ${esc(v)}</label>`).join("");
  $("text").value = "";
  $("dlg").showModal();
}
$("form").addEventListener("submit", e => {
  if (e.submitter && e.submitter.value === "cancel") return;
  const r = document.querySelector("input[name=reason]:checked");
  if (!r) { e.preventDefault(); alert("Bitte einen Grund wählen."); return; }
  send(pending.names, pending.mode, {duration: $("duration").value, reason: r.value, text: $("text").value});
});
async function mainSwitch(enabled) {
  $("msg").textContent = "…";
  const res = await fetch("enable", {method: "POST", headers: {"Content-Type": "application/json"},
                                     body: JSON.stringify({enabled})});
  $("msg").textContent = res.ok ? (enabled ? "Lastmanagement eingeschaltet." : "Lastmanagement ausgeschaltet.") : "Fehler " + res.status;
  load();
}
async function send(names, mode, extra) {
  $("msg").textContent = "…";
  const res = await fetch("set", {method: "POST", headers: {"Content-Type": "application/json"},
                                  body: JSON.stringify({names, mode, ...(extra || {})})});
  const j = await res.json();
  $("msg").textContent = res.ok ? "Übernommen – der Regler schaltet jetzt." : "Fehler: " + (j.error || res.status);
  setTimeout(load, 2500); setTimeout(load, 8000);
}
load(); setInterval(load, 15000);
</script>
</body></html>
"""
