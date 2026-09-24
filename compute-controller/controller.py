"""Decides when the compute machines (PrimeGrid) should run, from the PV surplus and a forecast.

One shared priority queue decides, cumulatively, which controllable loads get to run on PV
surplus -- the order of COMPUTE_MACHINES *is* the priority: the load listed first gets first claim
on the surplus, the next one only gets what's left, and so on, all the way down. Two loads can't
independently think they each have the same surplus available, because there is only one queue.

Two kinds of load, per machine ("name:watts" or "name:watts:kind" in COMPUTE_MACHINES):
  - "pc" (default): a PC to run PrimeGrid on. Dry run unless PC_<NAME>_ACTUATOR is set (see
    pc_actuator.py's module docstring for the two methods, "shelly" and "wol", and their exact
    on/off sequencing); a machine without it configured just gets decided, never actuated.
  - "tuya": a local, cloud-free Tuya device (e.g. a dehumidifier) switched for real over the LAN
    (see tuya_client.py and the README for how to get its id/local_key).
  - "weishaupt": the heat pump's hot water, boosted on surplus (Normal temperature raised to
    COMPUTE_DHW_TARGET_C plus a push). It only asks for surplus while the water is well below the
    target (see dhw_idle_reason) -- otherwise it reserves nothing. weishaupt-poller does the Modbus
    writes; this only puts the wish into Redis (see that poller's docstring).

Either kind's real actuation is additionally guarded by COMPUTE_MODE: "dryrun" (default) never
actuates anything; "live" actuates every machine that has its actuator configured (a "pc" without
PC_<NAME>_ACTUATOR, or a "tuya" without a working device config, stays dry-run regardless).

Every cycle it works out
  - headroom: PV production minus house consumption *without* the compute machines (15-minute
    means), i.e. the power that would otherwise be exported or charge the battery
  - a forecast of that headroom for the next hour: the measured production (last 30 minutes),
    scaled by how the irradiance forecast for the next hour compares with that of the same last
    30 minutes (15-minute forecast values; comparing with the whole last hour instead
    double-counted the trend: too optimistic while rising, too pessimistic while falling)
and then decides per machine, in priority order (the order of COMPUTE_MACHINES):
  - ON  when the headroom covers the machines up to and including this one plus a margin, the
        forecast headroom does too, and the battery is charged enough
  - OFF when the headroom no longer covers them, or the forecast says it won't and the battery
        isn't nearly full (so battery use is to be feared), or the battery gets too low
  - a change only applies once its condition has held for a while (clouds pass, the battery bridges
    short dips), and machines keep a minimum run time and pause; both against flapping
    (tuned with backtest.py: about 4-5 switching events per day instead of 15)

Reads:  ts:inverter:power_pv_total, ts:hoymiles:*:power_w, ts:inverter:house_consumption_total,
        solaredge:battery:battery1:latest (soe), Open-Meteo irradiance (hourly + 15-minute)
Writes: compute:latest, compute:<machine>:latest (also feeds machine_power() back into next
        cycle's headroom, whether measured by a Shelly or, for now, estimated from the decision),
        compute:<machine>:state, compute:events (list), tuya:<machine>:latest (raw device
        telemetry, "tuya" machines only), ts:compute:headroom_w, ts:compute:forecast_headroom_w,
        ts:compute:<machine>:desired, ts:compute:<machine>:threshold_on_w
"""

import logging
import os
import signal
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import redis
import requests

from tuya_client import TuyaDevice, TuyaDeviceError
import pc_actuator

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("compute-controller")
logging.getLogger("paramiko").setLevel(logging.WARNING)  # else two lines per SSH connection, every minute

MINUTE_MS = 60_000
HOUR_S = 3600
QUARTER_S = 900
# Window of the measured production the forecast is anchored on; the irradiance it is compared
# with must cover the same window, or the trend gets counted twice.
PV_RECENT_MIN = 30


# --------------------------------------------------------------------------- configuration

@dataclass
class Machine:
    name: str
    watts: float
    kind: str = "pc"           # "pc" or "tuya" (really switched, see module docstring)
    tuya_dps_switch: str = "1"  # DPS id of the on/off switch (almost always "1", but verify)
    # "pc" real actuation, opt-in per machine (PC_<NAME>_*, see module docstring); None = dry run.
    pc_actuator_kind: str | None = None  # "shelly" (Windows: SSH shutdown + cut the plug) or
                                         # "wol" (e.g. a Mac that won't reboot on power restore:
                                         # SSH sleep + Wake-on-LAN, its plug is never touched)
    pc_ssh_host: str | None = None
    pc_ssh_user: str | None = None
    pc_mac: str | None = None


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
    incomplete_off_min: float = 30.0  # without complete data this long, stop the machines (safe state)
    w_per_wm2: float = 10.0         # fallback conversion irradiance -> PV power (learned if enough data)
    battery_full_by_hour: float = 12.0   # try to have the battery full by then (local time) ...
    battery_max_charge_w: float = 2500.0 # ... it can't absorb more than this; the rest is exported
    battery_capacity_wh: float | None = None  # None = read from the battery (maximum_energy)
    forecast_derate: float = 0.8         # safety factor on the irradiance -> PV power forecast
    pc_ssh_key: str = "/run/secrets/pc_ssh_key"  # private key for "pc" actuation over SSH
    shelly_hosts: dict = None  # name -> host, from SHELLY_DEVICES (for the "shelly" pc actuator)
    # "weishaupt" hot-water boost
    weishaupt_name: str = "wgb14"      # weishaupt-poller's WEISHAUPT_NAME
    dhw_target_c: float = 55.0         # boost the hot water to this (a WGB 14 trips its high-pressure
                                       # switch, warning 15, as the water nears 60 degC: 58.5 did) ...
    dhw_start_delta_k: float = 5.0     # ... once it's at least this far below it
    dhw_max_per_day: int = 2           # boosts started per day (each writes stored settings)
    dhw_push_min: int = 120
    dhw_circulation: str = ""          # Shelly (SHELLY_DEVICES name) of the circulation pump, run while boosting


def parse_shelly_hosts(spec: str) -> dict:
    hosts = {}
    for entry in spec.split(","):
        entry = entry.strip()
        if not entry:
            continue
        name, _, host = entry.partition(":")
        hosts[name.strip()] = host.strip()
    return hosts


