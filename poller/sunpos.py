"""Low-precision solar position (azimuth/elevation), accurate to ~0.01 degree.

Implements the geocentric solar coordinate formulas from Jean Meeus,
"Astronomical Algorithms" (2nd ed.), chapters 25 (position of the sun) and 28
(equation of time) -- the same low-precision method underlying NOAA's solar
position calculator.
"""

import math
from datetime import datetime, timezone


def _julian_day(dt_utc: datetime) -> float:
    year = dt_utc.year
    month = dt_utc.month
    day = dt_utc.day + (dt_utc.hour + dt_utc.minute / 60.0 + dt_utc.second / 3600.0) / 24.0

    if month <= 2:
        year -= 1
        month += 12

    a = math.floor(year / 100.0)
    b = 2 - a + math.floor(a / 4.0)

    return math.floor(365.25 * (year + 4716)) + math.floor(30.6001 * (month + 1)) + day + b - 1524.5


def solar_position(dt_utc: datetime, lat_deg: float, lon_deg: float) -> tuple[float, float]:
    """Azimuth (degrees clockwise from north) and elevation (degrees above horizon, refraction-corrected).

    dt_utc must be timezone-aware in UTC (or naive and already representing UTC).
    lon_deg is positive east of Greenwich.
    """
    if dt_utc.tzinfo is not None:
        dt_utc = dt_utc.astimezone(timezone.utc)

    jd = _julian_day(dt_utc)
    t = (jd - 2451545.0) / 36525.0

    l0 = (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360.0
    m = 357.52911 + t * (35999.05029 - 0.0001537 * t)
    e = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)

    m_rad = math.radians(m)
    c = (
        math.sin(m_rad) * (1.914602 - t * (0.004817 + 0.000014 * t))
        + math.sin(2 * m_rad) * (0.019993 - 0.000101 * t)
        + math.sin(3 * m_rad) * 0.000289
    )

    true_long = l0 + c
    omega = 125.04 - 1934.136 * t
    apparent_long = true_long - 0.00569 - 0.00478 * math.sin(math.radians(omega))

    eps0 = 23.0 + (26.0 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))) / 60.0) / 60.0
    eps = eps0 + 0.00256 * math.cos(math.radians(omega))

    decl = math.asin(math.sin(math.radians(eps)) * math.sin(math.radians(apparent_long)))

    y = math.tan(math.radians(eps / 2.0)) ** 2
    l0_rad = math.radians(l0)
    eq_time_deg = (
        y * math.sin(2 * l0_rad)
        - 2 * e * math.sin(m_rad)
        + 4 * e * y * math.sin(m_rad) * math.cos(2 * l0_rad)
        - 0.5 * y * y * math.sin(4 * l0_rad)
        - 1.25 * e * e * math.sin(2 * m_rad)
    )
    eq_time = 4.0 * math.degrees(eq_time_deg)  # minutes

    utc_minutes = dt_utc.hour * 60 + dt_utc.minute + dt_utc.second / 60.0
    true_solar_time = (utc_minutes + eq_time + 4.0 * lon_deg) % 1440.0

    hour_angle = true_solar_time / 4.0 - 180.0
    if true_solar_time / 4.0 < 0:
        hour_angle = true_solar_time / 4.0 + 180.0

    lat_rad = math.radians(lat_deg)
    ha_rad = math.radians(hour_angle)

    cos_zenith = math.sin(lat_rad) * math.sin(decl) + math.cos(lat_rad) * math.cos(decl) * math.cos(ha_rad)
    cos_zenith = max(-1.0, min(1.0, cos_zenith))
    zenith = math.acos(cos_zenith)

    elevation = 90.0 - math.degrees(zenith)
    elevation += _atmospheric_refraction(elevation)

    sin_zenith = math.sin(zenith)
    if abs(sin_zenith) < 1e-6:
        azimuth = 0.0
    else:
        cos_az = (math.sin(lat_rad) * math.cos(zenith) - math.sin(decl)) / (math.cos(lat_rad) * sin_zenith)
        cos_az = max(-1.0, min(1.0, cos_az))
        az_from_south = math.degrees(math.acos(cos_az))
        if hour_angle > 0:
            azimuth = (az_from_south + 180.0) % 360.0
        else:
            azimuth = (540.0 - az_from_south) % 360.0

    return azimuth, elevation


def _atmospheric_refraction(true_elevation_deg: float) -> float:
    """Meeus ch. 16: apparent refraction in degrees, for a true (unrefracted) elevation angle."""
    if true_elevation_deg > 85.0:
        return 0.0

    te = math.tan(math.radians(true_elevation_deg))
    if true_elevation_deg > 5.0:
        refraction_arcmin = 58.1 / te - 0.07 / te**3 + 0.000086 / te**5
    elif true_elevation_deg > -0.575:
        refraction_arcmin = 1735.0 + true_elevation_deg * (
            -518.2 + true_elevation_deg * (103.4 + true_elevation_deg * (-12.79 + true_elevation_deg * 0.711))
        )
    else:
        refraction_arcmin = -20.774 / te

    return refraction_arcmin / 3600.0
