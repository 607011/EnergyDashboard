"""Replays the decision rules over the stored history, to tune the settings.

    docker compose run --rm --no-deps compute-controller python backtest.py [days]

Steps through the last days in 5-minute steps, rebuilds production, consumption and battery level
from Redis and runs the same `decide` as the controller. There are no historical forecasts, so the
forecast rule is left out; the machines' own consumption isn't in the history either, so the
headroom is the one *without* machines, which is what the controller works with anyway.
Prints run hours and the number of switching events for a few variants of the settings.
"""

import dataclasses
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import redis

import controller as c

STEP = 300_000  # 5 minutes


def load(r, days):
    now = int(time.time() * 1000)
    start = now - int(days * 24 * 3600 * 1000)

    def agg(key):
        try:
            return {t: v for t, v in r.ts().range(key, start, now, aggregation_type="avg", bucket_size_msec=STEP)}
        except redis.ResponseError as exc:
            # a series that doesn't exist is fine, anything else must not pass silently
            if "does not exist" not in str(exc) and "key" not in str(exc).lower():
                raise
            return {}

    return {
        "now": now, "start": start,
        "se": agg(c.PV_KEYS_SOLAREDGE),
        "hm": [agg(k) for k in c.hoymiles_keys(r)],
        "house_total": agg(c.HOUSE_KEY),
        "house_se": agg("ts:inverter:house_consumption"),
        "soc": agg("ts:battery:battery1:soe"),
    }


def replay(data, params, tz, list_events=0):
    def smooth(get, t):
        vals = [v for v in (get(t - i * STEP) for i in range(max(1, int(params.smooth_min * 60_000 // STEP)))) if v is not None]
        return sum(vals) / len(vals) if vals else None

    def house(t):
        if t in data["house_total"]:
            return data["house_total"][t]
        if t in data["house_se"]:
            return data["house_se"][t] + sum(s.get(t, 0.0) for s in data["hm"])
        return None

    def soc_at(t):
        for back in range(0, 4):
            if t - back * STEP in data["soc"]:
                return data["soc"][t - back * STEP]

    states = {}
    on_min = {m.name: 0 for m in params.machines}
    solar_wh = shortfall_wh = 0.0   # machine energy covered by the surplus / taken from battery or grid
    events = []
    t = data["start"] - data["start"] % STEP
    while t <= data["now"]:
        se = smooth(lambda x: data["se"].get(x), t)
        hm = [smooth(lambda x, s=s: s.get(x), t) for s in data["hm"]]
        hs = smooth(house, t)
        complete = se is not None and hs is not None
        pv = (se + sum(v or 0 for v in hm)) if complete else None
        inputs = c.Inputs(complete, pv - hs if complete else None, None, soc_at(t))
        for d in c.decide(params.machines, states, inputs, params, t):
            prev = states.get(d.machine.name, c.State(False, 0, 0))
            states[d.machine.name] = c.State(d.on, t if d.changed else prev.since_ms, d.pending_ms)
            if d.changed:
                events.append((t, d.machine.name, d.on, d.reason))
            if d.on:
                on_min[d.machine.name] += STEP // 60_000
        if inputs.headroom_w is not None:
            load = sum(m.watts for m in params.machines if states[m.name].on)
            available = max(inputs.headroom_w, 0.0)
            hours = STEP / 3_600_000
            solar_wh += min(load, available) * hours
            shortfall_wh += max(0.0, load - available) * hours
        t += STEP
    return on_min, events, solar_wh, shortfall_wh


VARIANTS = {
    "STANDARD (Mittel 15, Wartezeiten 15/20, Mindestzeiten 45/45)": {},
    "Mittel 10, Wartezeiten 20/20, Mindestzeiten 45/45": {"on_delay_min": 20, "off_delay_min": 20, "min_on_min": 45, "min_off_min": 45},
    "ohne Wartezeit (Mittel 10, Mindestzeiten 30/30)": {"smooth_min": 10, "on_delay_min": 0, "off_delay_min": 0, "min_on_min": 30, "min_off_min": 30},
    "Wartezeiten 30/30, Mindestzeiten 60/60": {"on_delay_min": 30, "off_delay_min": 30, "min_on_min": 60, "min_off_min": 60},
    "Mittel 20 min, Wartezeiten 20/30, Mindestzeiten 45/45": {"smooth_min": 20, "on_delay_min": 20, "off_delay_min": 30, "min_on_min": 45, "min_off_min": 45},
}


if __name__ == "__main__":
    days = float(sys.argv[1]) if len(sys.argv) > 1 else 4
    r = redis.Redis(host=os.environ.get("REDIS_HOST", "redis"), decode_responses=True)
    tz = ZoneInfo(os.environ.get("TIMEZONE", "UTC"))
    base = c.params_from_env()
    if not base.machines:
        base.machines = c.parse_machines("a:200,b:250,c:100")
    data = load(r, days)
    span_days = (data["now"] - data["start"]) / 86_400_000
    print(f"Rücklauf über {span_days:.1f} Tage, Rechner: " + ", ".join(f"{m.name} {m.watts:.0f} W" for m in base.machines))
    for name, changes in VARIANTS.items():
        params = dataclasses.replace(base, **changes)
        on_min, events, solar_wh, shortfall_wh = replay(data, params, tz)
        hours = ", ".join(f"{k} {v / 60:.1f} h" for k, v in on_min.items())
        total = solar_wh + shortfall_wh
        print(f"\n{name}\n  Laufzeit: {hours}\n  Schaltvorgänge: {len(events)} ({len(events) / span_days:.1f} pro Tag)"
              f"\n  Rechner-Energie: {total / 1000:.1f} kWh, davon aus dem Überschuss {solar_wh / 1000:.1f} kWh, "
              f"aus Batterie/Netz {shortfall_wh / 1000:.1f} kWh ({100 * shortfall_wh / total if total else 0:.0f} %)")
