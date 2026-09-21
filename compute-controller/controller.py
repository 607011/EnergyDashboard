"""Decides when the compute machines (PrimeGrid) should run, from the PV surplus and a forecast.

DRY RUN ONLY for now: the controller computes and records what it *would* switch, but nothing is
switched. Actuation (Shelly plugs, shutdown over SSH) comes later; until then this shows whether
the rules make sense.

Every cycle it works out
  - headroom: PV production minus house consumption *without* the compute machines (15-minute
    means), i.e. the power that would otherwise be exported or charge the battery
  - a forecast of that headroom for the next hour: the measured production, scaled by how the
    hourly irradiance forecast for the next hour compares with that of the last hour
and then decides per machine, in priority order (the order of COMPUTE_MACHINES):
  - ON  when the headroom covers the machines up to and including this one plus a margin, the
        forecast headroom does too, and the battery is charged enough
  - OFF when the headroom no longer covers them, or the forecast says it won't and the battery
        isn't nearly full (so battery use is to be feared), or the battery gets too low
  - a change only applies once its condition has held for a while (clouds pass, the battery bridges
    short dips), and machines keep a minimum run time and pause; both against flapping
    (tuned with backtest.py: about 4-5 switching events per day instead of 15)

Reads:  ts:inverter:power_pv_total, ts:hoymiles:*:power_w, ts:inverter:house_consumption_total,
        solaredge:battery:battery1:latest (soe), Open-Meteo hourly irradiance
Writes: compute:latest, compute:<machine>:latest, compute:<machine>:state, compute:events (list),
        ts:compute:headroom_w, ts:compute:forecast_headroom_w, ts:compute:<machine>:desired,
        ts:compute:<machine>:threshold_on_w
"""

import logging
import os
import signal
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import redis
import requests

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("compute-controller")

MINUTE_MS = 60_000
HOUR_S = 3600


# --------------------------------------------------------------------------- configuration

@dataclass
class Machine:
    name: str
    watts: float


@dataclass
class Params:
    machines: list
    mode: str = "dryrun"
    interval_s: float = 60.0
    smooth_min: float = 15.0        # averaging window for headroom and production
    margin_on_w: float = 200.0      # headroom needed on top of the machines' load to start one
    tolerance_off_w: float = 100.0  # how far below the load the headroom may sink before stopping
    min_on_min: float = 45.0
    min_off_min: float = 45.0
    on_delay_min: float = 15.0      # a start condition must hold this long before the machine starts
    off_delay_min: float = 20.0     # likewise for a stop (the battery bridges short dips)
    soc_on: float = 80.0            # battery level needed to start a machine (%)
    soc_min: float = 40.0           # below this, stop immediately
    soc_fc_off: float = 95.0        # a bad forecast only stops machines while the battery is below this
    w_per_wm2: float = 10.0         # fallback conversion irradiance -> PV power (learned if enough data)


def parse_machines(spec: str) -> list:
    machines = []
    for entry in spec.split(","):
        entry = entry.strip()
        if not entry:
            continue
        name, _, watts = entry.partition(":")
        machines.append(Machine(name.strip(), float(watts)))
    return machines


def params_from_env(env=os.environ) -> Params:
    def f(key, default):
        return float(env.get(key, default))
    return Params(
        machines=parse_machines(env.get("COMPUTE_MACHINES", "")),
        mode=env.get("COMPUTE_MODE", "dryrun"),
        interval_s=f("COMPUTE_INTERVAL", 60),
        smooth_min=f("COMPUTE_SMOOTH_MIN", 15),
        margin_on_w=f("COMPUTE_MARGIN_ON_W", 200),
        tolerance_off_w=f("COMPUTE_TOLERANCE_OFF_W", 100),
        min_on_min=f("COMPUTE_MIN_ON_MIN", 45),
        min_off_min=f("COMPUTE_MIN_OFF_MIN", 45),
        on_delay_min=f("COMPUTE_ON_DELAY_MIN", 15),
        off_delay_min=f("COMPUTE_OFF_DELAY_MIN", 20),
        soc_on=f("COMPUTE_SOC_ON", 80),
        soc_min=f("COMPUTE_SOC_MIN", 40),
        soc_fc_off=f("COMPUTE_SOC_FC_OFF", 95),
        w_per_wm2=f("COMPUTE_W_PER_WM2", 10),
    )


