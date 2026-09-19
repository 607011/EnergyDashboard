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

# Open-Meteo's "current" block is itself only refreshed every 15 minutes
# server-side, so polling more often than that just wastes requests.
WEATHER_INTERVAL = float(os.environ.get("WEATHER_INTERVAL", "900"))
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

    power_dc = inverter_values.get("power_dc")
    if isinstance(power_dc, (int, float)):
        inverter_values["power_pv_total"] = power_dc + battery_power

    # meter "power" is positive = export to the grid, negative = import from it
    # (verified against the SolarEdge app). Whatever the inverter puts out that
    # isn't exported must have gone to the house -- and whatever is imported
    # went to the house too -- so this holds regardless of charge/discharge state.
    meter_power = 0.0
    for meter_id, meter in inverter.meters().items():
        values = apply_scale_factors(meter)
        log_device(r, f"meter:{meter_id.lower()}", values, ts_ms)
        if isinstance(values.get("power"), (int, float)):
            meter_power += values["power"]

    power_ac = inverter_values.get("power_ac")
    if isinstance(power_ac, (int, float)):
        inverter_values["house_consumption"] = power_ac - meter_power

    log_device(r, "inverter", inverter_values, ts_ms)

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
