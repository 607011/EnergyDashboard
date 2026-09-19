# SE10K Modbus → Redis → Grafana

Fragt den SolarEdge SE10K-RWB48 lokal per Modbus TCP (SunSpec-Profil) ab,
schreibt die Werte nach Redis und zeigt sie in einem Grafana-Dashboard —
komplett ohne SolarEdge-Cloud.

Es werden automatisch erkannt und geloggt:
- der Wechselrichter selbst
- angeschlossene Energy Meter (sofern vorhanden)
- angeschlossene Batterien (sofern vorhanden, z. B. SolarEdge Home Battery)

## Setup

1. Am Wechselrichter unter *Communication → Modbus TCP* muss "Enabled" gesetzt sein
   (ist bei dir bereits der Fall, IP `192.168.0.174`).
2. `.env` aus der Vorlage anlegen und bei Bedarf anpassen:

   ```bash
   cp .env.example .env
   ```

3. Starten:

   ```bash
   docker compose up -d --build
   ```

4. Logs prüfen:

   ```bash
   docker compose logs -f poller
   ```

5. Dashboard öffnen: [http://localhost:3000](http://localhost:3000) — auch von anderen
   Geräten im WLAN unter `http://<IP-des-Docker-Hosts>:3000` erreichbar.
   (Login: `admin` / das in `.env` gesetzte `GRAFANA_ADMIN_PASSWORD`, Default `admin`).
   Das Dashboard "SolarEdge SE10K-RWB48" ist automatisch vorhanden (Provisioning).

## Architektur

```
SolarEdge SE10K  --Modbus TCP-->  poller (Python)  --> Redis (redis-stack)  <-- Grafana
```

- **poller**: pollt alle `POLL_INTERVAL` Sekunden alle Register, faltet SunSpec
  Value/Scale-Registerpaare zu fertigen Zahlen zusammen und schreibt sie nach Redis.
- **redis** (Image `redis/redis-stack-server`): normales Redis *plus* das
  RedisTimeSeries-Modul für die Verlaufsdaten.
- **grafana**: mit dem [Redis-Datasource-Plugin](https://github.com/RedisGrafana/grafana-redis-datasource),
  Datasource und Dashboard sind vorprovisioniert (`grafana/provisioning/`,
  `grafana/dashboards/solaredge.json`). Port `3000` ist auf allen Interfaces
  gebunden, also aus dem ganzen WLAN erreichbar.
- **grafana-init**: einmaliger Hilfscontainer, der nach jedem Start per
  Grafana-HTTP-API die in `GRAFANA_USERS` (`.env`) hinterlegten Zusatz-Nutzer
  anlegt bzw. deren Passwort/Rolle aktualisiert, dann beendet er sich.

## Datenmodell in Redis

Pro Gerät gibt es zwei Arten von Keys:

- `solaredge:<device>:latest` — Hash mit den aktuellsten Werten (z. B. `power_ac`,
  `energy_total`, `status_label`, `temperature`, `updated_at`, ...) — für Stat-/Gauge-Panels
- `ts:<device>:<feld>` — ein RedisTimeSeries-Key pro numerischem Feld, z. B.
  `ts:inverter:power_ac` — für Verlaufsgraphen in Grafana

`<device>` ist `inverter`, `meter:meter1`, `battery:battery1` usw.

Beispiele:

```bash
# aktuelle Werte des Wechselrichters
redis-cli HGETALL solaredge:inverter:latest

# Leistungsverlauf der letzten Stunde
redis-cli TS.RANGE ts:inverter:power_ac $(($(date +%s%3N)-3600000)) +
```

Die History-Länge wird über `TS_RETENTION_DAYS` begrenzt (Default: 365 Tage),
ältere Samples fallen automatisch raus (RedisTimeSeries-Retention). Ändert
sich der Wert, gleicht der Poller beim nächsten Start die Retention aller
bereits bestehenden Serien automatisch an (`TS.ADD` selbst ändert sie bei
existierenden Keys nicht, siehe `sync_retention()` in `poller/poller.py`).

### Langfristige Statistik (Compaction)

Damit mehrjährige Auswertungen nicht am Speicherplatz scheitern, legt der
Poller zusätzlich drei **unbegrenzt aufbewahrte** Tages-Rollups an
(RedisTimeSeries-Compaction-Rules, `TS.CREATERULE`):

- `ts:inverter:energy_total:daily` — Tages-Endstand des Lifetime-Zählers (`LAST`)
- `ts:inverter:power_pv_total:daily_avg` — Tagesdurchschnitt der PV-Leistung
- `ts:inverter:power_pv_total:daily_max` — Tagesspitze der PV-Leistung

Diese drei Reihen wachsen nur um ~365 Punkte pro Jahr und können daher für
immer aufbewahrt werden. Für die Produktion pro Monat eignet sich
`energy_total:daily` am besten, da es sich um einen monoton steigenden
Zähler handelt — die Monatsproduktion ist einfach die Differenz von End- und
Anfangswert, ganz ohne Rundungsfehler durch Mittelwertbildung:

```bash
# Endstand am Monatsende minus Endstand am Vormonatsende = Produktion des Monats (Wh)
redis-cli TS.RANGE ts:inverter:energy_total:daily <von_ts_ms> <bis_ts_ms>
```

Weitere Rollups lassen sich nach demselben Muster in `COMPACTION_RULES`
(`poller/poller.py`) ergänzen.

## Wichtige Felder

**Inverter:** `power_ac` (W), `power_dc` (W), `energy_total` (Wh, kumulativ),
`temperature` (°C), `frequency` (Hz), `status` / `status_label`
(Off/Sleeping/Producing/Fault/...), AC-Spannung/-Strom je Phase
(`l1_voltage`, `l1_current`, ...)

**Meter** (falls vorhanden): `power` (W, positiv = Einspeisung ins Netz,
negativ = Bezug aus dem Netz — anhand eines Vergleichs mit der SolarEdge-App
verifiziert), `export_energy_active`, `import_energy_active`

**Battery** (falls vorhanden): `soe` (State of Energy / Ladezustand in %),
`instantaneous_power` (positiv = Laden, negativ = Entladen), `status` /
`status_label`, `available_energy`

**Wichtiger Sonderfall bei DC-gekoppelter Batterie (Hybrid-Wechselrichter wie
der SE10K-RWB48):** Die Batterie hängt am selben DC-Bus wie die Panels, aber
*vor* der eigentlichen DC/AC-Umwandlungsstufe des Wechselrichters. Während die
Batterie lädt, zeigt `power_dc` deshalb nur den Rest, der zur AC-Umwandlung
übrig bleibt — nicht die gesamte Panel-Erzeugung. Der Poller berechnet daher
zusätzlich `power_pv_total = power_dc + Batterieladeleistung` (nur solange
`status_label` der Batterie `Charge` ist) als bestmögliche Näherung an die
tatsächliche Gesamtleistung der Panels, so wie sie auch die SolarEdge-App
als "aktuelle Sonnenenergie" anzeigt.

## Dashboard

Enthält: PV-Gesamtleistung (inkl. Batterieladeanteil), AC-Ausgangsleistung,
Netzleistung, Batterieleistung, Temperatur, Status, Batterie-Ladezustand
(aktuell + Verlauf), Wechselrichter-Leistungsverlauf, Gesamtertrag (Lifetime)
und Netzfrequenz. Panels für Meter/Batterie bleiben leer, falls keine
entsprechenden Geräte am Wechselrichter angeschlossen sind.

Das Dashboard liegt als JSON unter `grafana/dashboards/solaredge.json` und
wird beim Start automatisch geladen (Grafana-Provisioning); Änderungen in der
Grafana-UI lassen sich über "Export → Save JSON" wieder dorthin zurückspeichern.

## Konfiguration (`.env`)

| Variable                  | Default            | Bedeutung                                |
|----------------------------|--------------------|-------------------------------------------|
| `INVERTER_HOST`            | `192.168.0.174`    | IP des Wechselrichters                    |
| `INVERTER_PORT`            | `502`              | Modbus-TCP-Port                           |
| `MODBUS_UNIT`              | `1`                | Modbus Unit/Slave-ID                      |
| `MODBUS_TIMEOUT`           | `5`                | Timeout pro Leseversuch (Sekunden)        |
| `POLL_INTERVAL`            | `10`               | Abstand zwischen zwei Abfragen (Sekunden) |
| `TS_RETENTION_DAYS`        | `365`               | Aufbewahrungsdauer der Rohdaten-Verlaufsreihen |
| `GRAFANA_ADMIN_PASSWORD`   | `admin`            | Grafana-Login beim ersten Start           |
| `GRAFANA_USERS`            | *(leer)*           | Weitere Grafana-Nutzer, siehe unten       |

Der Poller reconnected automatisch mit exponentiellem Backoff, falls der
Wechselrichter kurzzeitig nicht erreichbar ist (z. B. nachts im Standby oder
bei Netzwerkproblemen).

### Weitere Grafana-Nutzer (`GRAFANA_USERS`)

Format in `.env`, kommagetrennt, je Eintrag `login:passwort:rolle`
(Rolle optional, Default `Viewer`):

```bash
GRAFANA_USERS=familie:einSicheresPasswort:Viewer,partner:anderesPasswort:Editor
```

Bei jedem `docker compose up` legt der `grafana-init`-Container diese Nutzer
über die Grafana-API an bzw. gleicht Passwort und Rolle ab, falls sie schon
existieren — kein manuelles Anlegen in der UI nötig. Rollen: `Viewer`
(nur ansehen), `Editor` (Dashboards bearbeiten), `Admin` (volle Rechte).

**Wichtig:** `GRAFANA_ADMIN_PASSWORD` wirkt nur beim allerersten Start (siehe
oben) — `GRAFANA_USERS` dagegen wird bei *jedem* Start erneut angewendet,
da es über die laufende API provisioniert und nicht nur beim Erststart gesetzt
wird. Ein geändertes Passwort in `.env` für einen bestehenden Nutzer wird beim
nächsten `docker compose up` also übernommen.
