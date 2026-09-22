"""Thin wrapper around tinytuya for local (LAN-only) control of a Tuya device.

No cloud calls at runtime: once a device's id/local_key are known (see README --
`python -m tinytuya wizard` against the Tuya IoT Cloud once, to read them out), everything here
talks straight to the device on the LAN.

Tuya devices expose their state as "DPS" (data points), a small dict of numbered fields whose
meaning is product-specific (e.g. {"1": true, "2": 45} where "1" might be the power switch and "2"
the target humidity). Which DPS number means what isn't discoverable in general -- it's read off a
live device with `status()` and configured via DPS_* environment variables (see README).
"""

import logging

import tinytuya

log = logging.getLogger("compute-controller")


class TuyaDeviceError(Exception):
    pass


class TuyaDevice:
    def __init__(self, name: str, device_id: str, local_key: str, ip: str | None, version: str):
        self.name = name
        self._dev = tinytuya.Device(dev_id=device_id, address=ip or "Auto", local_key=local_key, version=version)
        self._dev.set_socketTimeout(5)
        self._logged_dps_once = False

    def status(self) -> dict:
        """Raw DPS dict, e.g. {"1": True, "2": 45}. Raises TuyaDeviceError if unreachable."""
        result = self._dev.status()
        if not isinstance(result, dict) or "dps" not in result:
            raise TuyaDeviceError(f"{self.name}: unexpected response: {result}")
        dps = result["dps"]
        if not self._logged_dps_once:
            log.info("%s: raw DPS on first successful read: %s -- use these to fill in the "
                     "TUYA_%s_DPS_* variables (see README)", self.name, dps, self.name.upper())
            self._logged_dps_once = True
        return dps

    def set_switch(self, dps_id: str, on: bool) -> None:
        result = self._dev.set_value(dps_id, on)
        if isinstance(result, dict) and result.get("Error"):
            raise TuyaDeviceError(f"{self.name}: set_value failed: {result}")