def parse_machines(spec: str, env=os.environ) -> list:
    machines = []
    for entry in spec.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":")
        name, watts = parts[0].strip(), float(parts[1])
        kind = parts[2].strip() if len(parts) > 2 else "pc"
        prefix = f"PC_{name.upper()}_"
        machines.append(Machine(
            name, watts, kind,
            tuya_dps_switch=env.get(f"TUYA_{name.upper()}_DPS_SWITCH", "1"),
            pc_actuator_kind=env.get(prefix + "ACTUATOR") or None,
            pc_ssh_host=env.get(prefix + "SSH_HOST"),
            pc_ssh_user=env.get(prefix + "SSH_USER"),
            pc_mac=env.get(prefix + "MAC"),
        ))
    return machines


def params_from_env(env=os.environ) -> Params:
    def f(key, default):
        return float(env.get(key, default))
    return Params(
        machines=parse_machines(env.get("COMPUTE_MACHINES", ""), env),
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
        incomplete_off_min=f("COMPUTE_INCOMPLETE_OFF_MIN", 30),
        w_per_wm2=f("COMPUTE_W_PER_WM2", 10),
        battery_full_by_hour=f("COMPUTE_BATTERY_FULL_BY_HOUR", 12),
        battery_max_charge_w=f("COMPUTE_BATTERY_MAX_CHARGE_W", 2500),
        battery_capacity_wh=float(env["COMPUTE_BATTERY_CAPACITY_WH"]) if env.get("COMPUTE_BATTERY_CAPACITY_WH") else None,
        forecast_derate=f("COMPUTE_FORECAST_DERATE", 0.8),
        pc_ssh_key=env.get("PC_SSH_KEY_PATH", "/run/secrets/pc_ssh_key"),
        shelly_hosts=parse_shelly_hosts(env.get("SHELLY_DEVICES", "")),
        weishaupt_name=env.get("WEISHAUPT_NAME", "wgb14"),
        dhw_target_c=f("COMPUTE_DHW_TARGET_C", 55),
        dhw_start_delta_k=f("COMPUTE_DHW_START_DELTA_K", 5),
        dhw_max_per_day=int(f("COMPUTE_DHW_MAX_PER_DAY", 2)),
        dhw_push_min=int(f("COMPUTE_DHW_PUSH_MIN", 120)),
        dhw_circulation=env.get("COMPUTE_DHW_CIRCULATION", ""),
    )


# --------------------------------------------------------------------------- forecast (pure)

def forecast_window_mean(values, start_s, end_s, step_s=HOUR_S):
    """Mean of Open-Meteo values over [start_s, end_s].

    Each value is the mean of the `step_s` *preceding* its timestamp (1 h for hourly, 15 min for
    minutely_15 data), so a point stamped T covers (T - step, T]. `values` is a list of
    (T seconds, value). Returns None unless the data covers the whole window.
    """
    total = covered = 0.0
    for t_end, value in values:
        if value is None:
            continue
        overlap = min(end_s, t_end) - max(start_s, t_end - step_s)
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
    the plant-specific conversion error. `f_ref` must cover the same window as `pv_recent_w`.
    Around sunrise and sunset the reference irradiance is
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
    battery_w: float | None = None  # signed battery power, + charging / - discharging
    # load_w -> (ok, text): may this much machine load take surplus away from the battery and it
    # still gets full by the deadline (see battery_budget)? None = no forecast, fall back to soc_on.
    budget: object = None
    # name -> reason: machines with nothing to do right now (e.g. hot water already hot). They are
    # off, reserve nothing and skip every delay -- having no demand is not flapping.
    idle: dict = None
    incomplete_min: float | None = None  # how long the data has been incomplete (None = complete)


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


DISCHARGING_W = 50.0  # battery power below minus this counts as "discharging"
WEISHAUPT_STALE_MS = 5 * MINUTE_MS


def dhw_idle_reason(wp: dict, dhw_state: dict, boosting: bool, params: "Params", now_ms: int, today: str):
    """Why the hot-water boost has nothing to do now (None = it may compete for surplus).

    wp: weishaupt:<name>:latest; dhw_state: compute:<name>:dhw (date, starts, blocked).
    A running boost continues until the target is reached; a new one only starts once the water
    has cooled to target - start_delta, at most dhw_max_per_day times a day, and never again on a
    day an electric heater came on during one (burning surplus at COP 1 is exactly what not to do)
    or the heat pump raised a warning (e.g. 15, high-pressure switch: the water got too hot for it).
    """
    try:
        fresh = now_ms - int(wp.get("updated_at", 0)) <= WEISHAUPT_STALE_MS
        dhw = float(wp["dhw_temp"])
    except (KeyError, ValueError):
        fresh = False
    if not fresh:
        return "keine aktuellen Daten der Wärmepumpe"
    if wp.get("fault_free") == "0":
        return "Wärmepumpe meldet eine Störung"
    today_state = dhw_state if dhw_state.get("date") == today else {}
    if today_state.get("blocked") == "1":
        return "Heizstab oder Warnung während eines Boosts – heute kein Boost mehr"
    if boosting and (wp.get("heater1_on") == "1" or wp.get("heater2_on") == "1"):
        return "Heizstab eingeschaltet – Boost abgebrochen"
    if wp.get("warning_code") not in (None, ""):
        return (f"Wärmepumpe meldet Warnung {wp['warning_code']}"
                + (" – Boost abgebrochen" if boosting else ""))
    target = params.dhw_target_c
    if boosting:
        return f"Ziel {target:.0f} °C erreicht ({dhw:.1f} °C)" if dhw >= target else None
    if dhw > target - params.dhw_start_delta_k:
        return f"Warmwasser {dhw:.1f} °C, Boost erst unter {target - params.dhw_start_delta_k:.1f} °C"
    if int(today_state.get("starts", 0)) >= params.dhw_max_per_day:
        return f"heute schon {params.dhw_max_per_day} Boosts"
    return None