# --------------------------------------------------------------------------- forecast (pure)

def forecast_window_mean(hourly, start_s, end_s):
    """Mean of Open-Meteo hourly values over [start_s, end_s].

    Each value is the mean of the hour *preceding* its timestamp, so a point stamped T covers
    (T - 1 h, T]. `hourly` is a list of (T seconds, value). Returns None unless the hourly data
    covers the whole window.
    """
    total = covered = 0.0
    for t_end, value in hourly:
        if value is None:
            continue
        overlap = min(end_s, t_end) - max(start_s, t_end - HOUR_S)
        if overlap > 0:
            total += value * overlap
            covered += overlap
    if covered < (end_s - start_s) * 0.99:
        return None
    return total / covered


def forecast_pv(pv_recent_w, f_next, f_ref, w_per_wm2):
    """PV power expected over the next hour.

    Anchored on what the plant actually produces: the measured recent production is scaled by
    the irradiance forecast for the next hour relative to the last hour. That cancels most of
    the plant-specific conversion error. Around sunrise and sunset the reference irradiance is
    too small to divide by, so the fallback is the plain conversion irradiance -> watts.
    """
    if f_next is None:
        return None
    if pv_recent_w is not None and f_ref is not None and f_ref >= 50:
        return pv_recent_w * min(max(f_next / f_ref, 0.0), 2.5)
    return w_per_wm2 * f_next


# --------------------------------------------------------------------------- decision (pure)

@dataclass
class Inputs:
    complete: bool                 # all production and consumption figures are known
    headroom_w: float | None       # PV minus house consumption without the machines
    forecast_headroom_w: float | None
    soc: float | None


@dataclass
class State:
    on: bool = False
    since_ms: int = 0              # when it last changed
    pending_ms: int = 0            # since when the condition for the next change has held (0 = not at all)


@dataclass
class Decision:
    machine: Machine
    on: bool
    changed: bool
    threshold_on_w: float
    reason: str
    pending_ms: int = 0            # new value for State.pending_ms


