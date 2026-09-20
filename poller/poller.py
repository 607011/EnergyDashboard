"""Polls a SolarEdge inverter via Modbus TCP (SunSpec) and logs the values to Redis.

For each device (inverter, and any auto-detected meters/batteries) this writes:
  - a Redis hash        "solaredge:<device>:latest"      -> current values, for dashboards
  - a RedisTimeSeries per numeric field "ts:<device>:<field>" -> history, for Grafana graphs
"""

import logging
import os
import signal
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import redis
import requests
import solaredge_modbus

from sunpos import solar_position

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("solaredge-poller")

INVERTER_HOST = os.environ["INVERTER_HOST"]
INVERTER_PORT = int(os.environ.get("INVERTER_PORT", "502"))
MODBUS_UNIT = int(os.environ.get("MODBUS_UNIT", "1"))
MODBUS_TIMEOUT = float(os.environ.get("MODBUS_TIMEOUT", "5"))
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "10"))

REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
REDIS_DB = int(os.environ.get("REDIS_DB", "0"))
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD") or None

TS_RETENTION_MS = int(os.environ.get("TS_RETENTION_DAYS", "365")) * 24 * 60 * 60 * 1000

ONE_DAY_MS = 24 * 60 * 60 * 1000

# Long-term compaction: keep an indefinitely-retained daily rollup of a few key
# metrics, so multi-year statistics (e.g. monthly production) stay cheap even
# though the raw high-resolution series only cover TS_RETENTION_DAYS.
# energy_total is a monotonically increasing lifetime counter, so its daily
# "last" value alone is enough to reconstruct exact monthly/yearly production
# via a simple difference -- no lossy averaging needed for that one.
COMPACTION_RULES = [
    ("ts:inverter:energy_total", "ts:inverter:energy_total:daily", "last"),
    ("ts:inverter:power_pv_total", "ts:inverter:power_pv_total:daily_avg", "avg"),
    ("ts:inverter:power_pv_total", "ts:inverter:power_pv_total:daily_max", "max"),
]

LAT = float(os.environ["LAT"]) if os.environ.get("LAT") else None
LON = float(os.environ["LON"]) if os.environ.get("LON") else None

# Local timezone for "today" boundaries in the daily energy totals below.
TIMEZONE = ZoneInfo(os.environ.get("TIMEZONE", "UTC"))

# Open-Meteo's "current" block is itself only refreshed every 15 minutes
# server-side, so polling more often than that just wastes requests.
WEATHER_INTERVAL = float(os.environ.get("WEATHER_INTERVAL", "900"))
# Hoymiles values older than this (seconds) are ignored when adding them to the house consumption.
HOYMILES_MAX_AGE = float(os.environ.get("HOYMILES_MAX_AGE", "900"))
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"

MAX_BACKOFF = 60

running = True


def handle_signal(signum, _frame):
    global running
    log.info("Received signal %s, shutting down", signum)
    running = False


signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)


# SunSpec dtypes whose registers are plain integers scaled by a *separate*
# trailing "..._scale" register. FLOAT32/SEFLOAT values are already final and
# STRING values aren't numeric at all, so neither belongs in a scale group.
_SCALABLE_DTYPES = {"INT16", "UINT16", "INT32", "UINT32", "ACC32"}

_scale_group_cache: dict[str, dict] = {}


def scale_group_map(cache_key: str, registers: dict) -> dict:
    """Map each value key to the "..._scale" key that applies to it.

    SunSpec applies one scale-factor register to a whole run of preceding
    value registers (e.g. "current_scale" covers "current", "l1_current",
    "l2_current" and "l3_current" together) rather than one scale per field.
    """
    cached = _scale_group_cache.get(cache_key)
    if cached is not None:
        return cached

    mapping = {}
    pending = []
    for key, meta in registers.items():
        dtype_name = meta[3].name
        if key.endswith("_scale"):
            for pending_key in pending:
                mapping[pending_key] = key
            pending = []
        elif dtype_name in _SCALABLE_DTYPES:
            pending.append(key)
        # STRING/FLOAT32/SEFLOAT/etc. fields are skipped: not part of a scale group.

    _scale_group_cache[cache_key] = mapping
    return mapping