def decide(machines, states, inputs: Inputs, params: Params, now_ms: int):
    """Desired state per machine, in priority order.

    Priority with fill-in: a machine's load is counted on top of the higher-priority machines
    that run or are about to start (waiting out their on-delay) -- those keep their claim. A
    higher-priority machine that doesn't fit at all doesn't block a smaller one further down.

    Starting needs the surplus to cover the load plus a margin *and* the battery budget to allow
    it: with a forecast, the battery must still get full by the deadline despite the load (see
    battery_budget); without one, the old fixed soc_on threshold applies instead. A change needs
    its condition to hold for on_delay_min / off_delay_min first, on top of the minimum on and off
    times. The soc_min emergency stop only fires while the battery is actually discharging.
    """
    decisions = []
    reserved = 0.0
    for machine in machines:
        load = reserved + machine.watts
        state = states.get(machine.name) or State(False, 0, 0)
        if inputs.idle and machine.name in inputs.idle:
            decisions.append(Decision(machine, False, state.on, load + params.margin_on_w, inputs.idle[machine.name]))
            continue
        on_threshold = load + params.margin_on_w
        off_threshold = load - params.tolerance_off_w
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
            gone = inputs.incomplete_min or 0.0
            if state.on and gone >= params.incomplete_off_min:
                # Unknown surplus for this long: off is the safe state (else, e.g. after sunset,
                # the machines would run on from the battery unnoticed).
                d = result(False, f"seit {gone:.0f} min keine vollständigen Daten – sicherheitshalber aus")
            else:
                d = result(state.on, "keine Entscheidung: Erzeugungs- oder Verbrauchsdaten unvollständig"
                           + (f" (seit {gone:.0f} min, aus nach {params.incomplete_off_min:.0f})" if gone else ""),
                           state.pending_ms)
        else:
            h, fc, soc = inputs.headroom_w, inputs.forecast_headroom_w, inputs.soc
            discharging = inputs.battery_w is not None and inputs.battery_w < -DISCHARGING_W
            budget = inputs.budget(load) if inputs.budget else None
            if state.on:
                if soc is not None and soc < params.soc_min and discharging:
                    d = result(False, f"Batterie {soc:.0f} % unter {params.soc_min:.0f} % und entlädt")
                elif age_min < params.min_on_min:
                    d = result(True, f"läuft, Mindestlaufzeit ({age_min:.0f} von {params.min_on_min:.0f} min)")
                elif h < off_threshold:
                    d = after_delay(False, f"Überschuss {h:.0f} W deckt {load:.0f} W nicht mehr", params.off_delay_min)
                elif budget is not None and not budget[0]:
                    d = after_delay(False, f"{budget[1]} – Batterie hat Vorrang", params.off_delay_min)
                elif budget is None and fc is not None and fc < off_threshold and (soc is None or soc < params.soc_fc_off):
                    d = after_delay(False, f"Prognose {fc:.0f} W reicht nicht, Batterie "
                                    + (f"{soc:.0f} %" if soc is not None else "unbekannt")
                                    + " (Entladung droht)", params.off_delay_min)
                else:
                    d = result(True, f"läuft: Überschuss {h:.0f} W ≥ {off_threshold:.0f} W"
                               + (f"; {budget[1]}" if budget else ""))
            else:
                if age_min < params.min_off_min and state.since_ms > 0:
                    d = result(False, f"aus, Mindestpause ({age_min:.0f} von {params.min_off_min:.0f} min)")
                elif h < on_threshold:
                    d = result(False, f"Überschuss {h:.0f} W unter {on_threshold:.0f} W")
                elif budget is not None and not budget[0]:
                    d = result(False, f"{budget[1]} – Batterie hat Vorrang")
                elif budget is None and (soc is None or soc < params.soc_on):
                    d = result(False, "Batterie nicht bekannt" if soc is None
                               else f"Batterie {soc:.0f} % unter {params.soc_on:.0f} %")
                elif fc is not None and fc < load:
                    d = result(False, f"Prognose {fc:.0f} W reicht für {load:.0f} W nicht")
                else:
                    detail = budget[1] if budget else f"Batterie {soc:.0f} %"
                    # The on-delay guards against starting in a brief sunny spell and then running
                    # the minimum on-time from the battery. With a battery budget reserve of at
                    # least twice that worst case, it's pointless: start right away.
                    worst_wh = machine.watts * params.min_on_min / 60
                    reserve = budget[2] if budget and len(budget) > 2 else None
                    if reserve is not None and reserve >= 2 * worst_wh:
                        d = result(True, f"Überschuss {h:.0f} W ≥ {on_threshold:.0f} W; {detail} – sofort, "
                                   "Reserve reicht für einen Fehlstart")
                    else:
                        d = after_delay(True, f"Überschuss {h:.0f} W ≥ {on_threshold:.0f} W; {detail}", params.on_delay_min)
        decisions.append(d)
        if d.on or (not state.on and d.pending_ms):
            reserved += machine.watts
    return decisions


def battery_budget(hourly, now_s, soft_deadline_s, day_end_s, soc, capacity_wh, max_charge_w, house_w,
                   w_per_wm2, derate, soft_label="12:00"):
    """Whether machines may take surplus away from the battery, from today's irradiance forecast.

    Returns load_w -> (ok, text, reserve_wh), or None without the needed inputs; reserve_wh is by
    how much the battery would overshoot its need (inf when it's already full). The battery can charge from
    the forecast surplus (derate * w_per_wm2 * irradiance - house - load), but never faster than
    max_charge_w -- whatever is above that is exported anyway and free for the machines. `ok` means
    the battery still gets full by the deadline with this load running all the way: the soft
    deadline (noon) if the battery could make that at all, otherwise the end of the day.
    """
    if soc is None or not capacity_wh or house_w is None or not hourly:
        return None
    need = max(0.0, (100.0 - soc) / 100.0 * capacity_wh)

    def achievable(load, until):
        total = 0.0
        for t_end, ghi in hourly:
            if ghi is None:
                continue
            start, end = max(now_s, t_end - HOUR_S), min(until, t_end)
            if end <= start:
                continue
            surplus = derate * w_per_wm2 * ghi - house_w - load
            total += min(max(surplus, 0.0), max_charge_w) * (end - start) / HOUR_S
        return total

    soft = now_s < soft_deadline_s and achievable(0.0, soft_deadline_s) >= need
    until, label = (soft_deadline_s, soft_label) if soft else (day_end_s, "Abend")

    def check(load):
        if need <= 0:
            return True, "Batterie voll", float("inf")
        got = achievable(load, until)
        return got >= need, f"Batterie {soc:.0f} %, bis {label} {got / 1000:.1f} von {need / 1000:.1f} kWh", got - need
    return check


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


