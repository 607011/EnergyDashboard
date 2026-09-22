# SE10K Modbus → Redis → Grafana

Polls a SolarEdge SE10K-RWB48 locally via Modbus TCP (SunSpec profile),
writes the values to Redis, and visualizes them in a Grafana dashboard —
completely without the SolarEdge cloud. Also polls any Hoymiles WB-series
microinverters via the Hoymiles cloud (see below for why that one *does*
need a cloud) so their history is preserved locally too.

Automatically detected and logged:
- the inverter itself
- attached energy meters (if present)
- attached batteries (if present, e.g. SolarEdge Home Battery)
- any Hoymiles stations configured under `HOYMILES_*` (see [Hoymiles microinverters](#hoymiles-microinverters))
- a Weishaupt heat pump, if `WEISHAUPT_HOST` is set (see [Weishaupt heat pump](#weishaupt-heat-pump))

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
- **hoymiles-poller**: polls the Hoymiles S-Miles cloud every
  `HOYMILES_POLL_INTERVAL` seconds for each configured (or auto-discovered)
  station and writes it to Redis the same way the SolarEdge poller does.
- **weishaupt-poller**: reads a Weishaupt heat pump via Modbus TCP every
  `WEISHAUPT_POLL_INTERVAL` seconds; idles if `WEISHAUPT_HOST` is empty.
- **meter-form**: a small web form for entering the heat pump's electricity
  meter reading by hand (see [Weishaupt heat pump](#weishaupt-heat-pump)).

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
`power_pv_total = power_dc + battery power` (signed: charging adds, discharging
subtracts, so it is always what the panels themselves deliver) as the best
approximation of the panels' actual total output, the same way the SolarEdge
app shows it as "current solar power".

The two terms are measured separately (DC input vs. battery terminals, in different
Modbus reads), so while the battery discharges the sum can dip slightly below zero
from converter losses and timing skew (about −60 W in the evening, up to a few
hundred watts during fast load changes). The panels can't deliver negative power,
so `power_pv_total` is clamped at 0 and the unclamped sum is kept as
`power_pv_total_raw`. Samples stored before this change may still be negative.

**House consumption:** `house_consumption = power_ac - meter power` is the
SolarEdge-side balance. The Hoymiles microinverters feed the house behind the
same grid meter, so it leaves out what they supply. The poller therefore also
writes `house_consumption_total`, which adds the current power of every Hoymiles
microinverter (read from Redis, so `hoymiles-poller` must be running). Hoymiles
values older than `HOYMILES_MAX_AGE` seconds (default 900) are ignored, and
without any fresh Hoymiles data no total is written -- *except* at night (sun
elevation below -3°, from the sun position computed when `LAT`/`LON` are set):
the Hoymiles microinverters power down completely after dark and stop
reporting, which by staleness alone looks exactly like a cloud outage, but at
night they are certainly producing 0 W, so the total keeps recording through
the night instead of going dark for hours. A genuine daytime outage still
shows as missing rather than a false 0.

A house cannot have negative consumption; `power_ac` and the meter reading are
measured separately and can momentarily disagree (same issue as
`power_pv_total` above), so both `house_consumption` and
`house_consumption_total` are clamped at 0 and the unclamped values are kept as
`house_consumption_raw` / `house_consumption_total_raw`.

**Stale data:** if the inverter (or a meter/battery reading, or a Hoymiles device, or the heat
pump) hasn't been read successfully in a while, its "latest" values are removed rather than left
showing an old number as if it were current -- a Stat panel in Grafana that queries a missing hash
field shows "Keine Daten" (a value mapping on an empty string), instead of silently freezing on
whatever was last measured. Configurable per poller: `STALE_AFTER_SECONDS` (SolarEdge, default
120s), `HOYMILES_STALE_AFTER_SECONDS` (default 900s, since cloud data is already a few minutes old
by nature), `WEISHAUPT_STALE_AFTER_SECONDS` (default 120s, only clears the Modbus-read fields --
values derived from manual meter readings don't depend on that link and are left alone). History
graphs already show this as a gap on their own, without any of this.

There is no way to read a genuine whole-house consumption figure over Modbus
here: our SolarEdge meter is a single bidirectional grid meter
(`Export+Import`), not a separate CT clamp on the main feed, so this
subtraction is the only source for it.

The Hoymiles microinverters feed into the house wiring on the AC side, between
the SolarEdge meter and the loads. The meter therefore never sees their output
as generation; it only sees the *remaining* demand after that power has
already been used. So the plain `house_consumption` (and the SolarEdge app's
own "into house" figure, computed the same way, without knowing Hoymiles
exists) isn't an approximation of the whole-house load with a term missing --
it is structurally the *remaining* demand net of the Hoymiles supply, an
unavoidable consequence of the wiring rather than anything the app could
account for. `house_consumption_total` adds the Hoymiles power back in for
exactly that reason: to undo what the wiring already subtracted and recover
the actual whole-house consumption.

## Hoymiles microinverters

Hoymiles' plain "-W"/"-T" HMS microinverters speak a documented local
TCP/protobuf protocol (port 10081) that projects like
[OpenDTU](https://www.opendtu.solar/) and
[hoymiles-wifi](https://github.com/suaveolent/hoymiles-wifi) read directly,
no cloud needed. The **WB-series ("HiFlow Pro", e.g. HMS-1600-4WB)** is
different: it has integrated WiFi/Bluetooth but *no* local API — the only
channel is Bluetooth LE for initial setup, and all data goes through the
Hoymiles S-Miles cloud. There's no free official API for it either, so
`hoymiles-poller/hoymiles_client.py` implements the same (unofficial,
reverse-engineered) login flow the S-Miles app uses, based on the endpoints
documented in the MIT-licensed
[ioBroker.hoymiles](https://github.com/Eistee82/ioBroker.hoymiles) adapter.
A station can mix both kinds (ours does: an HMS-800W-2T and an HMS-1600-4WB),
so everything is read uniformly through the cloud.

Since this data only exists in Hoymiles' cloud otherwise, everything read
is stored locally in Redis with the same retention as the SolarEdge data —
so the history survives even if that cloud service changes or goes away.

Configure via `.env`:

```bash
HOYMILES_USER=you@example.com
HOYMILES_PASSWORD=yourPassword
# Optional "id:name,id:name" -- leave empty to auto-discover every station on the account
HOYMILES_STATIONS=
```

The poller lists the account's stations and each station's microinverters
(device tree), then polls two things every `HOYMILES_POLL_INTERVAL` seconds:

- **per microinverter** (the cloud's realtime "burst" channel): AC power and
  per-PV-string power → `hoymiles:<model>:latest` / `ts:hoymiles:<model>:<field>`
  with `<model>` slugified from the model number, e.g. `hms_800w_2t`.
  Fields: `power_w`, `p1_w` … `p4_w`.
- **per station**: today/month/year/lifetime energy → `hoymiles:<station>:latest`
  / `ts:hoymiles:<station>:<field>`, `<station>` slugified from the station name.
  Fields: `power_w`, `energy_today_wh`, `energy_month_wh`, `energy_year_wh`,
  `energy_total_wh`, `capacity_wp`, `last_data_time`. Hoymiles only reports
  energy at station level, so these are the *combined* figures of all its
  microinverters.

The cloud isn't built for tight polling loops, so the default
`HOYMILES_POLL_INTERVAL` is 300 seconds (5 minutes) — plenty for a
dashboard, and a reasonable citizen towards a service with no documented
rate limits.

## Weishaupt heat pump

`weishaupt-poller` reads a Weishaupt heat pump (Geoblock WGB, and the
compatible WAB/WBB/WSB) over Modbus TCP, using Weishaupt's *Datenpunktliste
Modbus TCP (WWP)*, document 83807301. On the heat pump's system device, turn on
*Settings → Modbus TCP → Access* (port 502, slave address 1). Weishaupt lets you
use either the WEM portal or Modbus TCP, not both. The interface is
unencrypted, so keep the heat pump on a trusted network.

Quirks the poller deals with:

- everything is an *input register* (function code 4), at most 5 consecutive
  registers per request, so reads are grouped into short blocks
- temperatures are signed 16-bit in 0.1 °C; values such as −32768 (no sensor)
  are dropped instead of being logged as temperatures
- there is **no electrical reading at all**, only the requested power in
  percent, and the energy statistics (registers 36xxx, whole kWh) **do not match
  the thermal energy on the display**: on our unit Modbus reports 4560 kWh for the
  year where the display shows 8324, and 0 for day and month where the display has
  values. The poller still logs them (`energy_*_kwh`) for reference, but nothing
  uses them, so the JAZ comes from manual readings instead (below)
- circuits that aren't installed answer with zeros or not at all, hence
  `WEISHAUPT_HEATING_CIRCUITS`

Data lands in `weishaupt:<name>:latest` and `ts:weishaupt:<name>:<field>`
(e.g. `outdoor_temp`, `dhw_temp`, `return_temp`, `power_demand_pct`,
`operating_status`, `hc1_room_setpoint`, `energy_total_year_kwh`). The
dashboard **Wärmepumpe: Weishaupt WGB 14** (`weishaupt-wgb14.json`) shows the
temperatures, the operating status over time and the values from the manual
readings (electricity, thermal energy, JAZ).

**Long-term compaction:** the raw series keep `TS_RETENTION_DAYS` (365) days. For the
outside temperature and the hot-water temperature (`outdoor_temp`, `dhw_temp`) the
poller also keeps daily minimum, mean and maximum forever
(`ts:weishaupt:<name>:<field>:daily_min|avg|max`, UTC days), plotted in the two
"Tageswerte" panels. The first full day appears after the rules are created. The manual
readings and the values derived from them are stored without a time limit as well;
`sync_retention` in both pollers leaves those alone.

### Electricity use and JAZ from manual readings

A seasonal performance factor (JAZ) is thermal energy divided by electricity
consumption. Neither is available over Modbus in a usable form (see above), and
our electricity meter (ORNO OR-WE-520) has only a pulse output. So both are read
off the displays by hand ("sneaker protocol"): the **electricity meter**, and the
heat pump's **thermal energy for the calendar year** (total, i.e. heating plus hot
water).

- **Form:** `https://<GRAFANA_DOMAIN>/meter/` (linked from the dashboard). Enter the
  electricity meter, the thermal year energy and, if you want, the time the reading
  was taken (preset to now, changeable). Caddy only lets logged-in Grafana users
  through, Google login included. Readings that don't fit between their neighbours
  in time are refused (a meter only counts up), which catches most typos; the
  thermal counter may drop only across New Year. Locally the form is on
  `http://127.0.0.1:8000/`, unprotected, loopback only.
- **Command line:** `make reading KWH=12345.6 THERMAL=8324` (`AT="2026-09-21 08:00"`
  for an earlier reading, `LOCAL=1` for the local stack) does the same over SSH.
- **Derived values:** `weishaupt-poller` takes every pair of consecutive readings that
  has both values and derives electricity use, thermal energy, use per day and the
  JAZ, both since the first such reading and for the last interval
  (`jaz_since_first`, `jaz_last_interval`, ...). The year counter restarts on
  1 January, so a value below the previous one counts as a new year; heat made
  between the last reading of a year and midnight is lost, so take a reading around
  New Year. A reading without the thermal value still counts for electricity use
  but not for the JAZ.

The heat pump counts whole kWh, so short intervals are imprecise; a JAZ over
weeks is far more trustworthy. Readings are stored forever
(`ts:weishaupt:<name>:electric_reading_kwh` and `...:thermal_reading_kwh`).

## Dashboards

- **SolarEdge SE10K-RWB48** (`grafana/dashboards/solaredge.json`): total PV
  power (incl. battery-charging share), AC output power, grid power,
  battery power, house consumption, temperature, status, battery charge
  level (current + history), inverter power history, lifetime yield, grid
  frequency, sun position (azimuth/elevation), outside
  temperature/irradiance/cloud cover, and today's running kWh totals.
  Meter/battery panels stay empty if no corresponding devices are connected
  to the inverter.
- **Hoymiles: \<model\>** (`hoymiles-hms-*.json`): one per microinverter —
  its own AC power, the power of each PV string, and a history graph.
- **Hoymiles: Mein Zuhause (Gesamtanlage)** (`hoymiles-mein-zuhause.json`): the
  station as a whole — combined power and today/month/year/lifetime yield.
- **Wärmepumpe: Weishaupt WGB 14** (`weishaupt-wgb14.json`): temperatures,
  requested power, operating status timeline, and electricity, thermal energy and
  JAZ from manual readings.
- **Gesamtübersicht: Alle PV-Systeme** (`grafana/dashboards/overview.json`):
  side-by-side current power and lifetime yield for every system, plus one
  chart overlaying all of their power curves.

All dashboards are provisioned as JSON under `grafana/dashboards/` and
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
| `HOYMILES_USER`            | —                  | S-Miles account email                       |
| `HOYMILES_PASSWORD`        | —                  | S-Miles account password                    |
| `HOYMILES_STATIONS`        | *(empty)*          | `id:name,...` override; empty = auto-discover all stations |
| `HOYMILES_POLL_INTERVAL`   | `300`              | Interval between two cloud polls (seconds)  |
| `WEISHAUPT_HOST`           | *(empty)*          | Heat pump IP; empty = no heat pump, poller idles |
| `WEISHAUPT_POLL_INTERVAL`  | `30`               | Interval between two heat pump polls (seconds) |
| `WEISHAUPT_HEATING_CIRCUITS` | `2`              | Installed heating circuits (1..4)           |
| `WEISHAUPT_NAME`           | `wgb14`            | Redis key component (`weishaupt:<name>:latest`) |

The poller automatically reconnects with exponential backoff if the
inverter is briefly unreachable (e.g. at night in standby, or during
network issues).

### Additional Grafana users (`GRAFANA_USERS`)

Format in `.env`, comma-separated, each entry `login:password:role:email`
(role optional, defaults to `Viewer`; email optional, but required for
[Sign in with Google](#sign-in-with-google) to find the user):

```bash
GRAFANA_USERS=family:aSecurePassword:Viewer,partner:anotherPassword:Editor:partner@example.com
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

### Sign in with Google

Grafana can offer a "Sign in with Google" button next to the password login.
It's prepared in `docker-compose.yml` and stays off until you enable it in `.env`:

1. In the [Google Cloud Console](https://console.cloud.google.com/apis/credentials),
   create an *OAuth client ID* of type **Web application** and add
   `<GRAFANA_ROOT_URL>login/google` as an authorized redirect URI.
2. Put the values into `.env`:

   ```bash
   GRAFANA_ROOT_URL=http://localhost:3000/
   GOOGLE_OAUTH_ENABLED=true
   GOOGLE_CLIENT_ID=1234567890-abc.apps.googleusercontent.com
   GOOGLE_CLIENT_SECRET=...
   ```

3. Give each person who may log in a Grafana user *with their Google email*
   (`login:password:role:email` in `GRAFANA_USERS`), then `docker compose up -d`.

By default (`GOOGLE_ALLOW_SIGN_UP=false`) only Grafana users that already exist,
matched by email, can log in with Google; nobody else gets an account
automatically. `GOOGLE_ALLOWED_DOMAINS` can additionally restrict to Google
Workspace domains — never set it to `gmail.com`, that would admit every Gmail user.

**First login of a new user:** Grafana links a Google account to a Grafana user
by Google's stable user ID, and users created by `grafana-init` have no such link
yet. A user's very first Google login is therefore refused with "Sign up is
disabled" unless you temporarily set `GF_AUTH_OAUTH_ALLOW_INSECURE_EMAIL_LOOKUP: "true"`
on the `grafana` service (it makes Grafana fall back to the email once). That's
safe here because Google verifies emails and sign-up stays off; remove it again
after the new user has logged in once.

**Redirect URI restriction:** Google only accepts `https://` redirect URIs on a
real public domain, or `http://localhost`. A LAN address like
`http://192.168.0.2:3000` or `http://solar.lan` is rejected. So this works out of
the box on the machine running Docker (`http://localhost:3000`, also through an
SSH tunnel: `ssh -L 3000:localhost:3000 <host>`). For direct LAN access you need a
public domain name resolving to the host (e.g. via local DNS) with a valid
HTTPS certificate in front of Grafana.

### HTTPS reverse proxy (Caddy)

Google login needs `https://` on a public-TLD hostname (see above), and
short names without `:3000` need something on port 80. The optional `caddy`
service (compose profile `proxy`) provides both:

- `https://<GRAFANA_DOMAIN>/` → Grafana, with a Let's Encrypt certificate
- `http://solar`, `http://solar.lan`, `http://dashboard.lan` → Grafana
- every other request on port 80 (e.g. `http://pihole/admin`) → the Pi-hole web UI
- `https://<GRAFANA_DOMAIN>/meter/` → the meter reading form, only for logged-in Grafana users

The hostname only has to *look* right to Google (public TLD); it doesn't have
to be reachable from the internet. Point it at the Docker host with a local DNS
record (e.g. in Pi-hole: `smart.example.com → 192.168.0.2`). The certificate is
obtained via the DNS-01 challenge, delegated through
[ACME-DNS](https://github.com/joohoi/acme-dns), so no DNS-provider credentials
are stored — useful for providers without an API (e.g. Strato).

Setup:

1. Register an ACME-DNS account and note the response:

   ```bash
   curl -X POST https://auth.acme-dns.io/register
   ```

2. At your DNS provider, create a CNAME
   `_acme-challenge.<GRAFANA_DOMAIN>` → the `fulldomain` from that response.
3. In `.env`: `GRAFANA_DOMAIN`, `ACMEDNS_USERNAME`, `ACMEDNS_PASSWORD`,
   `ACMEDNS_SUBDOMAIN`, `GRAFANA_ROOT_URL=https://<GRAFANA_DOMAIN>/`, and
   `COMPOSE_PROFILES=proxy`.
4. **On a host that runs Pi-hole**, it occupies ports 80/443, so move its web
   UI first (the DNS server keeps running, but FTL restarts briefly):

   ```bash
   sudo pihole-FTL --config webserver.port '8080o,8443os,[::]:8080o,[::]:8443os'
   sudo systemctl restart pihole-FTL
   ```

5. `docker compose up -d` (or `make deploy`).

The Pi-hole UI stays reachable through the proxy over plain HTTP
(`http://pihole/admin`); its own HTTPS listener is now on port 8443.

### Home-screen app (iPhone / iPad)

Behind the HTTPS name the dashboards can be installed as an app: open
`https://<GRAFANA_DOMAIN>/d/pv-overview` in Safari, then *Share → Add to Home Screen*.
It opens in its own window without the browser bar, with a sun icon and the name
"Energie". Grafana already declares itself standalone-capable to iOS; Caddy only
rewrites the HTML it serves (`replace-response` plugin, built into the `caddy` image) to
swap Grafana's icon for ours and to add a manifest (`caddy/pwa/manifest.webmanifest`,
start page and name) and the app title. The files in `caddy/pwa/` are served without a
login; the icons come from `scripts/make-pwa-icons.py`. There is no service worker:
the data is live, so offline mode would show nothing useful.

The start page and name are in the manifest (`start_url`, `name`); a link with
`?kiosk` hides Grafana's menus. Note that the installed app has its own session on iOS,
so you log in once inside it. Whether Google's login works inside an installed app is up to
Google (it refuses embedded webviews); the password login always works.

## macOS app (PV Monitor)

A small native window for a Mac with Apple Silicon (arm64 only, macOS 13+, Swift, no
dependencies) that shows the summed production of the three PV systems (SolarEdge plus the two
Hoymiles microinverters) and the house consumption. The house consumption figure turns **red while
its 5-minute mean is above the production's 5-minute mean**; the big figures are the current
values, the table shows current value and 5-minute mean per system. "Immer im Vordergrund" (also in
the View menu, Cmd-T) keeps the window above all others, including across Spaces.

- **Data:** it reads the last 15 minutes of `ts:inverter:power_pv_total`,
  `ts:hoymiles:<model>:power_w` and `ts:inverter:house_consumption_total` through Grafana's query
  API every 10 seconds, so all it needs is the HTTPS address and a read-only token; no Redis or Pi
  access. It works wherever `GRAFANA_ROOT_URL` resolves (at home, or over VPN).
- **Averages:** the mean of the samples in the last 5 minutes; a series without a sample in that
  window (the Hoymiles cloud data arrives every 5 minutes) uses its newest sample if it is at most
  15 minutes old, otherwise it counts as unknown. With an unknown production figure the total is
  marked `*` and the colour rule is suspended, since a missing system would make the production
  look too low and the consumption falsely red.
- **Setup:** from `macos-app/` (or `make -C macos-app <target>`):

  ```bash
  make install    # gets a read-only Grafana token (if none yet), builds the app, copies it to /Applications
  ```

  The token comes from a read-only Grafana service account (`pv-monitor`, role Viewer) that
  `make token` creates using `GRAFANA_ADMIN_PASSWORD` from `.env`; the app's address is
  `GRAFANA_ROOT_URL`. It is written to `~/Library/Application Support/PV Monitor/config.json`
  (`{"url": ..., "token": ...}`, mode 600). `make install` only fetches a token if that file is
  missing, so repeated installs don't pile tokens up (`make install NEW_TOKEN=1` forces a new one;
  old tokens stay valid until deleted in Grafana). The file can also be written by hand, and the
  environment variables `PV_URL` and `PV_TOKEN` override it. Other targets: `make build`,
  `make test`, `make run`, `make clean`. The app is signed ad hoc, which is enough for the Mac it
  was built on; elsewhere, right-click > Open once.
- **Testing without a GUI:** `PVMonitor --selftest` checks the averaging and the colour rule,
  `PVMonitor --print` fetches once and prints the figures, `PVMonitor --snapshot out.png [red]`
  renders the window with sample data to a PNG.

## Compute machines on PV surplus (PrimeGrid)

`compute-controller` decides when machines that run PrimeGrid should be on, so that they compute
on solar surplus and don't draw from the battery. **Dry run only for now:** it records what it
*would* switch (`compute:events`, dashboard **Rechner-Steuerung (Probelauf)**); nothing is switched
until the actuation part (Shelly plugs, shutdown over SSH) exists. Configure the machines in `.env`
as `COMPUTE_MACHINES=winola:200,gamer:250,imac:100` (`name:watts` under full load, in priority
order: the first is started first and stopped last). The watts are estimates until the plugs
measure them. Without `COMPUTE_MACHINES` the service idles.

Every minute it works out the **headroom** (production of all three PV systems minus house
consumption *without* the machines, 15-minute means: what would otherwise be exported or charge the
battery) and a **forecast** for the next hour. The forecast is the measured production scaled by
how Open-Meteo's hourly irradiance for the next hour compares with the last hour; converting
irradiance straight to watts was too inaccurate here (the learned factor is only the fallback near
sunrise and sunset). Then, per machine, cumulative load `L` (this machine plus the ones before it):

- **on** if the headroom is at least `L` + `COMPUTE_MARGIN_ON_W`, the forecast covers `L`, and the
  battery is at least `COMPUTE_SOC_ON` % (default 80)
- **off** if the headroom falls below `L` − `COMPUTE_TOLERANCE_OFF_W`, or the forecast does while
  the battery is below `COMPUTE_SOC_FC_OFF` % (battery use is to be feared), or the battery drops
  below `COMPUTE_SOC_MIN` % (immediately)
- a change only applies after its condition has held for `COMPUTE_ON_DELAY_MIN` /
  `COMPUTE_OFF_DELAY_MIN` (15 / 20 minutes), and a machine keeps `COMPUTE_MIN_ON_MIN` /
  `COMPUTE_MIN_OFF_MIN` (45 / 45) minutes of run time or pause. Without production or consumption
  data it keeps the current state.

The defaults come from `compute-controller/backtest.py`, which replays these rules over the stored
history (`docker compose run --rm --no-deps compute-controller python backtest.py [days]`):
without the delays the machines would have switched about 15 times a day, with them about 4-5,
at the price of a share of the machines' energy coming from battery or grid instead of surplus
(16 % over the first four days, without the forecast rule, which has no history to replay). The
machines' own consumption is not in the history; while it is unknown the controller counts it as
0 W, so if a machine actually runs, the true surplus is larger than shown.

`python controller.py --selftest` (in the container) checks the rules, the forecast and the delays.