def decide(machines, states, inputs: Inputs, params: Params, now_ms: int):
    """Desired state per machine, in priority order (cumulative load decides who runs).

    A change needs its condition to hold for on_delay_min / off_delay_min first (clouds pass, the
    battery bridges short dips), on top of the minimum on and off times.
    """
    decisions = []
    cumulative = 0.0
    for machine in machines:
        cumulative += machine.watts
        state = states.get(machine.name) or State(False, 0, 0)
        on_threshold = cumulative + params.margin_on_w
        off_threshold = cumulative - params.tolerance_off_w
        age_min = (now_ms - state.since_ms) / MINUTE_MS

        def result(on, reason, pending=0):
            return Decision(machine, on, on != state.on, on_threshold, reason, pending)

        def after_delay(want_on, reason, delay_min):
            """The change is wanted: apply it only once the condition has held for delay_min."""
            since = state.pending_ms or now_ms
            waited = (now_ms - since) / MINUTE_MS
            if waited >= delay_min:
                return result(want_on, reason)
            return result(state.on, f"{reason} – abwarten ({waited:.0f} von {delay_min:.0f} min)", since)

        if not inputs.complete or inputs.headroom_w is None:
            decisions.append(result(state.on, "keine Entscheidung: Erzeugungs- oder Verbrauchsdaten unvollständig",
                                    state.pending_ms))
            continue
        h, fc, soc = inputs.headroom_w, inputs.forecast_headroom_w, inputs.soc

        if state.on:
            if soc is not None and soc < params.soc_min:
                decisions.append(result(False, f"Batterie {soc:.0f} % unter {params.soc_min:.0f} %"))
            elif age_min < params.min_on_min:
                decisions.append(result(True, f"läuft, Mindestlaufzeit ({age_min:.0f} von {params.min_on_min:.0f} min)"))
            elif h < off_threshold:
                decisions.append(after_delay(False, f"Überschuss {h:.0f} W deckt {cumulative:.0f} W nicht mehr",
                                             params.off_delay_min))
            elif fc is not None and fc < off_threshold and (soc is None or soc < params.soc_fc_off):
                decisions.append(after_delay(False, f"Prognose {fc:.0f} W reicht nicht, Batterie "
                                             + (f"{soc:.0f} %" if soc is not None else "unbekannt")
                                             + " (Entladung droht)", params.off_delay_min))
            else:
                decisions.append(result(True, f"läuft: Überschuss {h:.0f} W ≥ {off_threshold:.0f} W"))
        else:
            if age_min < params.min_off_min and state.since_ms > 0:
                decisions.append(result(False, f"aus, Mindestpause ({age_min:.0f} von {params.min_off_min:.0f} min)"))
            elif h < on_threshold:
                decisions.append(result(False, f"Überschuss {h:.0f} W unter {on_threshold:.0f} W"))
            elif soc is None or soc < params.soc_on:
                decisions.append(result(False, "Batterie nicht bekannt" if soc is None
                                        else f"Batterie {soc:.0f} % unter {params.soc_on:.0f} %"))
            elif fc is not None and fc < cumulative:
                decisions.append(result(False, f"Prognose {fc:.0f} W reicht für {cumulative:.0f} W nicht"))
            else:
                extra = "" if fc is None else f", Prognose {fc:.0f} W"
                decisions.append(after_delay(True, f"Überschuss {h:.0f} W ≥ {on_threshold:.0f} W, Batterie {soc:.0f} %{extra}",
                                             params.on_delay_min))
    return decisions


# --------------------------------------------------------------------------- data access

PV_KEYS_SOLAREDGE = "ts:inverter:power_pv_total"
HOUSE_KEY = "ts:inverter:house_consumption_total"
BATTERY_KEY = "solaredge:battery:battery1:latest"


def ts_mean(r, key, now_ms, minutes, stale_min=15):
    """Mean over the last `minutes`; if the window is empty, the newest sample no older than stale_min."""
    try:
        points = r.ts().range(key, now_ms - int(minutes * MINUTE_MS), now_ms)
        if points:
            return sum(v for _, v in points) / len(points)
        last = r.ts().get(key)
        if last and now_ms - last[0] <= stale_min * MINUTE_MS:
            return last[1]
    except redis.ResponseError:
        pass
    return None


def hoymiles_keys(r):
    """Per-inverter Hoymiles power series (the station hash repeats the total and must not count twice)."""
    keys = []
    for hash_key in r.scan_iter(match="hoymiles:*:latest"):
        if r.hexists(hash_key, "p1_w"):
            keys.append(f"ts:{hash_key[:-len(':latest')]}:power_w")
    return keys


def machine_power(r, machine, now_ms):
    """Measured power of a machine (from the Shelly plug, once there is one); None while unknown."""
    h = r.hgetall(f"compute:{machine.name}:latest")
    try:
        if now_ms - int(h["power_updated_at"]) <= 5 * MINUTE_MS:
            return float(h["power_w"])
    except (KeyError, ValueError):
        pass
    return None