def apply_scale_factors(device) -> dict:
    """Read all registers from a device and fold value/scale register pairs into one number."""
    raw = device.read_all()
    scale_map = scale_group_map(type(device).__name__, device.registers)

    result = {}
    for key, value in raw.items():
        if key.endswith("_scale"):
            continue
        scale_key = scale_map.get(key)
        if scale_key is not None and scale_key in raw and isinstance(value, (int, float)):
            result[key] = value * (10 ** raw[scale_key])
        else:
            result[key] = value
    return result


def add_status_label(values: dict, status_map) -> dict:
    status = values.get("status")
    if isinstance(status, int) and 0 <= status < len(status_map):
        values["status_label"] = status_map[status]
    return values


def flatten_for_redis(data: dict) -> dict:
    flat = {}
    for key, value in data.items():
        if isinstance(value, float):
            value = round(value, 4)
        flat[key] = "" if value is None else str(value)
    return flat


def connect_inverter() -> solaredge_modbus.Inverter:
    log.info("Connecting to inverter at %s:%s (unit %s)", INVERTER_HOST, INVERTER_PORT, MODBUS_UNIT)
    return solaredge_modbus.Inverter(
        host=INVERTER_HOST,
        port=INVERTER_PORT,
        timeout=MODBUS_TIMEOUT,
        unit=MODBUS_UNIT,
    )


def log_device(r: redis.Redis, device_key: str, values: dict, ts_ms: int) -> None:
    latest_key = f"solaredge:{device_key}:latest"

    mapping = flatten_for_redis(values)
    mapping["updated_at"] = str(ts_ms)

    pipe = r.pipeline(transaction=False)
    pipe.hset(latest_key, mapping=mapping)

    for field, value in values.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        pipe.ts().add(
            f"ts:{device_key}:{field}",
            ts_ms,
            float(value),
            retention_msecs=TS_RETENTION_MS,
            labels={"device": device_key, "field": field},
            duplicate_policy="last",
        )

    pipe.execute()