def build_tuya_devices(machines: list, env=os.environ) -> dict:
    """One TuyaDevice per "tuya"-kind machine, from TUYA_<NAME>_ID/KEY/IP/VERSION. A machine
    missing its id/key is skipped (logged once) rather than raising, so a typo in one device's
    config doesn't take the whole controller down."""
    devices = {}
    for m in machines:
        if m.kind != "tuya":
            continue
        prefix = f"TUYA_{m.name.upper()}_"
        device_id, local_key = env.get(prefix + "ID"), env.get(prefix + "KEY")
        if not device_id or not local_key:
            log.error("%s is a tuya machine but %sID/%sKEY are not set -- leaving it out", m.name, prefix, prefix)
            continue
        devices[m.name] = TuyaDevice(m.name, device_id, local_key, env.get(prefix + "IP"), env.get(prefix + "VERSION", "3.3"))
    return devices


def tuya_target_state(desired_on: bool, polled_on: bool | None) -> bool:
    """Whether the switch command should be (re-)sent: on an actual change, or to correct drift
    from a manual toggle (e.g. someone used the Tuya app) or a command that didn't take effect --
    the just-polled state disagrees with what we intend."""
    return polled_on is None or polled_on != desired_on


def poll_and_actuate_tuya(r, dev: TuyaDevice, machine, want_on: bool, live: bool, now_ms: int) -> float | None:
    """Reads the device's telemetry (written to tuya:<name>:latest / its time series), and in
    live mode sends the switch command when needed (see tuya_target_state). Returns the machine's
    power draw to feed back into next cycle's headroom (measured if the device reports it, else
    the configured wattage while on, 0 while off) -- or None if the device couldn't be reached.
    """
    try:
        dps = dev.status()
    except (TuyaDeviceError, OSError) as exc:
        log.warning("%s: could not read the device (%s)", machine.name, exc)
        return None

    polled_on = dps.get(machine.tuya_dps_switch)
    polled_on = bool(polled_on) if isinstance(polled_on, bool) else None
    if live and tuya_target_state(want_on, polled_on):
        try:
            dev.set_switch(machine.tuya_dps_switch, want_on)
            log.info("%s: sent switch %s", machine.name, "on" if want_on else "off")
        except (TuyaDeviceError, OSError) as exc:
            log.warning("%s: could not send the switch command (%s)", machine.name, exc)

    pipe = r.pipeline(transaction=False)
    mapping = {f"dps_{k}": str(v) for k, v in dps.items()}
    mapping["updated_at"] = str(now_ms)
    pipe.delete(f"tuya:{machine.name}:latest")
    pipe.hset(f"tuya:{machine.name}:latest", mapping=mapping)
    for k, v in dps.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        pipe.ts().add(f"ts:tuya:{machine.name}:dps_{k}", now_ms, float(v), retention_msecs=RETENTION_MS,
                      labels={"device": machine.name, "field": f"dps_{k}"}, duplicate_policy="last")
    pipe.execute()

    effective_on = want_on if live else bool(polled_on)
    return machine.watts if effective_on else 0.0


def actuate_pc(r: redis.Redis, params: "Params", machine: Machine, want_on: bool, live: bool) -> tuple[bool, bool]:
    """Runs the SSH/Shelly/WoL actions for a "pc" machine with an actuator configured (see
    pc_actuator.py); does nothing if PC_<NAME>_ACTUATOR isn't set (stays dry-run). Returns
    (shutdown_sent, owned): whether a shutdown is still being waited out, and whether the
    controller itself switched the machine on (it only switches off what it switched on). Both
    are persisted in compute:<name>:latest by the caller and read back here next cycle.
    """
    owned = r.hget(f"compute:{machine.name}:latest", "owned") == "1"
    if not machine.pc_actuator_kind:
        return False, owned
    plug = r.hgetall(f"shelly:{machine.name}:latest")
    try:
        plug_fresh = plug and (int(r.hget(f"shelly:{machine.name}:latest", "updated_at") or 0))
    except (TypeError, ValueError):
        plug_fresh = False
    plug_on = plug.get("on") == "True" if plug else None
    try:
        power_w = float(plug["power_w"]) if plug.get("power_w") not in (None, "") else None
    except ValueError:
        power_w = None
    shutdown_sent = r.hget(f"compute:{machine.name}:latest", "shutdown_sent") == "1"

    if machine.pc_actuator_kind == "shelly":
        actions = pc_actuator.shelly_pc_actions(want_on, plug_on, power_w, shutdown_sent, owned)
    elif machine.pc_actuator_kind == "wol":
        actions = pc_actuator.wol_pc_actions(want_on, power_w, owned)
    else:
        log.error("%s: unknown PC_%s_ACTUATOR=%r", machine.name, machine.name.upper(), machine.pc_actuator_kind)
        return shutdown_sent, owned

    if not actions or not live:
        if actions and not live:
            log.info("%s: would %s (dry run, COMPUTE_MODE != live)", machine.name, ", ".join(actions))
        return shutdown_sent, owned

    plug_host = params.shelly_hosts.get(machine.name)
    for action in actions:
        try:
            if action in ("shelly_on", "shelly_off"):
                if not plug_host:
                    raise pc_actuator.ActuationError(f"no SHELLY_DEVICES entry named {machine.name!r}")
                pc_actuator.shelly_set_switch(plug_host, action == "shelly_on")
            elif action == "ssh_shutdown":
                pc_actuator.ssh_run(machine.pc_ssh_host, machine.pc_ssh_user, params.pc_ssh_key, "shutdown /s /t 0")
                shutdown_sent = True
            elif action == "ssh_sleep":
                pc_actuator.ssh_run(machine.pc_ssh_host, machine.pc_ssh_user, params.pc_ssh_key, pc_actuator.MAC_SLEEP)
            elif action == "wol":
                pc_actuator.send_wol(machine.pc_mac)
                owned = True  # the packet is out; the full-wake step below is retried next cycle if it fails
                time.sleep(pc_actuator.WOL_SETTLE_S)
                pc_actuator.ssh_run(machine.pc_ssh_host, machine.pc_ssh_user, params.pc_ssh_key,
                                    pc_actuator.MAC_FULL_WAKE, wait=True)
            elif action == "keep_awake":
                pc_actuator.ssh_run(machine.pc_ssh_host, machine.pc_ssh_user, params.pc_ssh_key,
                                    pc_actuator.MAC_KEEP_AWAKE, wait=True)
                continue  # runs every cycle, not worth a log line
            log.info("%s: %s", machine.name, action)
            if action in ("shelly_on", "wol"):
                owned = True
            elif action in ("shelly_off", "ssh_sleep"):
                owned = False
        except pc_actuator.ActuationError as exc:
            log.warning("%s: %s failed (%s)", machine.name, action, exc)
    if "shelly_off" in actions:
        shutdown_sent = False
    return shutdown_sent, owned


