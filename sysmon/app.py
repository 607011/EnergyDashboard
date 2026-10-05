"""Health of the Raspberry Pi the whole stack runs on, once a minute into Redis.

The Pi 4 before it lost packets in bursts for hours (2026-09-25/26, 2026-10-01) without a trace in
any log; the Pi 5 that replaced it came without a heatsink. So this records what would show
such trouble early: CPU and RP1 temperature, CPU clock (it drops when the Pi throttles), the
under-voltage alarm, load, CPU usage, memory, root filesystem, how much is written to the SD card
(its wear), network counters, and the packet loss to several hosts (20 pings a minute each).

Packet loss to the Pi came back in phases on the Pi 5 too (2026-10-03/04), with nothing in any log,
so two more things are recorded to tell the possible causes apart while it happens:
  - pings not only to the router but also to a wired host (heat pump) and a Wi-Fi one (a Shelly
    plug): only the router lost = the router; all lost = the Pi or its cable;
  - a capture of the incoming frames (raw socket; the default capabilities of a container suffice):
    broadcasts/multicasts per second and the top broadcast sender, and every ARP frame in which
    another device claims the Pi's address or the router's address shows up with a second MAC
    (address conflict / spoofing). Those findings also go to sysmon:<host>:events.

Runs with the host's network (docker-compose: network_mode host) so the interface counters and
the pings are the Pi's own; /proc and /sys are host-wide in a container anyway. Needs no
privileges. Writes sysmon:<host>:latest and ts:sysmon:<host>:<field>.
"""

import logging
import os
import socket
import struct
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import redis

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sysmon")

NAME = os.environ.get("SYSMON_NAME", "pi")
INTERVAL_S = float(os.environ.get("SYSMON_INTERVAL", "60"))
NET_IF = os.environ.get("SYSMON_INTERFACE", "end0")
DISK = os.environ.get("SYSMON_DISK", "mmcblk0")
GATEWAY = os.environ.get("SYSMON_PING_HOST", "192.168.0.1")
# name:address,... -- the first one counts as the router (its loss also stays in ping_loss_pct)
PING_HOSTS = [tuple(e.split(":", 1)) for e in os.environ.get(
    "SYSMON_PING_HOSTS", f"fritzbox:{GATEWAY},waermepumpe:192.168.0.95,steckdose:192.168.0.159").split(",") if ":" in e]
MY_IP = os.environ.get("SYSMON_OWN_IP", "192.168.0.2")
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


def mac_str(b: bytes) -> str:
    return ":".join(f"{x:02x}" for x in b)


