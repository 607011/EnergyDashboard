"""Polls the Hoymiles S-Miles cloud and logs the values to Redis.

Unlike the SolarEdge poller, this one talks to Hoymiles' cloud (see hoymiles_client.py
for why: at least one of our two microinverters has no local API). Everything it reads
is stored locally in Redis regardless, so history keeps working even if the cloud later
goes away or changes.

A station can have several microinverters attached; the station-level API only reports
their *combined* power/energy, so this polls two things per station:
  - the station's own realtime + energy totals (today/month/year/lifetime kWh) -- these
    can only be read as a combined figure, Hoymiles doesn't expose them per device
  - the "burst" channel for each attached microinverter's own AC power and per-PV-string
    power, so each one still gets an accurate individual dashboard

Writes:
  - a Redis hash        "hoymiles:<station>:latest"    -> station-level totals
  - a Redis hash        "hoymiles:<inverter>:latest"   -> one per microinverter
  - a RedisTimeSeries per numeric field for both, under "ts:hoymiles:<key>:<field>"
"""

import logging
import os
import re
import signal
import time

import redis
import requests

from hoymiles_client import HoymilesAuthError, HoymilesClient

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("hoymiles-poller")

HOYMILES_USER = os.environ["HOYMILES_USER"]
HOYMILES_PASSWORD = os.environ["HOYMILES_PASSWORD"]
# Optional "id:name,id:name" override; if unset, all stations on the account are auto-discovered.
HOYMILES_STATIONS = os.environ.get("HOYMILES_STATIONS", "")

# The cloud is not built for tight polling loops -- a few minutes is plenty for a dashboard.
POLL_INTERVAL = float(os.environ.get("HOYMILES_POLL_INTERVAL", "300"))

REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
REDIS_DB = int(os.environ.get("REDIS_DB", "0"))
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD") or None

TS_RETENTION_MS = int(os.environ.get("TS_RETENTION_DAYS", "365")) * 24 * 60 * 60 * 1000

MAX_BACKOFF = 300

running = True


def handle_signal(signum, _frame):
    global running
    log.info("Received signal %s, shutting down", signum)
    running = False


signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return slug or "device"


def parse_station_overrides(spec: str) -> dict[int, str]:
    overrides = {}
    for entry in spec.split(","):
        entry = entry.strip()
        if not entry:
            continue
        station_id, _, name = entry.partition(":")
        try:
            overrides[int(station_id)] = name or station_id
        except ValueError:
            log.warning("Ignoring malformed HOYMILES_STATIONS entry: %r", entry)
    return overrides


def to_number(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def log_device(r: redis.Redis, device_key: str, values: dict, ts_ms: int) -> None:
    latest_key = f"hoymiles:{device_key}:latest"

    mapping = {k: ("" if v is None else str(v)) for k, v in values.items()}
    mapping["updated_at"] = str(ts_ms)

    pipe = r.pipeline(transaction=False)
    pipe.hset(latest_key, mapping=mapping)

    for field, value in values.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        pipe.ts().add(
            f"ts:hoymiles:{device_key}:{field}",
            ts_ms,
            float(value),
            retention_msecs=TS_RETENTION_MS,
            labels={"device": device_key, "field": field},
            duplicate_policy="last",
        )

    pipe.execute()


def extract_station_values(raw: dict) -> dict:
    return {
        "power_w": to_number(raw.get("real_power")),
        "energy_today_wh": to_number(raw.get("today_eq")),
        "energy_month_wh": to_number(raw.get("month_eq")),
        "energy_year_wh": to_number(raw.get("year_eq")),
        "energy_total_wh": to_number(raw.get("total_eq")),
        "capacity_wp": to_number(raw.get("capacitor")),
        "last_data_time": raw.get("last_data_time") or raw.get("data_time"),
    }


def extract_inverter_values(burst_entry: dict) -> dict:
    values = {"power_w": to_number(burst_entry.get("pac"))}
    for port in ("p1", "p2", "p3", "p4"):
        power = to_number(burst_entry.get(port))
        if power is not None:
            values[f"{port}_w"] = power
    return values


def poll_once(client: HoymilesClient, r: redis.Redis, stations: dict[int, str], inverters_by_station: dict) -> None:
    ts_ms = int(time.time() * 1000)

    for station_id, name in stations.items():
        try:
            raw = client.get_station_realtime(station_id)
            log_device(r, slugify(name), extract_station_values(raw), ts_ms)
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 401:
                raise  # let the caller re-login
            log.warning("Station %s (%s): realtime request failed: %s", station_id, name, exc)
        except HoymilesAuthError:
            raise  # let the caller re-login
        except Exception:
            log.exception("Station %s (%s): failed to fetch station realtime data", station_id, name)

        inverters = inverters_by_station.get(station_id) or []
        if not inverters:
            continue

        try:
            uri = client.get_realtime_uri(station_id)
            burst = client.poll_burst(uri, [inv.serial for inv in inverters])
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 401:
                raise
            log.warning("Station %s (%s): burst request failed: %s", station_id, name, exc)
            continue
        except HoymilesAuthError:
            raise
        except Exception:
            log.exception("Station %s (%s): failed to fetch per-inverter power", station_id, name)
            continue

        by_serial = {entry.get("sn"): entry for entry in burst}
        for inv in inverters:
            entry = by_serial.get(inv.serial)
            if entry is None:
                log.warning("Inverter %s (%s): missing from burst response", inv.serial, inv.model_no)
                continue
            log_device(r, slugify(inv.model_no), extract_inverter_values(entry), ts_ms)


def discover_inverters(client: HoymilesClient, stations: dict[int, str]) -> dict:
    inverters_by_station = {}
    for station_id, name in stations.items():
        try:
            inverters = client.get_microinverters(station_id)
        except Exception:
            log.exception("Failed to read device tree for station %s (%s)", station_id, name)
            inverters = []
        inverters_by_station[station_id] = inverters
        for inv in inverters:
            log.info("Found microinverter %s (%s) on station %s (%s)", inv.serial, inv.model_no, station_id, name)
    return inverters_by_station


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

    client = HoymilesClient(HOYMILES_USER, HOYMILES_PASSWORD)
    backoff = 1

    overrides = parse_station_overrides(HOYMILES_STATIONS)
    stations: dict[int, str] = {}
    inverters_by_station: dict = {}

    while running:
        loop_start = time.monotonic()
        try:
            if client.token is None:
                client.login()
                log.info("Logged in to S-Miles cloud (profile=%s)", client.profile)

            if not stations:
                if overrides:
                    stations = overrides
                else:
                    discovered = client.get_stations()
                    stations = {s.id: s.name for s in discovered}
                    log.info("Discovered stations: %s", stations)
                if not stations:
                    raise RuntimeError("No stations found on this account")
                inverters_by_station = discover_inverters(client, stations)

            poll_once(client, r, stations, inverters_by_station)
            if backoff != 1:
                log.info("Poll recovered")
            backoff = 1
        except (HoymilesAuthError, requests.HTTPError) as exc:
            log.warning("Auth/session problem (%s), re-logging in in %ss", exc, backoff)
            client.token = None
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)
            continue
        except Exception:
            log.exception("Poll failed, retrying in %ss", backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)
            continue

        elapsed = time.monotonic() - loop_start
        time.sleep(max(0.0, POLL_INTERVAL - elapsed))

    log.info("Stopped")


if __name__ == "__main__":
    main()