class Forecast:
    """Hourly irradiance from Open-Meteo, cached for 30 minutes."""

    def __init__(self, lat, lon):
        self.lat, self.lon = lat, lon
        self.hourly = []
        self.fetched = 0.0

    def get(self):
        if self.lat is None or self.lon is None:
            return []
        if time.monotonic() - self.fetched > 1800:
            try:
                response = requests.get("https://api.open-meteo.com/v1/forecast", params={
                    "latitude": self.lat, "longitude": self.lon, "hourly": "shortwave_radiation",
                    "past_days": 1, "forecast_days": 2, "timezone": "UTC"}, timeout=10)
                response.raise_for_status()
                data = response.json()["hourly"]
                self.hourly = [
                    (int(datetime.fromisoformat(t).replace(tzinfo=timezone.utc).timestamp()), v)
                    for t, v in zip(data["time"], data["shortwave_radiation"])
                ]
                self.fetched = time.monotonic()
            except Exception:
                log.exception("Forecast fetch failed, using the last one")
                self.fetched = time.monotonic() - 1500   # retry in 5 minutes
        return self.hourly


def learn_w_per_wm2(r, now_ms, default):
    """Median of PV power / irradiance over the last 14 days (daylight samples); default if too few."""
    try:
        since = now_ms - 14 * 24 * 3600 * 1000
        ghi = r.ts().range("ts:weather:shortwave_radiation", since, now_ms)
        bucket = 600_000
        def agg(key):
            return {t: v for t, v in r.ts().range(key, since, now_ms, aggregation_type="avg", bucket_size_msec=bucket)}
        pv = [agg(PV_KEYS_SOLAREDGE)] + [agg(k) for k in hoymiles_keys(r)]
        ratios = []
        for t, g in ghi:
            if g < 150:
                continue
            b = t - t % bucket
            if b not in pv[0]:
                continue
            total = sum(series.get(b, 0.0) for series in pv)
            ratios.append(total / g)
        if len(ratios) >= 40:
            return statistics.median(ratios), len(ratios)
    except redis.ResponseError:
        pass
    return default, 0


# --------------------------------------------------------------------------- main loop

running = True


def handle_signal(signum, _frame):
    global running
    log.info("Received signal %s, shutting down", signum)
    running = False


