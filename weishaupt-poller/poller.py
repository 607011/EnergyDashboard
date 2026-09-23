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

Hot-water boost on PV surplus: compute-controller never talks Modbus itself (the heat pump gets
exactly one client, this one). It puts its wish into "weishaupt:<name>:boost" (active, target_c,
push_min, deadline_ms), refreshed every minute; this poller carries it out -- raise the DHW
"Normal" temperature to the target and start a push (a push also overcomes the switching
hysteresis, tested 2026-09-23) -- and restores the saved Normal temperature once the wish is
withdrawn *or stops being refreshed* (deadline passed: controller gone). The saved value lives in
"weishaupt:<name>:boost_state", so a restart of this poller mid-boost still restores it.
"""

import logging
import os
import signal
import time

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

# Manual readings ("sneaker protocol", see README): the electricity meter, and the heat pump's
# own thermal energy counter for the calendar year, both read off at the same time.
READING_KEY = f"ts:weishaupt:{DEVICE_KEY}:electric_reading_kwh"
THERMAL_READING_KEY = f"ts:weishaupt:{DEVICE_KEY}:thermal_reading_kwh"
# Derived from sparse readings: keep forever instead of the usual retention window.
LONG_TERM_FIELDS = {
    "jaz_since_first", "jaz_last_interval",
    "electric_kwh_since_first", "electric_kwh_last_interval", "electric_kwh_per_day_last",
    "thermal_kwh_since_first", "thermal_kwh_last_interval",
}
# Fields that only exist in the "latest" hash, not as time series of their own.
HASH_ONLY_FIELDS = {"electric_reading_kwh", "electric_reading_at", "thermal_reading_kwh"}

ONE_DAY_MS = 24 * 60 * 60 * 1000

# Long-term compaction: daily min/mean/max of a few temperatures, kept forever, so multi-year
# statistics stay cheap although the raw series only cover TS_RETENTION_DAYS. Days are UTC
# buckets, as with the SolarEdge rollups.
ROLLUP_FIELDS = ("outdoor_temp", "dhw_temp")
COMPACTION_RULES = [
    (f"ts:weishaupt:{DEVICE_KEY}:{field}", f"ts:weishaupt:{DEVICE_KEY}:{field}:daily_{name}", aggregation)
    for field in ROLLUP_FIELDS
    for name, aggregation in (("avg", "avg"), ("min", "min"), ("max", "max"))
]

# After this many seconds without a single successful Modbus read, the live readings are removed
# instead of left showing an increasingly wrong, frozen number with no sign that it's stale. The
# values derived from manual meter readings (electric_reading_kwh, jaz_*, ...) don't depend on
# Modbus and are left alone.
STALE_AFTER_S = float(os.environ.get("STALE_AFTER_SECONDS", "120"))

MAX_BACKOFF = 60
MAX_BLOCK = 5  # protocol limit of the heat pump

# Holding registers (read 0x03 / write 0x06), one at a time: this unit doesn't answer a block read.
HR_DHW_PUSH = 42102    # 0 = off, else minutes (5..235)
HR_DHW_NORMAL = 42103  # DHW "Normal" temperature, 0.1 degC (a stored setting)
BOOST_KEY = f"weishaupt:{DEVICE_KEY}:boost"              # the controller's wish
BOOST_STATE_KEY = f"weishaupt:{DEVICE_KEY}:boost_state"  # ours: saved_normal, date, writes
# Settings end up in the controller's non-volatile memory: cap the writes for a boost. Restoring
# the saved value is never held back by this.
MAX_WRITES_PER_DAY = int(os.environ.get("WEISHAUPT_MAX_WRITES_PER_DAY", "12"))

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

# energy statistics, whole kWh: <group> x (today, yesterday, month, year). On our unit these did
# not match the display's thermal energy, so they are logged for reference but not used anywhere.
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


def read_holding(client: ModbusTcpClient, address: int):
    rr = client.read_holding_registers(address, 1, slave=UNIT)
    return None if rr.isError() else rr.registers[0]


def write_checked(client: ModbusTcpClient, address: int, value: int) -> bool:
    rr = client.write_register(address, value, slave=UNIT)
    if rr.isError():
        log.warning("Writing %d to register %d failed: %s", value, address, rr)
        return False
    return read_holding(client, address) == value


def boost_plan(wish: dict, state: dict, normal: int | None, push: int | None, now_ms: int, today: str,
               max_writes: int = MAX_WRITES_PER_DAY) -> tuple[str | None, list, dict]:
    """What to do for the hot-water boost: (action, [(register, value), ...], new boost_state).
    action is "start", "extend" (push ran out while still wanted), "restore" or None.

    Pure, so it can be tested without a heat pump. `normal`/`push` are the registers' current raw
    values (None = unreadable, then nothing is done). The Normal temperature is saved *before* it
    is raised, and a value at or above the target is never saved (that would be a leftover of an
    earlier boost -- the value saved last time is restored then).
    """
    state = dict(state)
    if state.get("date") != today:
        state["date"], state["writes"] = today, "0"
    writes = int(state.get("writes", 0))
    try:
        want = wish.get("active") == "1" and now_ms < int(wish.get("deadline_ms", 0))
        target = round(float(wish["target_c"]) * 10) if want else None
        push_min = int(wish.get("push_min", 60))
    except (KeyError, ValueError):
        want = False
    if normal is None or push is None:
        return None, [], state
    boosting = "saved_normal" in state

    if want and not boosting:
        restore_to = normal if normal < target else int(state.get("last_saved_normal", 0))
        if not restore_to:
            log.warning("DHW boost: Normal is already %.1f degC and no earlier value is known to "
                        "restore later -- not boosting", normal / 10)
            return None, [], state
        if writes + 2 > max_writes:
            log.warning("DHW boost: %d setting writes today already, not boosting", writes)
            return None, [], state
        state["saved_normal"] = state["last_saved_normal"] = str(restore_to)
        state["writes"] = str(writes + 2)
        return "start", [(HR_DHW_NORMAL, target), (HR_DHW_PUSH, push_min)], state
    if want and boosting and push == 0 and writes < max_writes:
        state["writes"] = str(writes + 1)
        return "extend", [(HR_DHW_PUSH, push_min)], state
    if not want and boosting:
        saved = int(state["saved_normal"])
        plan = ([(HR_DHW_NORMAL, saved)] if normal != saved else []) + ([(HR_DHW_PUSH, 0)] if push else [])
        return "restore", plan, state
    return None, [], state


def apply_boost(client: ModbusTcpClient, r: redis.Redis, values: dict) -> None:
    """Reads the DHW push/Normal registers into `values` and carries out the controller's boost wish."""
    push, normal = read_holding(client, HR_DHW_PUSH), read_holding(client, HR_DHW_NORMAL)
    if push is not None:
        values["dhw_push_min"] = push
    if normal is not None:
        values["dhw_normal_setpoint"] = normal / 10
    action, plan, state = boost_plan(r.hgetall(BOOST_KEY), r.hgetall(BOOST_STATE_KEY), normal, push,
                                     int(time.time() * 1000), time.strftime("%Y-%m-%d"))
    r.hset(BOOST_STATE_KEY, mapping=state)  # saved_normal is stored before anything is written
    ok = all(write_checked(client, register, value) for register, value in plan)
    if action:
        log.info("DHW boost %s: %s%s", action, ", ".join(f"{reg}={val}" for reg, val in plan) or "nothing to write",
                 "" if ok else " -- FAILED, retrying next poll")
    if action == "restore" and ok:
        r.hdel(BOOST_STATE_KEY, "saved_normal")
    values["dhw_boost_active"] = int(r.hexists(BOOST_STATE_KEY, "saved_normal"))


