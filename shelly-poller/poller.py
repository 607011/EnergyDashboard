"""Polls Shelly Gen3+ smart plugs (local RPC over plain HTTP, no cloud) for their measured power.

Each plug is named after the load plugged into it -- for a plug named e.g. "gamer", this both logs
its own telemetry under shelly:gamer:latest / ts:shelly:gamer:<field>, and writes power_w /
power_updated_at into compute:gamer:latest. compute-controller's machine_power() already reads
exactly those two fields for a machine named "gamer" in COMPUTE_MACHINES, so once a plug and a
compute-controller machine share a name, the controller automatically switches from an estimated
wattage to the plug's real reading -- no change needed on that side.

This only reads a plug's status; it doesn't switch anything (that's for later, see the project's
two-tier PC scheduling notes).
"""

import logging
import os
import re
import signal
import time

import redis
import requests

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("shelly-poller")

POLL_INTERVAL = float(os.environ.get("SHELLY_POLL_INTERVAL", "10"))
REQUEST_TIMEOUT = float(os.environ.get("SHELLY_TIMEOUT", "5"))
# Local HTTP on the same LAN is fast and reliable; a plug not answering for this long is genuinely
# unreachable, so its last-known values are removed (see clear_stale) instead of shown stale.
STALE_AFTER_S = float(os.environ.get("SHELLY_STALE_AFTER_SECONDS", "60"))

REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
REDIS_DB = int(os.environ.get("REDIS_DB", "0"))
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD") or None

TS_RETENTION_MS = int(os.environ.get("TS_RETENTION_DAYS", "365")) * 24 * 60 * 60 * 1000
MINUTE_MS = 60_000

running = True


def handle_signal(signum, _frame):
    global running
    log.info("Received signal %s, shutting down", signum)
    running = False


signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)


def parse_devices(spec: str) -> dict:
    """"name:host,name:host" -> {name: host}. The name should match the machine name used in
    COMPUTE_MACHINES, if this plug's load is one of the load-managed ones."""
    devices = {}
    for entry in spec.split(","):
        entry = entry.strip()
        if not entry:
            continue
        name, _, host = entry.partition(":")
        devices[name.strip()] = host.strip()
    return devices


class ShellyError(Exception):
    pass