def cycle(r, params, forecast, tz, learned, now_ms):
    now_s = now_ms / 1000
    # production: SolarEdge + all Hoymiles inverters; every part must be known
    parts = [ts_mean(r, PV_KEYS_SOLAREDGE, now_ms, params.smooth_min)]
    parts += [ts_mean(r, k, now_ms, params.smooth_min) for k in hoymiles_keys(r)]
    house = ts_mean(r, HOUSE_KEY, now_ms, params.smooth_min)
    house_60 = ts_mean(r, HOUSE_KEY, now_ms, 60)
    complete = all(p is not None for p in parts) and len(parts) > 1 and house is not None
    pv = sum(p for p in parts if p is not None) if complete else None

    # the machines' own consumption is part of the house consumption once they run; take it out
    measured = [machine_power(r, m, now_ms) for m in params.machines]
    machines_w = sum(m for m in measured if m is not None)
    power_known = all(m is not None for m in measured) if measured else True
    headroom = pv - (house - machines_w) if complete else None
    baseline_house = (house_60 - machines_w) if house_60 is not None else None

    soc_raw = r.hget(BATTERY_KEY, "soe")
    soc = float(soc_raw) if soc_raw not in (None, "") else None

    # forecast of the headroom over the next hour
    hourly = forecast.get()
    f_next = forecast_window_mean(hourly, now_s, now_s + HOUR_S)
    f_ref = forecast_window_mean(hourly, now_s - HOUR_S, now_s)
    pv_recent = ts_mean(r, PV_KEYS_SOLAREDGE, now_ms, 30)
    if pv_recent is not None:
        pv_recent += sum(ts_mean(r, k, now_ms, 30) or 0.0 for k in hoymiles_keys(r))
    pv_forecast = forecast_pv(pv_recent, f_next, f_ref, learned)
    forecast_headroom = (pv_forecast - baseline_house) if pv_forecast is not None and baseline_house is not None else None

    states = {}
    for m in params.machines:
        h = r.hgetall(f"compute:{m.name}:state")
        states[m.name] = (State(h.get("on") == "1", int(h.get("since_ms", 0)), int(h.get("pending_ms", 0)))
                          if h else State(False, 0, 0))

    inputs = Inputs(complete, headroom, forecast_headroom, soc)
    decisions = decide(params.machines, states, inputs, params, now_ms)

    pipe = r.pipeline(transaction=False)
    label = {"device": "compute"}
    def add(key, value):
        pipe.ts().add(key, now_ms, float(value), retention_msecs=RETENTION_MS,
                      labels={**label, "field": key.rsplit(":", 1)[-1]}, duplicate_policy="last")
    def fmt(v, digits=0):
        return "" if v is None else f"{v:.{digits}f}"

    latest = {
        "mode": "dryrun", "complete": int(complete), "power_known": int(power_known),
        "production_w": fmt(pv), "house_w": fmt(house), "machines_w": fmt(machines_w),
        "headroom_w": fmt(headroom), "forecast_headroom_w": fmt(forecast_headroom),
        "forecast_pv_w": fmt(pv_forecast), "forecast_irradiance_next": fmt(f_next), "forecast_irradiance_last": fmt(f_ref),
        "soc": fmt(soc), "w_per_wm2": fmt(learned, 1), "updated_at": now_ms,
    }
    pipe.delete("compute:latest")
    pipe.hset("compute:latest", mapping=latest)
    if headroom is not None:
        add("ts:compute:headroom_w", headroom)
    if forecast_headroom is not None:
        add("ts:compute:forecast_headroom_w", forecast_headroom)

    for d in decisions:
        name = d.machine.name
        if d.changed:
            pipe.hset(f"compute:{name}:state", mapping={"on": int(d.on), "since_ms": now_ms, "pending_ms": 0})
            stamp = datetime.fromtimestamp(now_s, tz).strftime("%d.%m. %H:%M")
            line = f"{stamp}  {name}: {'EIN' if d.on else 'AUS'} (Probelauf, nicht geschaltet) – {d.reason}"
            pipe.lpush("compute:events", line)
            pipe.ltrim("compute:events", 0, 199)
            log.info(line)
        else:
            pipe.hset(f"compute:{name}:state", mapping={
                "on": int(d.on), "since_ms": states[name].since_ms, "pending_ms": d.pending_ms})
        pipe.hset(f"compute:{name}:latest", mapping={
            "desired": int(d.on), "reason": d.reason, "watts": d.machine.watts,
            "threshold_on_w": d.threshold_on_w, "decided_at": now_ms})
        add(f"ts:compute:{name}:desired", int(d.on))
        add(f"ts:compute:{name}:threshold_on_w", d.threshold_on_w)
    pipe.execute()
    return latest, decisions


RETENTION_MS = int(os.environ.get("TS_RETENTION_DAYS", "365")) * 24 * 3600 * 1000


def main():
    params = params_from_env()
    if params.mode != "dryrun":
        log.error("COMPUTE_MODE=%s: switching machines is not implemented yet, staying in dry run", params.mode)
    if not params.machines:
        log.info("COMPUTE_MACHINES is empty -- no machines configured, idling")
        signal.signal(signal.SIGTERM, handle_signal)
        signal.signal(signal.SIGINT, handle_signal)
        while running:
            time.sleep(1)
        return

    r = redis.Redis(host=os.environ.get("REDIS_HOST", "redis"), port=int(os.environ.get("REDIS_PORT", "6379")),
                    password=os.environ.get("REDIS_PASSWORD") or None, decode_responses=True)
    r.ping()
    lat = float(os.environ["LAT"]) if os.environ.get("LAT") else None
    lon = float(os.environ["LON"]) if os.environ.get("LON") else None
    tz = ZoneInfo(os.environ.get("TIMEZONE", "UTC"))
    forecast = Forecast(lat, lon)
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    log.info("Dry run with %d machine(s): %s", len(params.machines),
             ", ".join(f"{m.name} {m.watts:.0f} W" for m in params.machines))

    learned, learned_at = params.w_per_wm2, 0.0
    while running:
        started = time.monotonic()
        try:
            now_ms = int(time.time() * 1000)
            if started - learned_at > 3600:
                learned, samples = learn_w_per_wm2(r, now_ms, params.w_per_wm2)
                learned_at = started
                log.info("Irradiance -> power fallback: %.1f W per W/m² (%s)", learned,
                         f"learned from {samples} samples" if samples else "default, too little data")
            cycle(r, params, forecast, tz, learned, now_ms)
        except Exception:
            log.exception("Cycle failed")
        time.sleep(max(0.0, params.interval_s - (time.monotonic() - started)))
    log.info("Stopped")