def derive_efficiency(r: redis.Redis) -> tuple[dict, dict]:
    """From the manual readings, derive electricity use, thermal energy and the seasonal performance factor.

    Each reading has the electricity meter (kWh) and optionally the heat pump's thermal energy
    counter for the calendar year (kWh). The year counter restarts on 1 January, so a lower value
    than the previous one is taken as a new year and counts as that value; heat produced between
    the last reading of the old year and midnight is lost, so take a reading around New Year.

    Returns (values, timestamps): the timestamp of a derived value is that of the newest reading,
    so the series shows one point per reading.
    """
    try:
        electric = r.ts().range(READING_KEY, "-", "+")
    except redis.ResponseError:
        return {}, {}  # no reading entered yet
    if not electric:
        return {}, {}

    values = {"electric_reading_kwh": electric[-1][1], "electric_reading_at": electric[-1][0]}
    try:
        thermal = dict(r.ts().range(THERMAL_READING_KEY, "-", "+"))
    except redis.ResponseError:
        thermal = {}
    if thermal:
        values["thermal_reading_kwh"] = list(thermal.values())[-1]

    # (time, electric kWh, cumulative thermal kWh) for readings that have both
    points = []
    cumulative, previous = 0.0, None
    for t, e in electric:
        th = thermal.get(t)
        if th is None:
            continue
        if previous is not None:
            cumulative += th - previous if th >= previous else th
        previous = th
        points.append((t, e, cumulative))
    if len(points) < 2:
        return values, {}

    def span(a, b, suffix):
        electric_kwh, thermal_kwh = b[1] - a[1], b[2] - a[2]
        values[f"electric_kwh_{suffix}"] = electric_kwh
        values[f"thermal_kwh_{suffix}"] = thermal_kwh
        if electric_kwh > 0:
            values[f"jaz_{suffix}"] = thermal_kwh / electric_kwh

    span(points[0], points[-1], "since_first")
    span(points[-2], points[-1], "last_interval")
    days = (points[-1][0] - points[-2][0]) / 86_400_000
    if days > 0:
        values["electric_kwh_per_day_last"] = values["electric_kwh_last_interval"] / days

    at = {field: points[-1][0] for field in values if field not in HASH_ONLY_FIELDS}
    return values, at


