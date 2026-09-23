"""Real actuation for "pc" machines (the PrimeGrid PCs), over SSH plus a Shelly plug.

Two methods, picked per machine via PC_<NAME>_ACTUATOR:
  - "shelly": for the Windows PCs. Turning off means shutting Windows down over SSH, waiting for
    it to actually finish (measured by its Shelly plug's power dropping low) and only then cutting
    the plug -- cutting power while Windows is still shutting down risks filesystem corruption.
    Turning on just means switching the plug back on; the BIOS's "Restore on AC Power Loss" does
    the rest.
  - "wol": for a Mac that doesn't reliably power on from a real power cut (see the project's notes
    on this). Its plug is never touched -- turning off means putting it to sleep over SSH
    (`pmset sleepnow`), turning on means a Wake-on-LAN packet. Power stays on throughout, and the
    plug's own reading (near 0 W asleep, tens of W awake) is what tells the two apart.
    A magic packet alone only gets a Mac to a "DarkWake" (network up, no apps running, back asleep
    after ~45 s), so the packet is followed by `caffeinate -u` over SSH, which declares user activity
    and turns that into a full wake. While the controller wants the Mac on, it keeps a
    `caffeinate -i` running there (pid file, so it never touches anyone else's caffeinate), or the
    Mac's own idle sleep would send it to sleep an hour later: BOINC doesn't hold it awake.

Both methods use the plug's power reading that shelly-poller already keeps in Redis
(shelly:<name>:latest), so no direct network dependency between this module and the plug's actual
state beyond that.
"""

import logging
import socket

import paramiko
import requests

log = logging.getLogger("compute-controller")

# A plug at or below this is "off"/"asleep"; above it, "on"/"awake". Well clear of both a
# powered-down PC's near-zero draw and a sleeping Mac's few watts, and well below any real load.
POWER_THRESHOLD_W = 10.0
SSH_TIMEOUT_S = 10
SHELLY_TIMEOUT_S = 5
# Time for a magic packet to bring the Mac's network up before SSH can reach it.
WOL_SETTLE_S = 8

# Commands for the "wol" method (macOS). The pid file marks the controller's own caffeinate.
_CAFFEINATE_PID = "/tmp/se10k-compute-caffeinate.pid"
MAC_FULL_WAKE = "caffeinate -u -t 5"
MAC_KEEP_AWAKE = (f"p={_CAFFEINATE_PID}; kill -0 $(cat $p 2>/dev/null) 2>/dev/null"
                  " || { nohup caffeinate -i >/dev/null 2>&1 </dev/null & echo $! > $p; }")
MAC_SLEEP = f"p={_CAFFEINATE_PID}; kill $(cat $p 2>/dev/null) 2>/dev/null; rm -f $p; pmset sleepnow"


# --------------------------------------------------------------------------- pure decision logic

def shelly_pc_actions(desired_on: bool, plug_on: bool | None, power_w: float | None, shutdown_sent: bool,
                      owned: bool = True) -> list[str]:
    """What to do for a "shelly"-method PC. Returns a subset of ["shelly_on", "ssh_shutdown",
    "shelly_off"], in the order they'd need doing (there's always at most one meaningful action
    here since each step waits for the last one's effect before proceeding).

    `owned`: the controller itself switched this PC on. It only ever switches off a PC it switched
    on -- one a person started by hand is left alone, even while the controller wants it off.
    Unknown plug state means "don't know what's safe" -> do nothing rather than guess.
    """
    if plug_on is None:
        return []
    if desired_on:
        return [] if plug_on else ["shelly_on"]
    if not plug_on:
        return []  # already off
    if not owned:
        return []
    if power_w is None:
        return []
    if power_w > POWER_THRESHOLD_W:
        return [] if shutdown_sent else ["ssh_shutdown"]
    return ["shelly_off"]  # shutdown has visibly finished


def wol_pc_actions(desired_on: bool, power_w: float | None, owned: bool = True) -> list[str]:
    """What to do for a "wol"-method PC (the Mac). Returns a subset of ["wol", "keep_awake",
    "ssh_sleep"].

    `owned` as for shelly_pc_actions: only a machine the controller woke is kept awake or put back
    to sleep -- one a person woke keeps its own idle-sleep behaviour.
    """
    if power_w is None:
        return []
    awake = power_w > POWER_THRESHOLD_W
    if desired_on and not awake:
        return ["wol"]
    if desired_on and awake and owned:
        return ["keep_awake"]  # idempotent, re-checked every cycle (survives a reboot of the Mac)
    if not desired_on and awake and owned:
        return ["ssh_sleep"]
    return []


# --------------------------------------------------------------------------- I/O

class ActuationError(Exception):
    pass


def ssh_run(host: str, user: str, key_path: str, command: str, wait: bool = False) -> None:
    """Runs `command` over SSH. wait=False fires and forgets (for shutdown/sleep, which take the
    connection down with them); wait=True waits for the command to finish (bounded by the timeout)."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(host, username=user, key_filename=key_path, timeout=SSH_TIMEOUT_S,
                       look_for_keys=False, allow_agent=False)
        _, stdout, _ = client.exec_command(command, timeout=SSH_TIMEOUT_S)
        if wait:
            stdout.read()
    except (paramiko.SSHException, OSError) as exc:
        raise ActuationError(f"ssh {user}@{host} failed: {exc}") from exc
    finally:
        client.close()


def shelly_set_switch(host: str, on: bool, toggle_after_s: float | None = None) -> None:
    """toggle_after_s: the plug itself flips back after that long (a timer that survives us)."""
    params = {"id": 0, "on": str(on).lower()}
    if toggle_after_s:
        params["toggle_after"] = toggle_after_s
    try:
        response = requests.get(f"http://{host}/rpc/Switch.Set", params=params, timeout=SHELLY_TIMEOUT_S)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise ActuationError(f"Shelly {host} switch failed: {exc}") from exc


def send_wol(mac: str, broadcast: str = "255.255.255.255", ports=(9, 7)) -> None:
    try:
        mac_bytes = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    except ValueError as exc:
        raise ActuationError(f"invalid MAC address {mac!r}: {exc}") from exc
    packet = b"\xff" * 6 + mac_bytes * 16
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        for port in ports:
            sock.sendto(packet, (broadcast, port))
    except OSError as exc:
        raise ActuationError(f"sending WoL to {mac} failed: {exc}") from exc
    finally:
        sock.close()
