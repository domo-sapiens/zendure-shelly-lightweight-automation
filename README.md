# Zendure ↔ Shelly lightweight automation

Zero-feed-in control for a Zendure SolarFlow 800 Plus, driven by a script that
runs **on the Shelly Pro 3EM itself**. No cloud, no MQTT broker, no Home
Assistant. See [the research notes](zendure-shelly-direct-shelly-script.md) for
where this approach comes from, and [the HA alternative](zendure-shelly-home-assistant-path.md)
for the path not taken.

## How it works

Every `intervalMs`, the script on the Shelly:

1. `GET http://<zendure>/properties/report` — SoC, output limit, actual output
2. reads its own energy meter locally (no network call)
3. computes a new `outputLimit` so grid power lands on `targetGridW`
4. `POST http://<zendure>/properties/write` — but only if the value moved enough
   to be worth writing

## Layout

| Path | What |
| --- | --- |
| `src/zendure-control.js` | The control loop. Config is a `__CONFIG_JSON__` placeholder. |
| `config/config.example.json` | Template, committed. |
| `config/config.local.json` | **Your real values. Gitignored.** |
| `tools/deploy.py` | Renders config into the script and uploads it over the Shelly RPC API. |
| `tools/collector.py` | Polls both devices and stores a time series in SQLite. Runs on the Pi. |
| `tools/dashboard.py` | Read-only web UI over that database. Separate process from the collector. |
| `web/index.html` | The dashboard page. Charts hand-drawn on canvas, no dependencies. |
| `tools/discover.sh` | Dumps both devices' state into `notes/` so you can read off the real values. |
| `build/` | Generated script with config baked in. Gitignored. |
| `notes/` | Device dumps. Contain serials/MACs, so `notes/*.json` is gitignored. |

**Where secrets live:** nothing sensitive is in git. IPs, the Zendure serial and
the Shelly password live only in `config/config.local.json` on this machine. The
serial is not even in the config — the script reads it back from
`/properties/report` at runtime and echoes it into the write payload.

## Setup

```bash
cp config/config.example.json config/config.local.json
```

Fill in the two IPs, then confirm both devices answer:

```bash
tools/discover.sh <shelly-ip> <zendure-ip>
```

Deploy:

```bash
tools/deploy.py
```

Watch it run:

```bash
tools/deploy.py --logs
```

Stop it (also disables autostart-on-boot):

```bash
tools/deploy.py --stop
```

## Config reference

### `meter`

- **`profile`** — `"triphase"` reads `em:0.total_act_power`; `"monophase"` sums
  `em1:0/1/2.act_power`. The Pro 3EM exposes one or the other depending on how
  it is configured. `tools/discover.sh` prints which one you have.
- **`importPositive`** — `true` if the meter reports positive watts when you are
  *drawing* from the grid. If the sign is flipped, the loop runs away in the
  wrong direction, so verify this before trusting it.

### `control`

- **`targetGridW`** — where you want grid power to sit. `0` is correct when the
  battery is solar-charged and export is uncompensated: importing and exporting
  then cost exactly the same, so no bias is justified. A positive value is only
  right if stored energy costs more than grid energy — e.g. a grid-charged
  battery on a flat tariff, where round-trip losses make discharging to cover
  baseload a net loss. See [docs/assumptions.md](docs/assumptions.md).
- **`deadbandW`** — errors smaller than this are ignored. Read this together
  with the target: a deadband of 15 around a target of 0 means the loop is
  content anywhere in ±15 W, so **widening the band is equivalent to raising the
  target**. Narrowing it costs more writes to the Zendure.
- **`gain`** — how much of the error to correct per cycle. `1.0` is deadbeat and
  oscillates in practice; `0.8` settles in a couple of cycles.
- **`maxStepW`** — cap on setpoint movement per cycle, so a kettle switching on
  doesn't slam the inverter from 0 to 800 W.
- **`minWriteDeltaW`** — skip the POST when the new setpoint barely differs from
  the current one. Reduces writes to the Zendure by a lot.
- **`basis`** — `"actual"` regulates from `outputHomePower` (what the battery is
  really delivering); `"limit"` regulates from `outputLimit` and reproduces the
  original forum script. `"actual"` avoids the setpoint winding up to 800 W when
  the battery is too empty to follow it.
- **`busyTimeoutCycles`** — watchdog. If an HTTP callback never fires, the cycle
  is abandoned after this many ticks and a fresh one starts. Without it a single
  lost callback stops the loop regulating *silently*, with no error anywhere.
- **`healthEveryCycles`** — how often to print the counters line (poll errors,
  write errors, watchdog trips). `120` at a 5 s interval is every 10 minutes.
  Set `0` to disable.

### `battery`

- **`reserveSoc`** — at or below this SoC, output is forced to 0. Enforced before
  the deadband and before the `minWriteDeltaW` economy, so neither can let a
  discharge slip through.
- **`resumeSoc`** — discharge only resumes once SoC has climbed back to here. The
  gap matters: with output at 0 the pack voltage relaxes and the reported SoC
  ticks up on its own, so a single threshold would re-enable discharge
  immediately and cycle the battery at its floor — exactly the wear the reserve
  exists to prevent.
- **`hysteresis`** — off by default, and unrelated to the reserve above. Moving
  the device's own `minSoc` to force charge/discharge switching was
  [reported broken on Zendure firmware ≥ 1.0.23](zendure-shelly-direct-shelly-script.md)
  (rapid toggling between 10–20 % SoC). The reserve gate needs no cooperation
  from the Zendure and is not affected by that bug.

Defaults are 15 % / 20 %, which keeps the battery clear of 10 % with margin for
SoC estimation drift. The Zendure's own `minSoc` (10 % out of the box) stays
untouched underneath as an independent backstop.

## Logging and dashboard

A Raspberry Pi polls both devices independently and stores the series; see
[docs/pi-setup.md](docs/pi-setup.md). The control loop is never in that data
path, so the logger cannot disturb regulation.

The dashboard shows grid power against the deadband, a tracking-error panel that
separates *regulation error* from *saturation gap*, state of charge, and inverter
efficiency binned by output power.

Every tuning decision and the evidence behind it is in
[docs/assumptions.md](docs/assumptions.md), which separates what was measured
from what was merely assumed.

## Testing

The control logic runs against a simulated Shelly environment — no hardware
needed. Covers the reserve gate, the watchdog, late callbacks from abandoned
cycles, and normal regulation:

```bash
python3 tools/deploy.py --build-only && node tools/simulate.js
```

## Prerequisite on the Zendure side

The Zendure must **not** be assigned to a HEMS integration in the Zendure app. A
HEMS continuously overwrites externally-written setpoints and this loop will
fight it.