def sync_retention(r: redis.Redis) -> None:
    """TS.ADD's RETENTION option only applies when it creates a series, so a changed
    TS_RETENTION_DAYS needs this for the raw series that already exist. Series kept forever
    (manual readings, values derived from them, the daily rollups) are left alone."""
    keep_forever = {dest for _, dest, _ in COMPACTION_RULES}
    keep_forever |= {f"ts:weishaupt:{DEVICE_KEY}:{field}" for field in LONG_TERM_FIELDS | HASH_ONLY_FIELDS}
    changed = 0
    for key in r.scan_iter(match=f"ts:weishaupt:{DEVICE_KEY}:*"):
        if key in keep_forever:
            continue
        if r.ts().info(key).retention_msecs != TS_RETENTION_MS:
            r.ts().alter(key, retention_msecs=TS_RETENTION_MS)
            changed += 1
    if changed:
        log.info("Updated retention on %d existing series to %d days", changed, TS_RETENTION_MS // ONE_DAY_MS)


def ensure_compaction_rules(r: redis.Redis) -> None:
    for source_key, dest_key, aggregation in COMPACTION_RULES:
        if not r.exists(source_key):
            field = source_key.rsplit(":", 1)[-1]
            r.ts().create(source_key, retention_msecs=TS_RETENTION_MS, duplicate_policy="last",
                          labels={"device": DEVICE_KEY, "field": field})
        if not r.exists(dest_key):
            r.ts().create(dest_key, retention_msecs=0, labels={"device": DEVICE_KEY, "aggregation": aggregation, "rollup": "daily"})
        try:
            r.ts().createrule(source_key, dest_key, aggregation_type=aggregation, bucket_size_msec=ONE_DAY_MS)
            log.info("Compaction rule created: %s -> %s (%s, daily)", source_key, dest_key, aggregation)
        except redis.ResponseError as exc:
            if "already has" not in str(exc):
                raise


def clear_stale(r: redis.Redis) -> None:
    """Remove the Modbus-read fields after a prolonged outage (see STALE_AFTER_S), keeping the
    fields derived from manual meter readings (they don't depend on the Modbus link at all)."""
    field_names = [field for field, _, _ in REGISTERS]
    r.hdel(f"weishaupt:{DEVICE_KEY}:latest", *field_names)
    log.warning("No successful Modbus read for over %.0fs -- cleared the live readings "
                "(manual-reading based values are unaffected)", STALE_AFTER_S)


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
    sync_retention(r)
    ensure_compaction_rules(r)

    client = ModbusTcpClient(HOST, port=PORT, timeout=TIMEOUT)
    backoff = 1
    last_success = time.monotonic()
    cleared_stale = False

    def handle_failure():
        nonlocal cleared_stale
        if not cleared_stale and time.monotonic() - last_success >= STALE_AFTER_S:
            clear_stale(r)
            cleared_stale = True
        client.close()

    while running:
        loop_start = time.monotonic()
        try:
            if not client.connect():
                raise ConnectionError(f"cannot connect to {HOST}:{PORT}")
            values = read_values(client)
            apply_boost(client, r, values)
            derived, derived_at = derive_efficiency(r)
            values.update(derived)
            log_device(r, values, int(time.time() * 1000), derived_at)
            log.debug("Logged %d values", len(values))
            backoff = 1
            last_success = time.monotonic()
            cleared_stale = False
        except (ModbusException, ConnectionError, OSError) as exc:
            log.warning("Poll failed (%s), retrying in %ss", exc, backoff)
            handle_failure()
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)
            continue
        except Exception:
            log.exception("Unexpected error, retrying in %ss", backoff)
            handle_failure()
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)
            continue

        time.sleep(max(0.0, POLL_INTERVAL - (time.monotonic() - loop_start)))

    client.close()
    log.info("Stopped")


