"""Polls a Weishaupt heat pump (WGB/WBB/WAB/WSB/WWP family) via Modbus TCP and logs to Redis.

The register map is Weishaupt's "Datenpunktliste Modbus TCP (WWP)", document 83807301.
Things that differ from the SolarEdge poller:
  - all values are *input registers* (function code 0x04), at most 5 consecutive
    registers per request, so reads are grouped into short blocks
  - temperatures are signed 16-bit in 0.1 degC, with magic values (-32768 = no sensor,
    -32767 = open circuit, ...) that must not end up in the time series
  - the heat pump exposes no electrical power reading, only a 0..100 % power demand and
    energy counters in whole kWh

Writes:
  - a Redis hash        "weishaupt:<name>:latest"  -> most recent values
  - a RedisTimeSeries per numeric field, "ts:weishaupt:<name>:<field>"
"""

import logging
import os
import signal
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import redis
from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("weishaupt-poller")

HOST = os.environ.get("WEISHAUPT_HOST", "")
PORT = int(os.environ.get("WEISHAUPT_PORT", "502"))
UNIT = int(os.environ.get("WEISHAUPT_UNIT", "1"))
TIMEOUT = float(os.environ.get("MODBUS_TIMEOUT", "5"))
POLL_INTERVAL = float(os.environ.get("WEISHAUPT_POLL_INTERVAL", "30"))
# Redis key component, e.g. "wgb14" -> weishaupt:wgb14:latest
DEVICE_KEY = os.environ.get("WEISHAUPT_NAME", "wgb14")
# Heating circuits actually installed (1..4). Absent circuits answer with zeros or not at all.
HEATING_CIRCUITS = int(os.environ.get("WEISHAUPT_HEATING_CIRCUITS", "2"))

REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
REDIS_DB = int(os.environ.get("REDIS_DB", "0"))
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD") or None

TS_RETENTION_MS = int(os.environ.get("TS_RETENTION_DAYS", "365")) * 24 * 60 * 60 * 1000

TIMEZONE = ZoneInfo(os.environ.get("TIMEZONE", "UTC"))

# Manual electricity meter readings ("sneaker protocol", see README) and the derived counters.
READING_KEY = f"ts:weishaupt:{DEVICE_KEY}:electric_reading_kwh"
THERMAL_KEY = f"ts:weishaupt:{DEVICE_KEY}:thermal_total_kwh"
ACCUMULATOR_KEY = f"internal:weishaupt:{DEVICE_KEY}"
# Derived from sparse readings: keep forever instead of the usual retention window.
LONG_TERM_FIELDS = {
    "thermal_total_kwh",
    "jaz_since_first", "jaz_last_interval",
    "electric_kwh_since_first", "electric_kwh_last_interval", "electric_kwh_per_day_last",
    "thermal_kwh_since_first", "thermal_kwh_last_interval",
}
# Fields that only exist in the "latest" hash, not as time series of their own.
HASH_ONLY_FIELDS = {"electric_reading_kwh", "electric_reading_at"}

MAX_BACKOFF = 60
MAX_BLOCK = 5  # protocol limit of the heat pump

running = True


def handle_signal(signum, _frame):
    global running
    log.info("Received signal %s, shutting down", signum)
    running = False


signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)


# --- value decoders: raw register (signed 16-bit) -> number or None ---------

def temp(raw: int):
    """Sensor format: -500..5000 = -50.0..500.0 degC; anything below is a status code."""
    return raw / 10 if -500 <= raw <= 5000 else None


def setpoint(raw: int):
    """Setpoint format: -32768 / 1 = no request active."""
    return raw / 10 if 50 <= raw <= 5000 else None


def code(raw: int):
    """Error/warning codes: 65535 (-1 as signed) = none active."""
    return None if raw == -1 else raw & 0xFFFF


def plain(raw: int):
    return raw


def unsigned(raw: int):
    return raw & 0xFFFF


def percent(raw: int):
    return float(raw & 0xFFFF) if 0 <= (raw & 0xFFFF) <= 100 else None


def humidity(raw: int):
    v = raw & 0xFFFF
    return float(v) if 0 <= v <= 100 else None


