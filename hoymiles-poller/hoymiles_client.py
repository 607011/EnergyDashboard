"""Minimal client for the Hoymiles S-Miles cloud (reverse-engineered, unofficial).

Hoymiles' WB-series microinverters ("HiFlow Pro", e.g. HMS-1600-4WB) have built-in
WiFi/Bluetooth but -- unlike the plain "-W"/"-T" HMS models -- expose no local
TCP/protobuf API. The only way to read them is via the S-Miles cloud, using the
same login flow and endpoints the S-Miles Android app uses. (A station can mix
both kinds of inverters -- ours does -- so for uniformity everything here goes
through the cloud, even for the "-W" one that would also support a local API.)

Endpoints and the Argon2id challenge parameters below were derived from the
(MIT-licensed, actively maintained) ioBroker.hoymiles adapter's cloudConnection.ts:
https://github.com/Eistee82/ioBroker.hoymiles
"""

import base64
import hashlib
from dataclasses import dataclass

import requests
from argon2.low_level import Type, hash_secret_raw

CLOUD_HOST_DEFAULT = "https://neapi.hoymiles.com"

IAM_REGION_PATH = "/iam/pub/0/c/region_c"
IAM_PRE_INSPECT_PATH = "/iam/pub/3/auth/pre-insp"
IAM_LOGIN_V3_PATH = "/iam/pub/3/auth/login"
PROFILE_PROBE_PATH = "/pvm/api/0/station/select_by_page"

STATION_LIST_PATH = "/pvm/api/0/station/select_by_page"
STATION_LIST_PATH_HOME = "/pvmc/api/0/station/select_by_page_c"
STATION_REALTIME_PATH = "/pvm-data/api/0/station/data/count_station_real_data"
STATION_REALTIME_PATH_HOME = "/pvmc/api/0/station_data/count_station_real_data_c"
DEVICE_TREE_PATH = "/pvm/api/0/station/select_device_of_tree"
REALTIME_URI_PATH = "/pvm/api/0/station/get_sd_uri"

# Devices in the tree with this dev_type are individual microinverters (as opposed to
# the DTU/gateway node they hang off of).
DEV_TYPE_MICROINVERTER = 3

APP_USER_AGENT_PREFIX = "sma/ad"
APP_VERSION = "2.9.0"
APP_TID = 159
REQUEST_TIMEOUT = 15


class HoymilesAuthError(Exception):
    pass


@dataclass
class Station:
    id: int
    name: str


@dataclass
class Microinverter:
    serial: str
    model_no: str
    station_id: int
    port_count: int


def _build_legacy_challenge(password: bytes) -> str:
    md5_hex = hashlib.md5(password).hexdigest()
    sha256_b64 = base64.b64encode(hashlib.sha256(password).digest()).decode()
    return f"{md5_hex}.{sha256_b64}"


def _build_argon2_challenge(password: bytes, salt_hex: str) -> str:
    """Matches the S-Miles app exactly (Argon2IDUtil.kt): Argon2id, t=3, m=32 MiB, p=1, hashLen=32."""
    raw = hash_secret_raw(
        secret=password,
        salt=bytes.fromhex(salt_hex),
        time_cost=3,
        memory_cost=32768,
        parallelism=1,
        hash_len=32,
        type=Type.ID,
    )
    return raw.hex()