def sync_retention(r: redis.Redis) -> None:
    """TS.ADD's RETENTION option only applies when it creates a new series; changing
    TS_RETENTION_DAYS later has no effect on already-existing ones without this."""
    rollup_keys = {dest for _, dest, _ in COMPACTION_RULES}
    changed = 0
    for key in r.scan_iter(match="ts:*"):
        if key in rollup_keys:
            continue
        if r.ts().info(key).retention_msecs != TS_RETENTION_MS:
            r.ts().alter(key, retention_msecs=TS_RETENTION_MS)
            changed += 1
    if changed:
        log.info("Updated retention on %d existing series to %d days", changed, TS_RETENTION_MS // ONE_DAY_MS)


def ensure_compaction_rules(r: redis.Redis) -> None:
    for source_key, dest_key, aggregation in COMPACTION_RULES:
        if not r.exists(source_key):
            r.ts().create(source_key, retention_msecs=TS_RETENTION_MS, duplicate_policy="last")
        if not r.exists(dest_key):
            r.ts().create(dest_key, retention_msecs=0, labels={"aggregation": aggregation, "rollup": "daily"})
        try:
            r.ts().createrule(source_key, dest_key, aggregation_type=aggregation, bucket_size_msec=ONE_DAY_MS)
            log.info("Compaction rule created: %s -> %s (%s, daily)", source_key, dest_key, aggregation)
        except redis.ResponseError as exc:
            if "already has" not in str(exc):
                raise


_DAILY_STATE_KEY = "internal:daily_accumulator"


def update_daily_totals(
    r: redis.Redis,
    ts_ms: int,
    pv_power: float | None,
    house_power: float | None,
    export_wh: float | None,
    import_wh: float | None,
) -> None:
    """Running kWh-so-far for today (local time), persisted in Redis so it
    survives poller restarts. Power metrics (pv/house) have no hardware
    energy counter, so they're trapezoidally integrated poll by poll; grid
    export/import already have one (export_energy_active/import_energy_active),
    so today's total there is just today's counter minus its value at midnight.
    """
    today = datetime.fromtimestamp(ts_ms / 1000, TIMEZONE).strftime("%Y-%m-%d")
    state = r.hgetall(_DAILY_STATE_KEY)

    if state.get("date") != today:
        state = {
            "date": today,
            "pv_wh": "0",
            "house_wh": "0",
            "last_ts": str(ts_ms),
            "last_pv_power": str(pv_power or 0.0),
            "last_house_power": str(house_power or 0.0),
            "export_ref_wh": str(export_wh) if export_wh is not None else "",
            "import_ref_wh": str(import_wh) if import_wh is not None else "",
        }

    pv_wh = float(state["pv_wh"])
    house_wh = float(state["house_wh"])
    dt_hours = (ts_ms - int(state["last_ts"])) / 3_600_000.0
    if dt_hours > 0:
        if pv_power is not None:
            pv_wh += (float(state["last_pv_power"]) + pv_power) / 2.0 * dt_hours
        if house_power is not None:
            house_wh += (float(state["last_house_power"]) + house_power) / 2.0 * dt_hours

    state["pv_wh"] = str(pv_wh)
    state["house_wh"] = str(house_wh)
    state["last_ts"] = str(ts_ms)
    state["last_pv_power"] = str(pv_power if pv_power is not None else state["last_pv_power"])
    state["last_house_power"] = str(house_power if house_power is not None else state["last_house_power"])
    if state.get("export_ref_wh", "") == "" and export_wh is not None:
        state["export_ref_wh"] = str(export_wh)
    if state.get("import_ref_wh", "") == "" and import_wh is not None:
        state["import_ref_wh"] = str(import_wh)

    r.hset(_DAILY_STATE_KEY, mapping=state)

    export_today_wh = (
        export_wh - float(state["export_ref_wh"])
        if export_wh is not None and state.get("export_ref_wh")
        else 0.0
    )
    import_today_wh = (
        import_wh - float(state["import_ref_wh"])
        if import_wh is not None and state.get("import_ref_wh")
        else 0.0
    )

    log_device(
        r,
        "daily",
        {
            "pv_wh": round(pv_wh, 1),
            "house_wh": round(house_wh, 1),
            "export_wh": round(export_today_wh, 1),
            "import_wh": round(import_today_wh, 1),
        },
        ts_ms,
    )


def fetch_weather(lat: float, lon: float) -> dict:
    """Current outside temperature and solar irradiance from Open-Meteo (free, no API key)."""
    response = requests.get(
        WEATHER_URL,
        params={
            "latitude": lat,
            "longitude": lon,
            "current": "temperature_2m,shortwave_radiation,cloud_cover",
            "timezone": "UTC",
        },
        timeout=10,
    )
    response.raise_for_status()
    current = response.json()["current"]
    return {
        "temperature_c": current["temperature_2m"],
        "shortwave_radiation": current["shortwave_radiation"],
        "cloud_cover": current["cloud_cover"],
    }


def hoymiles_power(r: redis.Redis, now_ms: int) -> float | None:
    """Combined AC power (W) of all Hoymiles microinverters, as last stored by hoymiles-poller.

    Per-inverter hashes are recognised by their per-string field p1_w; the station hash holds
    the same power again as a total and must not be counted twice. Returns None if no
    inverter has fresh data (cloud outage, hoymiles-poller not running).
    """
    total = None
    for key in r.scan_iter(match="hoymiles:*:latest"):
        h = r.hgetall(key)
        if "p1_w" not in h:
            continue
        try:
            fresh = now_ms - int(h["updated_at"]) <= HOYMILES_MAX_AGE * 1000
            power = float(h.get("power_w") or 0)
        except (KeyError, ValueError):
            continue
        if fresh:
            total = (total or 0.0) + power
    return total


def poll_once(inverter: solaredge_modbus.Inverter, r: redis.Redis) -> None:
    ts_ms = int(time.time() * 1000)

    inverter_values = apply_scale_factors(inverter)
    if not inverter_values or "c_manufacturer" not in inverter_values:
        raise ConnectionError("Empty/incomplete response from inverter")
    add_status_label(inverter_values, solaredge_modbus.INVERTER_STATUS_MAP)

    # On a DC-coupled hybrid inverter (like the SE10K-RWB48), the battery taps
    # the DC bus *before* the inverter's own DC/AC conversion stage. So
    # "power_dc" alone isn't the panels' output: while charging it's missing
    # the power diverted straight to the battery, and while discharging it's
    # inflated by the battery's contribution flowing onto the same bus.
    # instantaneous_power is signed (+ while charging, - while discharging),
    # so adding it back unconditionally corrects both directions at once.
    battery_power = 0.0
    for battery_id, battery in inverter.batteries().items():
        values = apply_scale_factors(battery)
        add_status_label(values, solaredge_modbus.BATTERY_STATUS_MAP)
        log_device(r, f"battery:{battery_id.lower()}", values, ts_ms)
        if isinstance(values.get("instantaneous_power"), (int, float)):
            battery_power += values["instantaneous_power"]

    # The two terms are measured separately (DC input vs. battery terminals, in different
    # Modbus reads), so while the battery discharges the sum can dip below zero from
    # converter losses and timing skew. The panels can't deliver negative power: the
    # published value is clamped at 0, the unclamped sum is kept as power_pv_total_raw.
    power_dc = inverter_values.get("power_dc")
    if isinstance(power_dc, (int, float)):
        pv_total_raw = power_dc + battery_power
        inverter_values["power_pv_total_raw"] = pv_total_raw
        inverter_values["power_pv_total"] = max(0.0, pv_total_raw)

    # meter "power" is positive = export to the grid, negative = import from it
    # (verified against the SolarEdge app). Whatever the inverter puts out that
    # isn't exported must have gone to the house -- and whatever is imported
    # went to the house too -- so this holds regardless of charge/discharge state.
    meter_power = 0.0
    export_wh = None
    import_wh = None
    for meter_id, meter in inverter.meters().items():
        values = apply_scale_factors(meter)
        log_device(r, f"meter:{meter_id.lower()}", values, ts_ms)
        if isinstance(values.get("power"), (int, float)):
            meter_power += values["power"]
        if isinstance(values.get("export_energy_active"), (int, float)):
            export_wh = (export_wh or 0.0) + values["export_energy_active"]
        if isinstance(values.get("import_energy_active"), (int, float)):
            import_wh = (import_wh or 0.0) + values["import_energy_active"]

    power_ac = inverter_values.get("power_ac")
    house_consumption = None
    if isinstance(power_ac, (int, float)):
        house_consumption = power_ac - meter_power
        inverter_values["house_consumption"] = house_consumption
        # The Hoymiles microinverters feed the house behind the same grid meter, so the
        # SolarEdge balance (AC power minus meter) leaves out what they supply.
        hoymiles = hoymiles_power(r, ts_ms)
        if hoymiles is not None:
            inverter_values["house_consumption_total"] = house_consumption + hoymiles

    log_device(r, "inverter", inverter_values, ts_ms)

    update_daily_totals(r, ts_ms, inverter_values.get("power_pv_total"), house_consumption, export_wh, import_wh)

    if LAT is not None and LON is not None:
        azimuth, elevation = solar_position(datetime.now(timezone.utc), LAT, LON)
        log_device(r, "sun", {"azimuth": round(azimuth, 2), "elevation": round(elevation, 2)}, ts_ms)


def main() -> None:
    r = redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        db=REDIS_DB,
        password=REDIS_PASSWORD,
        decode_responses=True,
    )
    r.ping()
    log.info("Connected to Redis at %s:%s", REDIS_HOST, REDIS_PORT)
    sync_retention(r)
    ensure_compaction_rules(r)

    inverter = connect_inverter()
    backoff = 1
    next_weather_poll = 0.0

    while running:
        loop_start = time.monotonic()

        if LAT is not None and LON is not None and time.monotonic() >= next_weather_poll:
            try:
                weather = fetch_weather(LAT, LON)
                log_device(r, "weather", weather, int(time.time() * 1000))
            except Exception:
                log.exception("Weather fetch failed, will retry next cycle")
            next_weather_poll = time.monotonic() + WEATHER_INTERVAL

        try:
            poll_once(inverter, r)
            if backoff != 1:
                log.info("Poll recovered")
            backoff = 1
        except Exception:
            log.exception("Poll failed, reconnecting in %ss", backoff)
            try:
                inverter.disconnect()
            except Exception:
                pass
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)
            inverter = connect_inverter()
            continue

        elapsed = time.monotonic() - loop_start
        time.sleep(max(0.0, POLL_INTERVAL - elapsed))

    log.info("Stopped")


if __name__ == "__main__":
    main()