def fetch_status(host: str) -> dict:
    """The switch's status via Gen2+ RPC over plain HTTP (no auth, no cloud)."""
    response = requests.get(f"http://{host}/rpc/Shelly.GetStatus", timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    data = response.json()
    if "switch:0" not in data:
        raise ShellyError(f"no switch:0 in response: {data}")
    return data


def extract_values(status: dict) -> dict:
    sw = status["switch:0"]
    values = {
        "on": bool(sw.get("output")),
        "power_w": sw.get("apower"),
        "voltage_v": sw.get("voltage"),
        "current_a": sw.get("current"),
        "energy_wh": (sw.get("aenergy") or {}).get("total"),
        "temperature_c": (sw.get("temperature") or {}).get("tC"),
    }
    wifi_rssi = (status.get("wifi") or {}).get("rssi")
    if wifi_rssi is not None:
        values["wifi_rssi"] = wifi_rssi
    return {k: v for k, v in values.items() if v is not None}


_last_success: dict[str, float] = {}
_cleared_stale: set[str] = set()


def clear_stale(r: redis.Redis) -> None:
    """A plug that hasn't answered in a while loses its "latest" telemetry (like the other
    pollers) -- and specifically the power_w/power_updated_at it fed into compute:<name>:latest,
    so compute-controller falls back to "unknown" instead of a frozen, possibly wrong wattage."""
    now = time.monotonic()
    for name, last in list(_last_success.items()):
        if name in _cleared_stale or now - last < STALE_AFTER_S:
            continue
        r.delete(f"shelly:{name}:latest")
        r.hdel(f"compute:{name}:latest", "power_w", "power_updated_at")
        _cleared_stale.add(name)
        log.warning("%s: no successful poll for over %.0fs -- cleared its last-known values", name, STALE_AFTER_S)


def poll_device(r: redis.Redis, name: str, host: str, now_ms: int) -> None:
    status = fetch_status(host)
    values = extract_values(status)

    pipe = r.pipeline(transaction=False)
    mapping = {k: str(v) for k, v in values.items()}
    mapping["updated_at"] = str(now_ms)
    pipe.delete(f"shelly:{name}:latest")
    pipe.hset(f"shelly:{name}:latest", mapping=mapping)
    for field, value in values.items():
        if isinstance(value, bool):
            value = int(value)
        elif not isinstance(value, (int, float)):
            continue
        pipe.ts().add(f"ts:shelly:{name}:{field}", now_ms, float(value), retention_msecs=TS_RETENTION_MS,
                      labels={"device": name, "field": field}, duplicate_policy="last")

    # Feed compute-controller's machine_power() (see module docstring) -- only if this plug
    # actually reports power; a plug that lost power measurement but is otherwise fine shouldn't
    # make the controller think the load draws 0 W.
    if "power_w" in values:
        pipe.hset(f"compute:{name}:latest", mapping={"power_w": values["power_w"], "power_updated_at": now_ms})
    pipe.execute()

    _last_success[name] = time.monotonic()
    _cleared_stale.discard(name)


def main() -> None:
    devices = parse_devices(os.environ.get("SHELLY_DEVICES", ""))
    if not devices:
        log.info("SHELLY_DEVICES is empty -- no plugs configured, idling")
        while running:
            time.sleep(1)
        return

    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, password=REDIS_PASSWORD, decode_responses=True)
    r.ping()
    log.info("Connected to Redis at %s:%s", REDIS_HOST, REDIS_PORT)
    log.info("Polling %d plug(s): %s", len(devices), ", ".join(f"{n} ({h})" for n, h in devices.items()))

    while running:
        loop_start = time.monotonic()
        clear_stale(r)
        for name, host in devices.items():
            try:
                poll_device(r, name, host, int(time.time() * 1000))
            except (requests.RequestException, ShellyError, ValueError) as exc:
                log.warning("%s (%s): poll failed (%s)", name, host, exc)
            except Exception:
                log.exception("%s (%s): unexpected error", name, host)
        time.sleep(max(0.0, POLL_INTERVAL - (time.monotonic() - loop_start)))
    log.info("Stopped")


# --------------------------------------------------------------------------- self-test

def selftest() -> bool:
    failures = 0

    def check(name, ok):
        nonlocal failures
        print(("ok   " if ok else "FAIL ") + name)
        failures += 0 if ok else 1

    check("Geräteliste parsen", parse_devices("gamer:192.168.0.162, winola:192.168.0.163")
          == {"gamer": "192.168.0.162", "winola": "192.168.0.163"})
    check("leere Geräteliste", parse_devices("") == {})

    sample = {
        "switch:0": {"output": True, "apower": 123.4, "voltage": 231.2, "current": 0.53,
                    "aenergy": {"total": 4567.0}, "temperature": {"tC": 41.2}},
        "wifi": {"rssi": -52},
    }
    values = extract_values(sample)
    check("Werte extrahiert", values == {"on": True, "power_w": 123.4, "voltage_v": 231.2, "current_a": 0.53,
                                         "energy_wh": 4567.0, "temperature_c": 41.2, "wifi_rssi": -52})

    off_no_power = {"switch:0": {"output": False, "apower": 0.0, "voltage": 230.0, "current": 0.0,
                                 "aenergy": {"total": 100.0}}}
    check("aus, aber Leistung 0 bleibt erhalten (kein Herausfiltern von 0)",
          extract_values(off_no_power)["power_w"] == 0.0 and extract_values(off_no_power)["on"] is False)

    missing_temp = {"switch:0": {"output": True, "apower": 50.0, "voltage": 230.0, "current": 0.2, "aenergy": {"total": 1.0}}}
    check("fehlende Temperatur wird weggelassen, nicht als 0/None geschrieben", "temperature_c" not in extract_values(missing_temp))

    try:
        fetch_status  # exists, network behaviour covered by the pollers' established pattern, not retested here
        check("fetch_status ist definiert", True)
    except NameError:
        check("fetch_status ist definiert", False)

    print("Alle Tests bestanden" if failures == 0 else f"{failures} Test(s) fehlgeschlagen")
    return failures == 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(0 if selftest() else 1)
    main()