def actuate_dhw(r, params: "Params", machine: Machine, want_on: bool, live: bool, now_ms: int, wp: dict) -> float:
    """Hands the hot-water boost wish to weishaupt-poller -- refreshed every cycle with a deadline,
    so if this controller stops, the poller restores the heat pump's setting on its own -- and
    runs the circulation pump while boosting (its own timer turns it off again). Returns the power
    to count for the machine: the estimate while the compressor runs, else 0."""
    active = want_on and live
    r.hset(f"weishaupt:{params.weishaupt_name}:boost", mapping={
        "active": int(active), "target_c": params.dhw_target_c, "push_min": params.dhw_push_min,
        "deadline_ms": now_ms + 10 * MINUTE_MS, "updated_at": now_ms})
    if active and params.dhw_circulation:
        host = (params.shelly_hosts or {}).get(params.dhw_circulation)
        try:
            if not host:
                raise pc_actuator.ActuationError(f"no SHELLY_DEVICES entry named {params.dhw_circulation!r}")
            pc_actuator.shelly_set_switch(host, True, toggle_after_s=600)
        except pc_actuator.ActuationError as exc:
            log.warning("%s: circulation pump not switched (%s)", machine.name, exc)
    try:
        running = float(wp.get("power_demand_pct") or 0) > 0
    except ValueError:
        running = False
    return machine.watts if active and running else 0.0


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
    """Irradiance from Open-Meteo, hourly (whole-day battery budget) and 15-minute (next-hour
    forecast; in Central Europe from ICON-D2, elsewhere interpolated), cached for 30 minutes."""

    def __init__(self, lat, lon):
        self.lat, self.lon = lat, lon
        self.hourly = []
        self.quarter = []
        self.fetched = 0.0

    def get(self):
        if self.lat is None or self.lon is None:
            return []
        if time.monotonic() - self.fetched > 1800:
            try:
                response = requests.get("https://api.open-meteo.com/v1/forecast", params={
                    "latitude": self.lat, "longitude": self.lon, "hourly": "shortwave_radiation",
                    "minutely_15": "shortwave_radiation", "past_days": 1, "forecast_days": 2, "timezone": "UTC"}, timeout=10)
                response.raise_for_status()
                body = response.json()

                def series(block):
                    data = body.get(block) or {}
                    return [(int(datetime.fromisoformat(t).replace(tzinfo=timezone.utc).timestamp()), v)
                            for t, v in zip(data.get("time", []), data.get("shortwave_radiation", []))]
                self.hourly = series("hourly")
                self.quarter = series("minutely_15")
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