class HoymilesClient:
    def __init__(self, user: str, password: str):
        self.user = user
        self.password = password.encode("utf-8")
        self.session = requests.Session()
        self.base_url = CLOUD_HOST_DEFAULT
        self.token: str | None = None
        self.profile: str = "installer"  # "installer" or "home", set by login()
        self.dc: int = 0  # data-center marker from region_c, embedded in the User-Agent

    def _user_agent(self) -> str:
        return f"{APP_USER_AGENT_PREFIX}/{APP_VERSION}/{APP_TID}/{self.dc}"

    def _post(self, path: str, body: dict, base_url: str | None = None, auth: bool = True) -> dict:
        headers = {"User-Agent": self._user_agent()}
        if auth and self.token:
            headers["Authorization"] = self.token
        response = self.session.post(
            (base_url or self.base_url) + path,
            json=body,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        result = response.json()
        # An expired session token is *not* reported as HTTP 401: the cloud answers 200 with a
        # non-zero status and "token verify error" (it expires after about 24 hours).
        if auth and isinstance(result, dict) and result.get("status") != "0" \
                and "token" in str(result.get("message", "")).lower():
            raise HoymilesAuthError(f"session token rejected: {result.get('message')}")
        return result

    def login(self) -> None:
        # Phase 1: region -- non-fatal on failure, we just keep the default host.
        try:
            region = self._post(IAM_REGION_PATH, {"email": self.user}, auth=False)
            region_data = region.get("data") or {}
            if region.get("status") == "0" and region_data.get("login_url"):
                self.base_url = region_data["login_url"]
            if isinstance(region_data.get("dc"), int):
                self.dc = region_data["dc"]
        except requests.RequestException:
            pass

        # Phase 2: pre-inspect -- gives us a nonce and (usually) an Argon2 salt.
        pre_insp = self._post(IAM_PRE_INSPECT_PATH, {"u": self.user}, auth=False)
        if pre_insp.get("status") != "0":
            raise HoymilesAuthError(f"pre-insp failed: {pre_insp.get('message')}")

        pre_data = pre_insp.get("data") or {}
        nonce = pre_data.get("n")
        salt = pre_data.get("a")
        challenge = _build_argon2_challenge(self.password, salt) if salt else _build_legacy_challenge(self.password)

        # Phase 3: login with the computed challenge.
        login_result = self._post(IAM_LOGIN_V3_PATH, {"u": self.user, "ch": challenge, "n": nonce}, auth=False)
        if login_result.get("status") != "0":
            raise HoymilesAuthError(f"login rejected: {login_result.get('message')}")

        token = (login_result.get("data") or {}).get("token")
        if not token:
            raise HoymilesAuthError("login accepted but no token returned")
        self.token = token

        # Phase 4: profile probe -- "installer"/"web" accounts get status=0 on /pvm/...;
        # "home" (S-Miles Home app) accounts are rejected here and use /pvmc/.../*_c instead.
        try:
            probe = self._post(PROFILE_PROBE_PATH, {"page": 1, "page_size": 1})
            self.profile = "installer" if probe.get("status") == "0" else "home"
        except requests.HTTPError:
            self.profile = "home"

    def get_stations(self) -> list[Station]:
        path = STATION_LIST_PATH_HOME if self.profile == "home" else STATION_LIST_PATH
        result = self._post(path, {"page": 1, "page_size": 100})
        if result.get("status") != "0":
            raise RuntimeError(f"station list failed: {result.get('message')}")
        entries = (result.get("data") or {}).get("list") or []
        stations = []
        for entry in entries:
            station_id = entry.get("id") or entry.get("sid")
            if station_id:
                stations.append(Station(id=int(station_id), name=entry.get("name") or str(station_id)))
        return stations

    def get_station_realtime(self, station_id: int) -> dict:
        path = STATION_REALTIME_PATH_HOME if self.profile == "home" else STATION_REALTIME_PATH
        result = self._post(path, {"sid": station_id})
        if result.get("status") != "0":
            raise RuntimeError(f"realtime data failed: {result.get('message')}")
        return result.get("data") or {}

    def get_microinverters(self, station_id: int) -> list[Microinverter]:
        """Flatten the station's device tree (DTU -> microinverter) into a plain list.

        A station can have several DTUs, each with one or more microinverters attached;
        only the microinverter nodes (dev_type == 3) are relevant for per-device power.
        """
        result = self._post(DEVICE_TREE_PATH, {"id": station_id})
        if result.get("status") != "0":
            raise RuntimeError(f"device tree failed: {result.get('message')}")

        inverters = []
        for dtu in result.get("data") or []:
            for child in dtu.get("children") or []:
                if child.get("type") != DEV_TYPE_MICROINVERTER:
                    continue
                serial = child.get("sn")
                if not serial:
                    continue
                port_array = (child.get("extend_data") or {}).get("port_array") or []
                inverters.append(
                    Microinverter(
                        serial=serial,
                        model_no=child.get("model_no") or serial,
                        station_id=station_id,
                        port_count=len(port_array) or 2,
                    )
                )
        return inverters

    def get_realtime_uri(self, station_id: int) -> str:
        """URL for the fast-updating "burst" channel -- the token embedded in its query
        string is short-lived, so fetch a fresh one for each poll rather than caching it."""
        result = self._post(REALTIME_URI_PATH, {"sid": station_id})
        if result.get("status") != "0":
            raise RuntimeError(f"get_sd_uri failed: {result.get('message')}")
        uri = (result.get("data") or {}).get("uri")
        if not uri:
            raise RuntimeError("get_sd_uri returned no uri")
        return uri if uri.startswith("http") else f"https://{uri}"

    def poll_burst(self, uri: str, inverter_serials: list[str]) -> list[dict]:
        """Per-inverter realtime AC power (`pac`) and per-PV-string power (`p1`..`p4`)."""
        headers = {"Authorization": self.token or "", "User-Agent": self._user_agent()}
        response = self.session.post(
            uri,
            json={"m": 3, "mis": inverter_serials, "t": 1},
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        result = response.json()
        if result.get("status") != "0":
            raise RuntimeError(f"burst poll failed: {result.get('message')}")
        return (result.get("data") or {}).get("mis") or []