# --------------------------------------------------------------------------- self-test

def selftest():
    failures = 0

    def check(name, ok):
        nonlocal failures
        print(("ok   " if ok else "FAIL ") + name)
        failures += 0 if ok else 1

    def near(a, b, tol=1e-6):
        return a is not None and abs(a - b) < tol

    # --- forecast window mean: a value stamped T covers (T-1h, T]
    H = HOUR_S
    hourly = [(10 * H, 100.0), (11 * H, 300.0), (12 * H, 500.0)]   # covers 9h..12h
    check("Fenster deckt genau eine Stunde", near(forecast_window_mean(hourly, 10 * H, 11 * H), 300.0))
    check("Fenster über zwei Stunden gewichtet", near(forecast_window_mean(hourly, 10.5 * H, 11.5 * H), (300 + 500) / 2))
    check("Fenster ohne volle Daten: None", forecast_window_mean(hourly, 11.5 * H, 12.5 * H) is None)

    # --- forecast: scaled by the trend, anchored on measured production
    check("Prognose: gleiche Strahlung, gleiche Leistung", near(forecast_pv(2000, 400, 400, 10), 2000))
    check("Prognose: Strahlung halbiert, Leistung halbiert", near(forecast_pv(2000, 200, 400, 10), 1000))
    check("Prognose: Anstieg auf das 2,5fache begrenzt", near(forecast_pv(1000, 4000, 100, 10), 2500))
    check("Prognose: bei Dämmerung Umrechnung Strahlung->Watt", near(forecast_pv(50, 30, 20, 10), 300))
    check("Prognose: ohne Vorhersage None", forecast_pv(2000, None, 400, 10) is None)

    # --- decisions
    P = Params(machines=[Machine("a", 200), Machine("b", 300)], on_delay_min=0, off_delay_min=0,
               min_on_min=30, min_off_min=30)
    T = 10_000_000_000
    long_ago = T - 10 * 3600 * 1000
    def inp(h, fc=None, soc=90.0, complete=True):
        return Inputs(complete, h, fc, soc)
    def run(states, i):
        return {d.machine.name: d for d in decide(P.machines, states, i, P, T)}

    off = {"a": State(False, long_ago), "b": State(False, long_ago)}
    d = run(off, inp(300, 800))
    check("Überschuss unter Schwelle (400): bleibt aus", not d["a"].on and not d["a"].changed)
    d = run(off, inp(450, 800))
    check("Überschuss 450 ≥ 400: erster Rechner an, zweiter (700) noch nicht", d["a"].on and d["a"].changed and not d["b"].on)
    d = run(off, inp(800, 800))
    check("Überschuss 800 ≥ 700: beide an", d["a"].on and d["b"].on)
    d = run(off, inp(800, 800, soc=60))
    check("Batterie 60 % < 80 %: nicht starten", not d["a"].on and "Batterie" in d["a"].reason)
    d = run(off, inp(800, 100))
    check("Prognose reicht nicht: nicht starten", not d["a"].on and "Prognose" in d["a"].reason)
    d = run(off, inp(800, None))
    check("ohne Prognose zählt der aktuelle Überschuss", d["a"].on)
    fresh_off = {"a": State(False, T - 5 * MINUTE_MS)}
    P1 = Params(machines=[Machine("a", 200)], on_delay_min=0, off_delay_min=0, min_on_min=30, min_off_min=30)
    check("Mindestpause verhindert Neustart", not decide(P1.machines, fresh_off, inp(900, 900), P1, T)[0].on)

    on = {"a": State(True, long_ago), "b": State(True, long_ago)}
    d = run(on, inp(700, 800))
    check("Überschuss 700 hält beide (Abschaltschwelle 400 bzw. 200)", d["a"].on and d["b"].on)
    d = run(on, inp(350, 800))
    check("Überschuss 350 < 400: zweiter aus, erster bleibt", d["a"].on and not d["b"].on)
    d = run(on, inp(50, 800))
    check("Überschuss 50: beide aus", not d["a"].on and not d["b"].on)
    d = run(on, inp(700, 100, soc=80))
    check("schlechte Prognose bei 80 % Batterie: aus (Entladung droht)", not d["b"].on)
    d = run(on, inp(700, 100, soc=98))
    check("schlechte Prognose bei fast voller Batterie: läuft weiter", d["a"].on and d["b"].on)
    fresh_on = {"a": State(True, T - 5 * MINUTE_MS)}
    check("Mindestlaufzeit hält, auch bei wenig Überschuss", decide(P1.machines, fresh_on, inp(-500, None), P1, T)[0].on)
    check("Batterie unter Minimum stoppt trotz Mindestlaufzeit", not decide(P1.machines, fresh_on, inp(900, 900, soc=30), P1, T)[0].on)
    d = run(on, inp(None, None, complete=False))
    check("unvollständige Daten: Zustand bleibt", d["a"].on and d["b"].on and not d["a"].changed)
    d = run(off, inp(None, None, complete=False))
    check("unvollständige Daten: aus bleibt aus", not d["a"].on)

    # --- delays: a change applies only after its condition has held for a while
    PD = Params(machines=[Machine("a", 200)], on_delay_min=15, off_delay_min=20, min_on_min=30, min_off_min=30)
    st = {"a": State(False, long_ago, 0)}
    d = decide(PD.machines, st, inp(900, 900), PD, T)[0]
    check("Start-Bedingung erfüllt: zunächst abwarten", not d.on and not d.changed and d.pending_ms == T and "abwarten" in d.reason)
    st = {"a": State(False, long_ago, T)}
    check("nach 10 min noch abwarten", not decide(PD.machines, st, inp(900, 900), PD, T + 10 * MINUTE_MS)[0].on)
    d = decide(PD.machines, st, inp(900, 900), PD, T + 15 * MINUTE_MS)[0]
    check("nach 15 min startet er", d.on and d.changed)
    d = decide(PD.machines, st, inp(100, 900), PD, T + 10 * MINUTE_MS)[0]
    check("Bedingung verschwindet: Wartezeit beginnt von vorn", not d.on and d.pending_ms == 0)
    on_state = {"a": State(True, long_ago, 0)}
    d = decide(PD.machines, on_state, inp(0, 900), PD, T)[0]
    check("Stopp-Bedingung: zunächst abwarten, läuft weiter", d.on and d.pending_ms == T and "abwarten" in d.reason)
    on_state = {"a": State(True, long_ago, T)}
    check("nach 20 min stoppt er", not decide(PD.machines, on_state, inp(0, 900), PD, T + 20 * MINUTE_MS)[0].on)
    check("Batterie unter Minimum stoppt ohne Wartezeit",
          not decide(PD.machines, {"a": State(True, long_ago, 0)}, inp(900, 900, soc=30), PD, T)[0].on)

    # --- config
    check("Rechner-Konfiguration", [(m.name, m.watts) for m in parse_machines("winola:200, gamer:250")] == [("winola", 200), ("gamer", 250)])

    print("Alle Tests bestanden" if failures == 0 else f"{failures} Test(s) fehlgeschlagen")
    return failures == 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(0 if selftest() else 1)
    main()