def selftest() -> bool:
    failures = 0

    def check(name, ok):
        nonlocal failures
        print(("ok   " if ok else "FAIL ") + name)
        failures += 0 if ok else 1

    day, now = "2026-09-23", 1000
    wish = {"active": "1", "target_c": "58", "push_min": "120", "deadline_ms": "2000"}
    off = {**wish, "active": "0"}
    action, plan, st = boost_plan(wish, {}, 520, 0, now, day)
    check("Boost-Start: Normal 58 °C + Push, 52 °C gemerkt",
          action == "start" and plan == [(HR_DHW_NORMAL, 580), (HR_DHW_PUSH, 120)] and st["saved_normal"] == "520")
    check("Boost läuft: nichts schreiben", boost_plan(wish, st, 580, 120, now, day)[:2] == (None, []))
    check("Push abgelaufen, Boost noch gewünscht: Push erneuern",
          boost_plan(wish, st, 580, 0, now, day)[:2] == ("extend", [(HR_DHW_PUSH, 120)]))
    check("Boost beendet: 52 °C zurück, Push aus",
          boost_plan(off, st, 580, 90, now, day)[:2] == ("restore", [(HR_DHW_NORMAL, 520), (HR_DHW_PUSH, 0)]))
    check("Regler meldet sich nicht mehr (Frist vorbei): zurücksetzen",
          boost_plan(wish, st, 580, 90, 3000, day)[0] == "restore")
    check("Rücksetzen, obwohl schon alles stimmt: Merker trotzdem löschen",
          boost_plan(off, st, 520, 0, now, day)[:2] == ("restore", []))
    leftover = {k: v for k, v in st.items() if k != "saved_normal"}
    check("Normal steht noch auf 58 °C (Rest): früher gemerkten Wert nehmen",
          boost_plan(wish, leftover, 580, 0, now, day)[2].get("saved_normal") == "520")
    check("Normal steht auf 58 °C, kein Rücksetzwert bekannt: kein Boost",
          boost_plan(wish, {}, 580, 0, now, day)[0] is None)
    check("Schreiblimit erreicht: kein Boost", boost_plan(wish, {"date": day, "writes": "11"}, 520, 0, now, day)[0] is None)
    check("Rücksetzen trotz Schreiblimit",
          boost_plan(off, {"date": day, "writes": "12", "saved_normal": "520"}, 580, 30, now, day)[0] == "restore")
    check("Register unlesbar: nichts tun", boost_plan(wish, st, None, 0, now, day)[:2] == (None, []))
    check("neuer Tag: Schreibzähler zurück", boost_plan(wish, {"date": "2026-09-22", "writes": "12"}, 520, 0, now, day)[0] == "start")

    print("Alle Tests bestanden" if failures == 0 else f"{failures} Test(s) fehlgeschlagen")
    return failures == 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(0 if selftest() else 1)
    main()
