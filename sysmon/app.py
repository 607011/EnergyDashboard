"""Health of the Raspberry Pi the whole stack runs on, once a minute into Redis.

The Pi 4 before it lost packets in bursts for hours (2026-09-25/26, 2026-10-01) without a trace in
any log; the Pi 5 that replaced it came without a heatsink. So this records what would show
such trouble early: CPU and RP1 temperature, CPU clock (it drops when the Pi throttles), the
under-voltage alarm, load, CPU usage, memory, root filesystem, how much is written to the SD card
(its wear), network counters, and the packet loss to the router (20 pings a minute).

Runs with the host's network (docker-compose: network_mode host) so the interface counters and
the pings are the Pi's own; /proc and /sys are host-wide in a container anyway. Needs no
privileges. Writes sysmon:<host>:latest and ts:sysmon:<host>:<field>.
"""

import logging
import os
import socket
import struct
import time

import redis

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sysmon")

NAME = os.environ.get("SYSMON_NAME", "pi")
INTERVAL_S = float(os.environ.get("SYSMON_INTERVAL", "60"))
NET_IF = os.environ.get("SYSMON_INTERFACE", "end0")
DISK = os.environ.get("SYSMON_DISK", "mmcblk0")
GATEWAY = os.environ.get("SYSMON_PING_HOST", "192.168.0.1")
PINGS = int(os.environ.get("SYSMON_PINGS", "20"))
RETENTION_MS = int(os.environ.get("TS_RETENTION_DAYS", "365")) * 24 * 3600 * 1000

r = redis.Redis(host=os.environ.get("REDIS_HOST", "127.0.0.1"), port=int(os.environ.get("REDIS_PORT", "6379")),
                password=os.environ.get("REDIS_PASSWORD") or None, decode_responses=True)


def read(path: str) -> str | None:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def hwmon(name: str, entry: str) -> float | None:
    """A value from the hwmon device with that name (cpu_thermal, rp1_adc, rpi_volt)."""
    base = "/sys/class/hwmon"
    try:
        for h in os.listdir(base):
            if read(f"{base}/{h}/name") == name:
                value = read(f"{base}/{h}/{entry}")
                return float(value) if value is not None else None
    except (OSError, ValueError):
        pass
    return None


def cpu_times() -> tuple[int, int]:
    """(busy, total) jiffies from /proc/stat."""
    fields = [int(x) for x in read("/proc/stat").splitlines()[0].split()[1:]]
    idle = fields[3] + fields[4]  # idle + iowait
    return sum(fields) - idle, sum(fields)


def disk_sectors() -> tuple[int, int]:
    """(read, written) 512-byte sectors of the SD card since boot."""
    for line in read("/proc/diskstats").splitlines():
        parts = line.split()
        if parts[2] == DISK:
            return int(parts[5]), int(parts[9])
    return 0, 0


def net(counter: str) -> int:
    return int(read(f"/sys/class/net/{NET_IF}/statistics/{counter}") or 0)


def ping_loss(host: str, count: int) -> tuple[float | None, float | None]:
    """(loss %, mean rtt ms) with unprivileged ICMP sockets, 0.25 s apart, 1 s timeout each."""
    received, rtts = 0, []
    for seq in range(count):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP) as sock:
                sock.settimeout(1.0)
                t = time.monotonic()
                sock.sendto(struct.pack("!BBHHH", 8, 0, 0, 0, seq) + b"sysmon", (host, 0))
                sock.recvfrom(1024)
                rtts.append((time.monotonic() - t) * 1000)
                received += 1
        except PermissionError:
            return None, None
        except OSError:
            pass
        time.sleep(0.25)
    return 100.0 * (count - received) / count, (sum(rtts) / len(rtts) if rtts else None)


def meminfo() -> dict:
    out = {}
    for line in read("/proc/meminfo").splitlines():
        key, _, rest = line.partition(":")
        out[key] = int(rest.split()[0]) * 1024
    return out