def cycle(r, params, forecast, tz, learned, now_ms, tuya_devices=None):
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

    battery = r.hgetall(BATTERY_KEY)
    def num(field):
        try:
            return float(battery[field]) if battery.get(field) not in (None, "") else None
        except ValueError:
            return None
    soc = num("soe")
    battery_w = num("instantaneous_power")
    capacity_wh = params.battery_capacity_wh or num("maximum_energy") or num("rated_energy")

    # forecast of the headroom over the next hour
    hourly = forecast.get()
    ref_start = now_s - PV_RECENT_MIN * 60
    f_next = forecast_window_mean(forecast.quarter, now_s, now_s + HOUR_S, QUARTER_S)
    f_ref = forecast_window_mean(forecast.quarter, ref_start, now_s, QUARTER_S)
    if f_next is None or f_ref is None:  # no 15-minute data: hourly, coarser but the same windows
        f_next = forecast_window_mean(hourly, now_s, now_s + HOUR_S)
        f_ref = forecast_window_mean(hourly, ref_start, now_s)
    pv_recent = ts_mean(r, PV_KEYS_SOLAREDGE, now_ms, PV_RECENT_MIN)
    if pv_recent is not None:
        pv_recent += sum(ts_mean(r, k, now_ms, PV_RECENT_MIN) or 0.0 for k in hoymiles_keys(r))
    pv_forecast = forecast_pv(pv_recent, f_next, f_ref, learned)
    forecast_headroom = (pv_forecast - baseline_house) if pv_forecast is not None and baseline_house is not None else None

    states = {}
    for m in params.machines:
        h = r.hgetall(f"compute:{m.name}:state")
        states[m.name] = (State(h.get("on") == "1", int(h.get("since_ms", 0)), int(h.get("pending_ms", 0)))
                          if h else State(False, 0, 0))

    local_now = datetime.fromtimestamp(now_s, tz)
    midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    soft_deadline = midnight.timestamp() + params.battery_full_by_hour * HOUR_S
    day_end = (midnight + timedelta(days=1)).timestamp()
    soft_label = f"{int(params.battery_full_by_hour):02d}:{int(params.battery_full_by_hour % 1 * 60):02d}"
    budget = battery_budget(hourly, now_s, soft_deadline, day_end, soc, capacity_wh, params.battery_max_charge_w,
                            baseline_house, learned, params.forecast_derate, soft_label)
    base_budget = budget(0.0) if budget else None

    # hot-water boost: is there anything to do at all? (see dhw_idle_reason)
    today = local_now.strftime("%Y-%m-%d")
    idle, weishaupt_values = {}, {}
    for m in params.machines:
        if m.kind != "weishaupt":
            continue
        wp = weishaupt_values[m.name] = r.hgetall(f"weishaupt:{params.weishaupt_name}:latest")
        dhw_state = r.hgetall(f"compute:{m.name}:dhw")
        reason = dhw_idle_reason(wp, dhw_state, states[m.name].on, params, now_ms, today)
        if reason:
            idle[m.name] = reason
        if reason and reason.endswith("Boost abgebrochen"):
            starts = dhw_state.get("starts", 0) if dhw_state.get("date") == today else 0
            r.hset(f"compute:{m.name}:dhw", mapping={"date": today, "starts": starts, "blocked": 1})

    if complete and headroom is not None:
        r.delete("compute:incomplete_since")
        incomplete_min = None
    else:
        r.set("compute:incomplete_since", now_ms, nx=True)
        incomplete_min = (now_ms - int(r.get("compute:incomplete_since"))) / MINUTE_MS

    inputs = Inputs(complete, headroom, forecast_headroom, soc, battery_w, budget, idle, incomplete_min)
    decisions = decide(params.machines, states, inputs, params, now_ms)

    pipe = r.pipeline(transaction=False)
    label = {"device": "compute"}
    def add(key, value):
        pipe.ts().add(key, now_ms, float(value), retention_msecs=RETENTION_MS,
                      labels={**label, "field": key.rsplit(":", 1)[-1]}, duplicate_policy="last")
    def fmt(v, digits=0):
        return "" if v is None else f"{v:.{digits}f}"

    latest = {
        "mode": params.mode, "complete": int(complete), "power_known": int(power_known),
        "production_w": fmt(pv), "house_w": fmt(house), "machines_w": fmt(machines_w),
        "headroom_w": fmt(headroom), "forecast_headroom_w": fmt(forecast_headroom),
        "forecast_pv_w": fmt(pv_forecast), "forecast_irradiance_next": fmt(f_next), "forecast_irradiance_last": fmt(f_ref),
        "soc": fmt(soc), "w_per_wm2": fmt(learned, 1), "updated_at": now_ms,
        "battery_budget": base_budget[1] if base_budget else "",
    }
    pipe.delete("compute:latest")
    pipe.hset("compute:latest", mapping=latest)
    if headroom is not None:
        add("ts:compute:headroom_w", headroom)
    if forecast_headroom is not None:
        add("ts:compute:forecast_headroom_w", forecast_headroom)

    live = params.mode == "live"
    for d in decisions:
        name = d.machine.name
        is_tuya = bool(d.machine.kind == "tuya" and tuya_devices and name in tuya_devices)
        has_pc_actuator = d.machine.kind == "pc" and bool(d.machine.pc_actuator_kind)
        is_dhw = d.machine.kind == "weishaupt"
        actuated = (is_tuya or has_pc_actuator or is_dhw) and live
        if is_dhw and d.changed and d.on:
            h = r.hgetall(f"compute:{name}:dhw")
            same_day = h.get("date") == today
            pipe.hset(f"compute:{name}:dhw", mapping={
                "date": today, "starts": (int(h.get("starts", 0)) if same_day else 0) + 1,
                "blocked": h.get("blocked", 0) if same_day else 0})
        if d.changed:
            pipe.hset(f"compute:{name}:state", mapping={"on": int(d.on), "since_ms": now_ms, "pending_ms": 0})
            stamp = datetime.fromtimestamp(now_s, tz).strftime("%d.%m. %H:%M")
            note = "geschaltet" if actuated else ("Probelauf, kein Aktor" if not (is_tuya or has_pc_actuator or is_dhw) else "Probelauf, nicht geschaltet")
            line = f"{stamp}  {name}: {'EIN' if d.on else 'AUS'} ({note}) – {d.reason}"
            pipe.lpush("compute:events", line)
            pipe.ltrim("compute:events", 0, 199)
            log.info(line)
        else:
            pipe.hset(f"compute:{name}:state", mapping={
                "on": int(d.on), "since_ms": states[name].since_ms, "pending_ms": d.pending_ms})

        # For a live tuya machine this also sends the switch command and returns its real (or
        # estimated) power, which feeds back into next cycle's headroom via machine_power(); for a
        # "pc" machine with an actuator this runs its SSH/Shelly/WoL steps (its power already
        # feeds back on its own, via shelly-poller writing the same compute:<name>:latest fields).
        power_w = None
        shutdown_sent = owned = None
        if is_tuya:
            power_w = poll_and_actuate_tuya(r, tuya_devices[name], d.machine, d.on, live, now_ms)
        elif has_pc_actuator:
            shutdown_sent, owned = actuate_pc(r, params, d.machine, d.on, live)
        elif is_dhw:
            power_w = actuate_dhw(r, params, d.machine, d.on, live, now_ms, weishaupt_values.get(name, {}))
        machine_fields = {
            "desired": int(d.on), "reason": d.reason, "watts": d.machine.watts,
            "threshold_on_w": d.threshold_on_w, "decided_at": now_ms, "actuated": int(actuated),
        }
        if power_w is not None:
            machine_fields["power_w"] = power_w
            machine_fields["power_updated_at"] = now_ms
        if shutdown_sent is not None:
            machine_fields["shutdown_sent"] = int(shutdown_sent)
            machine_fields["owned"] = int(owned)
        pipe.hset(f"compute:{name}:latest", mapping=machine_fields)
        add(f"ts:compute:{name}:desired", int(d.on))
        add(f"ts:compute:{name}:threshold_on_w", d.threshold_on_w)
    pipe.execute()
    return latest, decisions


RETENTION_MS = int(os.environ.get("TS_RETENTION_DAYS", "365")) * 24 * 3600 * 1000


