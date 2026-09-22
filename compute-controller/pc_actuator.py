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


# --------------------------------------------------------------------------- pure decision logic

def shelly_pc_actions(desired_on: bool, plug_on: bool | None, power_w: float | None, shutdown_sent: bool) -> list[str]:
    """What to do for a "shelly"-method PC. Returns a subset of ["shelly_on", "ssh_shutdown",
    "shelly_off"], in the order they'd need doing (there's always at most one meaningful action
    here since each step waits for the last one's effect before proceeding).

    Unknown plug state means "don't know what's safe" -> do nothing rather than guess.
    """
    if plug_on is None:
        return []
    if desired_on:
        return [] if plug_on else ["shelly_on"]
    if not plug_on:
        return []  # already off
    if power_w is None:
        return []
    if power_w > POWER_THRESHOLD_W:
        return [] if shutdown_sent else ["ssh_shutdown"]
    return ["shelly_off"]  # shutdown has visibly finished


def wol_pc_actions(desired_on: bool, power_w: float | None) -> list[str]:
    """What to do for a "wol"-method PC (the Mac). Returns a subset of ["wol", "ssh_sleep"]."""
    if power_w is None:
        return []
    awake = power_w > POWER_THRESHOLD_W
    if desired_on and not awake:
        return ["wol"]
    if not desired_on and awake:
        return ["ssh_sleep"]
    return []


# --------------------------------------------------------------------------- I/O

class ActuationError(Exception):
    pass


def ssh_run(host: str, user: str, key_path: str, command: str) -> None:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(host, username=user, key_filename=key_path, timeout=SSH_TIMEOUT_S,
                       look_for_keys=False, allow_agent=False)
        client.exec_command(command, timeout=SSH_TIMEOUT_S)
    except (paramiko.SSHException, OSError) as exc:
        raise ActuationError(f"ssh {user}@{host} failed: {exc}") from exc
    finally:
        client.close()


def shelly_set_switch(host: str, on: bool) -> None:
    try:
        response = requests.get(f"http://{host}/rpc/Switch.Set", params={"id": 0, "on": str(on).lower()},
                                timeout=SHELLY_TIMEOUT_S)
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