def sample(prev: dict, dt_s: float) -> tuple[dict, dict]:
    values = {}
    temp = read("/sys/class/thermal/thermal_zone0/temp")
    if temp:
        values["cpu_temp_c"] = int(temp) / 1000
    rp1 = hwmon("rp1_adc", "temp1_input")
    if rp1 is not None:
        values["rp1_temp_c"] = rp1 / 1000
    freq = read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
    if freq:
        values["cpu_freq_mhz"] = int(freq) / 1000
    uv = hwmon("rpi_volt", "in0_lcrit_alarm")
    if uv is not None:
        values["undervoltage"] = uv
    load = read("/proc/loadavg").split()
    values["load1"], values["load5"], values["load15"] = (float(x) for x in load[:3])
    values["uptime_h"] = float(read("/proc/uptime").split()[0]) / 3600

    mem = meminfo()
    values["mem_used_pct"] = 100.0 * (mem["MemTotal"] - mem["MemAvailable"]) / mem["MemTotal"]
    values["swap_used_mb"] = (mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)) / 2**20
    st = os.statvfs("/")  # the container's overlay reports the Pi's root filesystem
    used = st.f_blocks - st.f_bfree
    values["disk_used_pct"] = 100.0 * used / (used + st.f_bavail)  # like df (blocks reserved for root not counted)

    now = {"cpu": cpu_times(), "disk": disk_sectors(),
           **{c: net(c) for c in ("rx_bytes", "tx_bytes", "rx_dropped", "rx_errors", "tx_errors")}}
    if prev and dt_s > 0:
        busy = now["cpu"][0] - prev["cpu"][0]
        total = now["cpu"][1] - prev["cpu"][1]
        if total > 0:
            values["cpu_usage_pct"] = 100.0 * busy / total
        per_hour = 3600 / dt_s
        values["sd_read_mb_h"] = (now["disk"][0] - prev["disk"][0]) * 512 / 2**20 * per_hour
        values["sd_write_mb_h"] = (now["disk"][1] - prev["disk"][1]) * 512 / 2**20 * per_hour
        values["net_rx_kbit_s"] = (now["rx_bytes"] - prev["rx_bytes"]) * 8 / 1000 / dt_s
        values["net_tx_kbit_s"] = (now["tx_bytes"] - prev["tx_bytes"]) * 8 / 1000 / dt_s
        for c in ("rx_dropped", "rx_errors", "tx_errors"):
            values[f"net_{c}_per_min"] = (now[c] - prev[c]) * 60 / dt_s

    loss, rtt = ping_loss(GATEWAY, PINGS)
    if loss is not None:
        values["ping_loss_pct"] = loss
    if rtt is not None:
        values["ping_rtt_ms"] = rtt
    return values, now


def store(values: dict, ts_ms: int) -> None:
    pipe = r.pipeline(transaction=False)
    pipe.delete(f"sysmon:{NAME}:latest")
    pipe.hset(f"sysmon:{NAME}:latest", mapping={**{k: round(v, 2) for k, v in values.items()}, "updated_at": ts_ms})
    for k, v in values.items():
        pipe.ts().add(f"ts:sysmon:{NAME}:{k}", ts_ms, float(v), retention_msecs=RETENTION_MS,
                      labels={"device": f"sysmon-{NAME}", "field": k}, duplicate_policy="last")
    pipe.execute()


def main() -> None:
    log.info("Monitoring %s (interface %s, disk %s, pinging %s) every %.0f s", NAME, NET_IF, DISK, GATEWAY, INTERVAL_S)
    prev, prev_t = {}, 0.0
    while True:
        started = time.monotonic()
        try:
            values, prev_now = sample(prev, started - prev_t if prev_t else 0)
            prev, prev_t = prev_now, started
            store(values, int(time.time() * 1000))
        except Exception:
            log.exception("Sampling failed")
        time.sleep(max(1.0, INTERVAL_S - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