def main():
    params = params_from_env()
    if params.mode not in ("dryrun", "live"):
        log.warning("Unknown COMPUTE_MODE=%s, treating as dryrun", params.mode)
        params.mode = "dryrun"
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    r = redis.Redis(host=os.environ.get("REDIS_HOST", "redis"), port=int(os.environ.get("REDIS_PORT", "6379")),
                    password=os.environ.get("REDIS_PASSWORD") or None, decode_responses=True)
    r.ping()
    lat = float(os.environ["LAT"]) if os.environ.get("LAT") else None
    lon = float(os.environ["LON"]) if os.environ.get("LON") else None
    tz = ZoneInfo(os.environ.get("TIMEZONE", "UTC"))
    forecast = Forecast(lat, lon)
    tuya_devices = build_tuya_devices(params.machines)

    if not params.machines:
        log.info("COMPUTE_MACHINES is empty -- no controllable loads configured, just publishing the PV surplus")
    else:
        log.info("Mode=%s with %d machine(s): %s", params.mode, len(params.machines),
                 ", ".join(f"{m.name} {m.watts:.0f} W ({m.kind})" for m in params.machines))
        for m in params.machines:
            if m.kind == "tuya" and m.name not in tuya_devices:
                log.error("%s: tuya machine without a working device config -- see the error above; "
                         "it will only ever be decided, never actuated, until that's fixed", m.name)

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
            cycle(r, params, forecast, tz, learned, now_ms, tuya_devices)
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
    Q = QUARTER_S
    quarter = [(40 * Q, 100.0), (41 * Q, 200.0), (42 * Q, 300.0)]   # covers 39Q..42Q
    check("15-Min-Werte: letzte halbe Stunde", near(forecast_window_mean(quarter, 40 * Q, 42 * Q, Q), 250.0))
    check("15-Min-Werte: halbe Viertelstunde anteilig", near(forecast_window_mean(quarter, 40.5 * Q, 41.5 * Q, Q), 250.0))

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
    def inp(h, fc=None, soc=90.0, complete=True, battery_w=None, budget=None):
        return Inputs(complete, h, fc, soc, battery_w, budget)
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

    # --- hot-water boost: a machine without demand reserves nothing
    PW = Params(machines=[Machine("ww", 2000, "weishaupt"), Machine("a", 200)], on_delay_min=0,
                off_delay_min=0, min_on_min=30, min_off_min=30)
    dw = {d.machine.name: d for d in decide(PW.machines, {"ww": State(False, long_ago), "a": State(False, long_ago)},
                                            Inputs(True, 450, 800, 90.0, idle={"ww": "Warmwasser warm"}), PW, T)}
    check("Warmwasser ohne Bedarf: aus, reserviert nichts, Rechner startet", not dw["ww"].on and dw["a"].on)
    dw = decide(PW.machines[:1], {"ww": State(True, T - MINUTE_MS)}, Inputs(True, 5000, 5000, 90.0, idle={"ww": "Ziel erreicht"}), PW, T)[0]
    check("Boost am Ziel: sofort aus, trotz Mindestlaufzeit", not dw.on and dw.changed)
    wp = {"updated_at": str(T), "dhw_temp": "45.0", "fault_free": "1", "heater1_on": "0", "heater2_on": "0"}
    today = "2026-09-23"
    PD = Params(machines=[])
    check("Warmwasser 45 °C (< 50): Boost möglich", dhw_idle_reason({**wp, "dhw_temp": "45.0"}, {}, False, PD, T, today) is None)
    check("Warmwasser 52 °C: kein neuer Boost", "erst unter 50.0" in dhw_idle_reason({**wp, "dhw_temp": "52.0"}, {}, False, PD, T, today))
    check("laufender Boost bei 52 °C: weiter", dhw_idle_reason({**wp, "dhw_temp": "52.0"}, {}, True, PD, T, today) is None)
    check("laufender Boost bei 55 °C: Ziel erreicht", "erreicht" in dhw_idle_reason({**wp, "dhw_temp": "55.0"}, {}, True, PD, T, today))
    check("Heizstab während Boost: Abbruch", dhw_idle_reason({**wp, "heater1_on": "1"}, {}, True, PD, T, today).endswith("Boost abgebrochen"))
    check("Warnung (z. B. 15, Hochdruck) während Boost: Abbruch",
          dhw_idle_reason({**wp, "warning_code": "15"}, {}, True, PD, T, today).endswith("Boost abgebrochen"))
    check("Warnung ohne Boost: keiner startet", "Warnung 15" in dhw_idle_reason({**wp, "warning_code": "15"}, {}, False, PD, T, today))
    check("nach Heizstab-Abbruch heute gesperrt", "heute kein Boost" in dhw_idle_reason(wp, {"date": today, "blocked": "1"}, False, PD, T, today))
    check("Sperre gilt nur für den Tag", dhw_idle_reason(wp, {"date": "2026-09-22", "blocked": "1"}, False, PD, T, today) is None)
    check("zwei Boosts heute: kein dritter", "heute schon 2" in dhw_idle_reason(wp, {"date": today, "starts": "2"}, False, PD, T, today))
    check("veraltete Wärmepumpen-Daten: kein Boost", "keine aktuellen" in dhw_idle_reason({**wp, "updated_at": str(T - 10 * MINUTE_MS)}, {}, False, PD, T, today))
    check("Störung: kein Boost", "Störung" in dhw_idle_reason({**wp, "fault_free": "0"}, {}, False, PD, T, today))

    # --- on-delay skipped when the battery budget reserve covers a false start twice over
    PS = Params(machines=[Machine("a", 200)], on_delay_min=15, off_delay_min=20, min_on_min=45, min_off_min=45)
    worst = 200 * 45 / 60  # 150 Wh
    d = decide(PS.machines, {"a": State(False, long_ago)}, inp(900, 900, budget=lambda load: (True, "B", 2 * worst)), PS, T)[0]
    check("Budget-Reserve ≥ 2× Fehlstart: sofort an", d.on and d.changed and "sofort" in d.reason)
    d = decide(PS.machines, {"a": State(False, long_ago)}, inp(900, 900, budget=lambda load: (True, "B", 2 * worst - 1)), PS, T)[0]
    check("Budget-Reserve knapp: Wartezeit bleibt", not d.on and "abwarten" in d.reason)
    d = decide(PS.machines, {"a": State(False, long_ago)}, inp(900, 900, budget=lambda load: (True, "Batterie voll", float("inf"))), PS, T)[0]
    check("Batterie voll: sofort an", d.on)
    d = decide(PS.machines, {"a": State(False, long_ago)}, inp(900, 900), PS, T)[0]
    check("ohne Prognose-Budget: Wartezeit bleibt", not d.on and "abwarten" in d.reason)

    # --- without complete data: hold, but not forever
    d = decide(P1.machines, {"a": State(True, long_ago)}, Inputs(False, None, None, 90.0, incomplete_min=10), P1, T)[0]
    check("Daten 10 min unvollständig: Zustand halten", d.on and not d.changed)
    d = decide(P1.machines, {"a": State(True, long_ago)}, Inputs(False, None, None, 90.0, incomplete_min=30), P1, T)[0]
    check("Daten 30 min unvollständig: sicherheitshalber aus", not d.on and d.changed and "sicherheitshalber" in d.reason)
    d = decide(P1.machines, {"a": State(False, long_ago)}, Inputs(False, None, None, 90.0, incomplete_min=5), P1, T)[0]
    check("ohne Daten wird nichts eingeschaltet", not d.on and not d.changed)

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
    check("Batterie unter Minimum stoppt trotz Mindestlaufzeit, wenn sie entlädt",
          not decide(P1.machines, fresh_on, inp(900, 900, soc=30, battery_w=-300), P1, T)[0].on)
    check("Batterie unter Minimum, lädt aber: kein Notstopp",
          decide(P1.machines, fresh_on, inp(900, 900, soc=30, battery_w=1000), P1, T)[0].on)
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
          not decide(PD.machines, {"a": State(True, long_ago, 0)}, inp(900, 900, soc=30, battery_w=-300), PD, T)[0].on)

    # --- priority with fill-in
    PB = Params(machines=[Machine("big", 700), Machine("small", 200)], on_delay_min=0, off_delay_min=0,
                min_on_min=30, min_off_min=30)
    d = {x.machine.name: x for x in decide(PB.machines, {"big": State(False, long_ago), "small": State(False, long_ago)},
                                            inp(450, 800), PB, T)}
    check("großes Gerät passt nicht, kleines darf nachrücken", not d["big"].on and d["small"].on)
    PBD = Params(machines=[Machine("big", 700), Machine("small", 200)], on_delay_min=15, off_delay_min=0,
                 min_on_min=30, min_off_min=30)
    d = {x.machine.name: x for x in decide(PBD.machines, {"big": State(False, long_ago), "small": State(False, long_ago)},
                                            inp(1000, 2000), PBD, T)}
    check("wartendes Gerät mit Vorrang behält seinen Anspruch", d["big"].pending_ms == T and not d["small"].pending_ms)

    # --- battery budget decides instead of a fixed soc_on
    ok_budget = lambda load: (True, "ok")
    no_budget = lambda load: (False, "zu wenig")
    check("Budget reicht: Start trotz 23 % Batterie",
          decide(P1.machines, {"a": State(False, long_ago)}, inp(900, 900, soc=23, budget=ok_budget), P1, T)[0].on)
    d = decide(P1.machines, {"a": State(False, long_ago)}, inp(900, 900, soc=95, budget=no_budget), P1, T)[0]
    check("Budget reicht nicht: kein Start, auch bei 95 %", not d.on and "Vorrang" in d.reason)
    check("Budget reicht nicht mehr: läuft aus",
          not decide(P1.machines, {"a": State(True, long_ago)}, inp(900, 900, soc=50, budget=no_budget), P1, T)[0].on)

    H = HOUR_S
    sunny = [(t * H, 500.0) for t in range(8, 19)]   # 500 W/m² from 7:00 to 18:00 (hour ending at t)
    # 10 W per W/m², derate 1: 5000 W PV, 100 W house -> 4900 W surplus, capped at 2500 W charging
    b = battery_budget(sunny, 8 * H, 12 * H, 24 * H, soc=50, capacity_wh=10000, max_charge_w=2500,
                       house_w=100, w_per_wm2=10, derate=1.0)
    check("Budget: 4 h × 2500 W reichen für 5 kWh bis Mittag", b(0)[0] and "12:00" in b(0)[1])
    check("Budget: Last unterhalb der Ladeleistung-Kappung kostet nichts", b(2400)[0])
    check("Budget: große Last macht Mittag unmöglich", not b(4000)[0])
    b2 = battery_budget(sunny, 8 * H, 12 * H, 24 * H, soc=0, capacity_wh=20000, max_charge_w=2500,
                        house_w=100, w_per_wm2=10, derate=1.0)
    check("Budget: Mittag nicht erreichbar -> Frist Abend", "Abend" in b2(0)[1] and b2(0)[0])
    check("Budget: volle Batterie braucht nichts",
          battery_budget(sunny, 8 * H, 12 * H, 24 * H, 100, 10000, 2500, 100, 10, 1.0)(9999)[0])
    check("Budget: ohne Prognose keine Aussage", battery_budget([], 8 * H, 12 * H, 24 * H, 50, 10000, 2500, 100, 10, 1.0) is None)

    # --- config
    check("Rechner-Konfiguration", [(m.name, m.watts) for m in parse_machines("winola:200, gamer:250")] == [("winola", 200), ("gamer", 250)])
    check("Geräteart Standard ist pc", parse_machines("winola:200")[0].kind == "pc")
    ms = parse_machines("dehumidifier:900:tuya", {"TUYA_DEHUMIDIFIER_DPS_SWITCH": "3"})
    check("Geräteart tuya mit DPS-Override", ms[0].kind == "tuya" and ms[0].tuya_dps_switch == "3")
    check("Tuya-DPS-Schalter ohne Override auf 1", parse_machines("dehumidifier:900:tuya")[0].tuya_dps_switch == "1")
    check("Shelly-Hosts parsen", parse_shelly_hosts("gamer:192.168.0.162, winola:192.168.0.208") == {"gamer": "192.168.0.162", "winola": "192.168.0.208"})
    ms = parse_machines("winola:200", {"PC_WINOLA_ACTUATOR": "shelly", "PC_WINOLA_SSH_HOST": "192.168.0.144", "PC_WINOLA_SSH_USER": "lands"})
    check("PC-Aktor-Konfiguration geparst", ms[0].pc_actuator_kind == "shelly" and ms[0].pc_ssh_host == "192.168.0.144" and ms[0].pc_ssh_user == "lands")
    check("ohne PC_*_ACTUATOR bleibt es Probelauf", parse_machines("winola:200", {})[0].pc_actuator_kind is None)

    # --- pc actuation: only switch off what the controller itself switched on
    sa, wa = pc_actuator.shelly_pc_actions, pc_actuator.wol_pc_actions
    check("Windows-PC einschalten: Steckdose an", sa(True, False, 0.0, False, owned=False) == ["shelly_on"])
    check("Windows-PC von Hand an, Regler will aus: in Ruhe lassen", sa(False, True, 80.0, False, owned=False) == [])
    check("Windows-PC vom Regler an, soll aus: SSH-Shutdown", sa(False, True, 80.0, False, owned=True) == ["ssh_shutdown"])
    check("Shutdown läuft, Leistung niedrig: Steckdose aus", sa(False, True, 3.0, True, owned=True) == ["shelly_off"])
    check("Mac schläft, soll an: WoL", wa(True, 1.0, owned=False) == ["wol"])
    check("Mac von Hand wach, Regler will aus: in Ruhe lassen", wa(False, 80.0, owned=False) == [])
    check("Mac vom Regler geweckt, soll aus: Ruhezustand", wa(False, 80.0, owned=True) == ["ssh_sleep"])
    check("Mac vom Regler geweckt, soll an: wach halten", wa(True, 80.0, owned=True) == ["keep_awake"])
    check("Mac von Hand wach, soll an: eigenes Ruheverhalten behalten", wa(True, 80.0, owned=False) == [])

    # --- tuya actuation: (re-)send the command on a change or when reality disagrees, not otherwise
    check("Zustand wechselt: senden", tuya_target_state(True, False))
    check("gewünschter Zustand liegt schon an: nicht senden", not tuya_target_state(True, True))
    check("kein bekannter Ist-Zustand: senden (zur Sicherheit)", tuya_target_state(False, None))
    check("manuell umgeschaltet (Drift): erneut senden", tuya_target_state(True, False))

    print("Alle Tests bestanden" if failures == 0 else f"{failures} Test(s) fehlgeschlagen")
    return failures == 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(0 if selftest() else 1)
    main()