# (field, register, decoder). Consecutive registers are merged into blocks of <= 5.
REGISTERS: list[tuple[str, int, callable]] = [
    # system
    ("outdoor_temp", 30001, temp),
    ("outdoor_temp_2", 30002, temp),
    ("error_code", 30003, code),
    ("warning_code", 30004, code),
    ("operating_status", 30006, unsigned),
    # domestic hot water
    ("dhw_setpoint", 32101, setpoint),
    ("dhw_temp", 32102, temp),
    # heat pump
    ("hp_status", 33101, unsigned),
    ("fault_free", 33102, unsigned),  # 1 = OK, 0 = fault active
    ("power_demand_pct", 33103, percent),
    ("flow_temp", 33104, temp),
    ("return_temp", 33105, temp),
    # second heat generator / electric heaters
    ("second_generator_on", 34101, plain),
    ("heater1_on", 34104, plain),
    ("heater2_on", 34105, plain),
    # SG-Ready inputs
    ("sg_ready_1", 35101, plain),
    ("sg_ready_2", 35102, plain),
]

# heating circuits: registers 31x01..31x05 (x = circuit number)
for _hc in range(1, HEATING_CIRCUITS + 1):
    _base = 31001 + _hc * 100
    REGISTERS += [
        (f"hc{_hc}_room_setpoint", _base, setpoint),
        (f"hc{_hc}_room_temp", _base + 1, temp),
        (f"hc{_hc}_humidity", _base + 2, humidity),
        (f"hc{_hc}_flow_setpoint", _base + 3, setpoint),
        (f"hc{_hc}_flow_temp", _base + 4, temp),
    ]

# energy statistics, whole kWh: <group> x (today, yesterday, month, year)
for _group, _base in (("total", 36101), ("heating", 36201), ("dhw", 36301), ("cooling", 36401)):
    for _i, _period in enumerate(("today", "yesterday", "month", "year")):
        REGISTERS.append((f"energy_{_group}_{_period}_kwh", _base + _i, unsigned))


def build_blocks(registers) -> list[tuple[int, int, list]]:
    """Group registers into (start, count, [(field, offset, decoder)]) blocks of consecutive addresses."""
    blocks: list[tuple[int, int, list]] = []
    for field, addr, decoder in sorted(registers, key=lambda x: x[1]):
        if blocks and addr == blocks[-1][0] + blocks[-1][1] and blocks[-1][1] < MAX_BLOCK:
            start, count, items = blocks[-1]
            items.append((field, addr - start, decoder))
            blocks[-1] = (start, count + 1, items)
        else:
            blocks.append((addr, 1, [(field, 0, decoder)]))
    return blocks


BLOCKS = build_blocks(REGISTERS)
# Blocks the device refused (e.g. circuits that aren't installed): don't ask again every cycle.
unsupported: set[int] = set()


def read_values(client: ModbusTcpClient) -> dict:
    values: dict = {}
    for start, count, items in BLOCKS:
        if start in unsupported:
            continue
        rr = client.read_input_registers(start, count, slave=UNIT)
        if rr.isError():
            # A Modbus exception response means the device is alive but has no such
            # registers; a real transport failure raises ModbusException instead.
            log.warning("Registers %d..%d not readable (%s), skipping them from now on", start, start + count - 1, rr)
            unsupported.add(start)
            continue
        signed = [r - 0x10000 if r > 0x7FFF else r for r in rr.registers]
        for field, offset, decoder in items:
            value = decoder(signed[offset])
            if value is not None:
                values[field] = value
    return values


def update_thermal_total(r: redis.Redis, values: dict) -> None:
    """Keep a monotonic thermal energy counter derived from the calendar-year counter.

    The heat pump's own counters reset at midnight / month / year boundaries, which is
    useless for computing a performance factor over several days or across New Year.
    A decrease is only accepted as a rollover in January; anywhere else it's treated as
    a bad reading and skipped, as is an implausible jump.
    """
    year = values.get("energy_total_year_kwh")
    if year is None:
        return
    acc = r.hgetall(ACCUMULATOR_KEY)
    if acc:
        last, total = float(acc["last_year_kwh"]), float(acc["thermal_total_kwh"])
        if year >= last:
            delta = year - last
        elif datetime.now(TIMEZONE).month == 1:
            delta = year
        else:
            log.warning("Year energy counter fell from %s to %s outside January, ignoring", last, year)
            values["thermal_total_kwh"] = total
            return
        if delta > 500:
            log.warning("Year energy counter jumped by %s kWh, ignoring", delta)
            values["thermal_total_kwh"] = total
            return
        total += delta
    else:
        total = 0.0
    r.hset(ACCUMULATOR_KEY, mapping={"last_year_kwh": year, "thermal_total_kwh": total})
    values["thermal_total_kwh"] = total


