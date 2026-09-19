# SE10K Modbus → Redis → Grafana

Polls a SolarEdge SE10K-RWB48 locally via Modbus TCP (SunSpec profile),
writes the values to Redis, and visualizes them in a Grafana dashboard —
completely without the SolarEdge cloud.

Automatically detected and logged:
- the inverter itself
- attached energy meters (if present)
- attached batteries (if present, e.g. SolarEdge Home Battery)

## Setup

1. On the inverter, under *Communication → Modbus TCP*, "Enabled" must be set
   (already the case for you, IP `192.168.0.174`).
2. Create `.env` from the template and adjust as needed:

   ```bash
   cp .env.example .env
   ```

3. Start:

   ```bash
   docker compose up -d --build
   ```

4. Check the logs:

   ```bash
   docker compose logs -f poller
   ```

5. Open the dashboard: [http://localhost:3000](http://localhost:3000) — also reachable
   from other devices on the LAN at `http://<docker-host-ip>:3000`.
   (Login: `admin` / the `GRAFANA_ADMIN_PASSWORD` set in `.env`, default `admin`).
   The "SolarEdge SE10K-RWB48" dashboard is available automatically (provisioning).

## Architecture

```
SolarEdge SE10K  --Modbus TCP-->  poller (Python)  --> Redis (redis-stack)  <-- Grafana
```

- **poller**: polls all registers every `POLL_INTERVAL` seconds, folds SunSpec
  value/scale register pairs into finished numbers, and writes them to Redis.
- **redis** (image `redis/redis-stack-server`): plain Redis *plus* the
  RedisTimeSeries module for the history data.
- **grafana**: with the [Redis datasource plugin](https://github.com/RedisGrafana/grafana-redis-datasource),
  datasource and dashboard are pre-provisioned (`grafana/provisioning/`,
  `grafana/dashboards/solaredge.json`). Port `3000` is bound on all
  interfaces, so it's reachable from the whole LAN.
- **grafana-init**: a one-shot helper container that, after every start, uses
  the Grafana HTTP API to create the extra users listed in `GRAFANA_USERS`
  (`.env`) or update their password/role, then exits.

## Data model in Redis

There are two kinds of keys per device:

- `solaredge:<device>:latest` — hash with the most recent values (e.g. `power_ac`,
  `energy_total`, `status_label`, `temperature`, `updated_at`, ...) — for stat/gauge panels
- `ts:<device>:<field>` — a RedisTimeSeries key per numeric field, e.g.
  `ts:inverter:power_ac` — for history graphs in Grafana

`<device>` is `inverter`, `meter:meter1`, `battery:battery1`, etc.

Examples:

```bash
# current inverter values
redis-cli HGETALL solaredge:inverter:latest

# power history of the last hour
redis-cli TS.RANGE ts:inverter:power_ac $(($(date +%s%3N)-3600000)) +
```

History length is capped via `TS_RETENTION_DAYS` (default: 365 days),
older samples fall off automatically (RedisTimeSeries retention). If the
value changes, the poller aligns the retention of all existing series on
the next start (`TS.ADD` itself doesn't change it on existing keys, see
`sync_retention()` in `poller/poller.py`).

### Long-term statistics (compaction)

So multi-year analyses don't run out of disk space, the poller additionally
creates three **indefinitely retained** daily rollups (RedisTimeSeries
compaction rules, `TS.CREATERULE`):

- `ts:inverter:energy_total:daily` — end-of-day value of the lifetime counter (`LAST`)
- `ts:inverter:power_pv_total:daily_avg` — daily average PV power
- `ts:inverter:power_pv_total:daily_max` — daily peak PV power

These three series only grow by ~365 points per year and can therefore be
kept forever. For monthly production, `energy_total:daily` is the best fit
since it's a monotonically increasing counter — monthly production is
simply the difference between the end and start value, with no rounding
error from averaging:

```bash
# end value at month end minus end value at previous month end = that month's production (Wh)
redis-cli TS.RANGE ts:inverter:energy_total:daily <from_ts_ms> <to_ts_ms>
```

Further rollups can be added the same way in `COMPACTION_RULES`
(`poller/poller.py`).

## Important fields

**Inverter:** `power_ac` (W), `power_dc` (W), `energy_total` (Wh, cumulative),
`temperature` (°C), `frequency` (Hz), `status` / `status_label`
(Off/Sleeping/Producing/Fault/...), AC voltage/current per phase
(`l1_voltage`, `l1_current`, ...)

**Meter** (if present): `power` (W, positive = export to the grid,
negative = import from the grid — verified against the SolarEdge app),
`export_energy_active`, `import_energy_active`

**Battery** (if present): `soe` (State of Energy / charge level in %),
`instantaneous_power` (positive = charging, negative = discharging), `status` /
`status_label`, `available_energy`

**Important special case with a DC-coupled battery (hybrid inverter like
the SE10K-RWB48):** the battery hangs off the same DC bus as the panels, but
*before* the inverter's actual DC/AC conversion stage. So while the battery
is charging, `power_dc` only shows what's left over for AC conversion — not
the panels' total output. The poller therefore additionally computes
`power_pv_total = power_dc + battery charging power` (only while the
battery's `status_label` is `Charge`) as the best approximation of the
panels' actual total output, the same way the SolarEdge app shows it as
"current solar power".

## Dashboard

Includes: total PV power (incl. battery-charging share), AC output power,
grid power, battery power, house consumption, temperature, status, battery
charge level (current + history), inverter power history, lifetime yield,
grid frequency, sun position (azimuth/elevation), and outside
temperature/irradiance/cloud cover. Meter/battery panels stay empty if no
corresponding devices are connected to the inverter.

The dashboard lives as JSON under `grafana/dashboards/solaredge.json` and is
loaded automatically on startup (Grafana provisioning); changes made in the
Grafana UI can be saved back there via "Export → Save JSON".

## Configuration (`.env`)

| Variable                  | Default            | Meaning                                    |
|----------------------------|--------------------|---------------------------------------------|
| `INVERTER_HOST`            | `192.168.0.174`    | IP of the inverter                          |
| `INVERTER_PORT`            | `502`              | Modbus TCP port                             |
| `MODBUS_UNIT`              | `1`                | Modbus unit/slave ID                        |
| `MODBUS_TIMEOUT`           | `5`                | Timeout per read attempt (seconds)          |
| `POLL_INTERVAL`            | `10`               | Interval between two polls (seconds)        |
| `TS_RETENTION_DAYS`        | `365`              | Retention period of the raw history series  |
| `GRAFANA_ADMIN_PASSWORD`   | `admin`            | Grafana login on first start                |
| `GRAFANA_USERS`            | *(empty)*          | Additional Grafana users, see below         |

The poller automatically reconnects with exponential backoff if the
inverter is briefly unreachable (e.g. at night in standby, or during
network issues).

### Additional Grafana users (`GRAFANA_USERS`)

Format in `.env`, comma-separated, each entry `login:password:role`
(role optional, defaults to `Viewer`):

```bash
GRAFANA_USERS=family:aSecurePassword:Viewer,partner:anotherPassword:Editor
```

On every `docker compose up`, the `grafana-init` container creates these
users via the Grafana API, or syncs their password and role if they already
exist — no manual setup in the UI needed. Roles: `Viewer` (view only),
`Editor` (can edit dashboards), `Admin` (full access).

**Important:** `GRAFANA_ADMIN_PASSWORD` only takes effect on the very first
start (see above) — `GRAFANA_USERS`, on the other hand, is re-applied on
*every* start, since it's provisioned via the running API rather than only
set at first boot. So a changed password in `.env` for an existing user is
picked up on the next `docker compose up`.