class FrameWatch(threading.Thread):
    """Counts incoming frames on the interface and notes ARP claims for our and the router's IP."""

    def __init__(self, iface: str, own_ip: str, gateway_ip: str):
        super().__init__(daemon=True, name="framewatch")
        self.iface = iface
        self.own_mac = bytes.fromhex((read(f"/sys/class/net/{iface}/address") or "00:00:00:00:00:00").replace(":", ""))
        self.own_ip, self.gw_ip = socket.inet_aton(own_ip), socket.inet_aton(gateway_ip)
        self.lock = threading.Lock()
        self.ok = False
        self._reset()

    def _reset(self):
        self.bcast = self.mcast = self.arp = 0
        self.bcast_src = Counter()
        self.conflict = Counter()   # MAC -> ARP frames claiming our IP
        self.gw_macs = Counter()    # MAC -> ARP frames claiming the router's IP

    def run(self):
        try:
            sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.ntohs(0x0003))
            sock.bind((self.iface, 0))
            self.ok = True
        except OSError as exc:
            log.warning("Frame capture not possible (%s) -- broadcast/ARP figures will be missing", exc)
            return
        while True:
            frame, addr = sock.recvfrom(2048)
            if addr[2] == socket.PACKET_OUTGOING or len(frame) < 14:
                continue
            dst, src, etype = frame[0:6], frame[6:12], frame[12:14]
            with self.lock:
                if dst == b"\xff" * 6:
                    self.bcast += 1
                    self.bcast_src[src] += 1
                elif dst[0] & 1:
                    self.mcast += 1
                if etype == b"\x08\x06" and len(frame) >= 42:
                    self.arp += 1
                    sha, spa = frame[22:28], frame[28:32]
                    if spa == self.own_ip and sha != self.own_mac:
                        self.conflict[sha] += 1
                    elif spa == self.gw_ip:
                        self.gw_macs[sha] += 1

    def snapshot(self, dt_s: float) -> tuple[dict, dict, list]:
        """(numbers, texts, events) since the last call; resets the counters."""
        if not self.ok or dt_s <= 0:
            return {}, {}, []
        with self.lock:
            bcast, mcast, arp = self.bcast, self.mcast, self.arp
            top = self.bcast_src.most_common(1)
            conflict, gw = dict(self.conflict), dict(self.gw_macs)
            self._reset()
        nums = {"net_bcast_per_s": bcast / dt_s, "net_mcast_per_s": mcast / dt_s, "net_arp_per_min": arp * 60 / dt_s,
                "net_ip_conflict": sum(conflict.values()), "net_gw_macs": len(gw)}
        texts = {"net_top_bcast": f"{mac_str(top[0][0])} ({top[0][1]})" if top else ""}
        events = []
        if conflict:
            events.append("Adresskonflikt: " + ", ".join(f"{mac_str(m)} behauptet {socket.inet_ntoa(self.own_ip)} ({n}×)"
                                                        for m, n in conflict.items()))
        if len(gw) > 1:
            events.append("Router-Adresse mit mehreren MACs: " + ", ".join(f"{mac_str(m)} ({n}×)" for m, n in gw.items()))
        if bcast / dt_s > 50 and top:
            events.append(f"Rundsende-Flut: {bcast / dt_s:.0f}/s, meiste von {mac_str(top[0][0])} ({top[0][1]})")
        return nums, texts, events


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

    with ThreadPoolExecutor(max_workers=len(PING_HOSTS) or 1) as pool:
        results = list(pool.map(lambda h: (h[0], *ping_loss(h[1], PINGS)), PING_HOSTS))
    for i, (name, loss, rtt) in enumerate(results):
        if loss is not None:
            values[f"ping_loss_{name}_pct"] = loss
            if i == 0:
                values["ping_loss_pct"] = loss
        if rtt is not None:
            values[f"ping_rtt_{name}_ms"] = rtt
            if i == 0:
                values["ping_rtt_ms"] = rtt
    return values, now


def store(values: dict, ts_ms: int, texts: dict | None = None, events: list | None = None) -> None:
    pipe = r.pipeline(transaction=False)
    pipe.delete(f"sysmon:{NAME}:latest")
    pipe.hset(f"sysmon:{NAME}:latest", mapping={**{k: round(v, 2) for k, v in values.items()}, **(texts or {}),
                                                "updated_at": ts_ms})
    stamp = datetime.now().strftime("%d.%m. %H:%M")
    for e in events or []:
        pipe.lpush(f"sysmon:{NAME}:events", f"{stamp}  {e}")
        log.warning(e)
    pipe.ltrim(f"sysmon:{NAME}:events", 0, 499)
    for k, v in values.items():
        pipe.ts().add(f"ts:sysmon:{NAME}:{k}", ts_ms, float(v), retention_msecs=RETENTION_MS,
                      labels={"device": f"sysmon-{NAME}", "field": k}, duplicate_policy="last")
    pipe.execute()


def main() -> None:
    log.info("Monitoring %s (interface %s, disk %s, pinging %s) every %.0f s", NAME, NET_IF, DISK,
             ", ".join(f"{n} {a}" for n, a in PING_HOSTS), INTERVAL_S)
    watch = FrameWatch(NET_IF, MY_IP, PING_HOSTS[0][1] if PING_HOSTS else GATEWAY)
    watch.start()
    prev, prev_t = {}, 0.0
    while True:
        started = time.monotonic()
        try:
            dt = started - prev_t if prev_t else 0
            values, prev_now = sample(prev, dt)
            nums, texts, events = watch.snapshot(dt)
            values.update(nums)
            prev, prev_t = prev_now, started
            store(values, int(time.time() * 1000), texts, events)
        except Exception:
            log.exception("Sampling failed")
        time.sleep(max(1.0, INTERVAL_S - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