def value_at(r: redis.Redis, key: str, ts_ms: int):
    res = r.ts().revrange(key, 0, ts_ms, count=1)
    return res[0][1] if res else None


def derive_efficiency(r: redis.Redis) -> tuple[dict, dict]:
    """From the manual meter readings, derive electricity use and the seasonal performance factor.

    Returns (values, timestamps): the timestamp of a derived value is that of the newest
    reading, so the series shows one point per reading. Only readings taken after the
    thermal counter started can be used, since the thermal side needs a matching value.
    """
    try:
        readings = r.ts().range(READING_KEY, "-", "+")
    except redis.ResponseError:
        return {}, {}  # no reading entered yet
    if not readings:
        return {}, {}

    values = {"electric_reading_kwh": readings[-1][1], "electric_reading_at": readings[-1][0]}
    usable = []
    for t, electric in readings:
        thermal = value_at(r, THERMAL_KEY, t)
        if thermal is not None:
            usable.append((t, electric, thermal))
    if len(usable) < 2:
        return values, {}

    def span(a, b, suffix):
        electric, thermal = b[1] - a[1], b[2] - a[2]
        values[f"electric_kwh_{suffix}"] = electric
        values[f"thermal_kwh_{suffix}"] = thermal
        if electric > 0:
            values[f"jaz_{suffix}"] = thermal / electric

    span(usable[0], usable[-1], "since_first")
    span(usable[-2], usable[-1], "last_interval")
    days = (usable[-1][0] - usable[-2][0]) / 86_400_000
    if days > 0:
        values["electric_kwh_per_day_last"] = values["electric_kwh_last_interval"] / days

    at = {field: usable[-1][0] for field in values if field not in HASH_ONLY_FIELDS}
    return values, at


def log_device(r: redis.Redis, values: dict, ts_ms: int, ts_at: dict | None = None) -> None:
    latest_key = f"weishaupt:{DEVICE_KEY}:latest"
    ts_at = ts_at or {}

    pipe = r.pipeline(transaction=False)
    pipe.delete(latest_key)  # drop fields that vanished (e.g. a sensor that dropped out)
    mapping = {k: str(v) for k, v in values.items()}
    mapping["updated_at"] = str(ts_ms)
    pipe.hset(latest_key, mapping=mapping)

    for field, value in values.items():
        if field in HASH_ONLY_FIELDS:
            continue
        pipe.ts().add(
            f"ts:weishaupt:{DEVICE_KEY}:{field}",
            ts_at.get(field, ts_ms),
            float(value),
            retention_msecs=0 if field in LONG_TERM_FIELDS else TS_RETENTION_MS,
            labels={"device": DEVICE_KEY, "field": field},
            duplicate_policy="last",
        )
    pipe.execute()


def main() -> None:
    if not HOST:
        log.info("WEISHAUPT_HOST is not set -- no heat pump configured, idling")
        while running:
            time.sleep(1)
        return

    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, password=REDIS_PASSWORD, decode_responses=True)
    r.ping()
    log.info("Connected to Redis at %s:%s", REDIS_HOST, REDIS_PORT)

    client = ModbusTcpClient(HOST, port=PORT, timeout=TIMEOUT)
    backoff = 1

    while running:
        loop_start = time.monotonic()
        try:
            if not client.connect():
                raise ConnectionError(f"cannot connect to {HOST}:{PORT}")
            values = read_values(client)
            update_thermal_total(r, values)
            derived, derived_at = derive_efficiency(r)
            values.update(derived)
            log_device(r, values, int(time.time() * 1000), derived_at)
            log.debug("Logged %d values", len(values))
            backoff = 1
        except (ModbusException, ConnectionError, OSError) as exc:
            log.warning("Poll failed (%s), retrying in %ss", exc, backoff)
            client.close()
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)
            continue
        except Exception:
            log.exception("Unexpected error, retrying in %ss", backoff)
            client.close()
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)
            continue

        time.sleep(max(0.0, POLL_INTERVAL - (time.monotonic() - loop_start)))

    client.close()
    log.info("Stopped")


if __name__ == "__main__":
    main()
