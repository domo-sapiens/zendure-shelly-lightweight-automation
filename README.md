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

- **`targetGridW`** — where you want grid power to sit. A small positive value
  (~20 W) keeps you just barely importing, which is the safe side of zero
  feed-in. `0` invites brief export on every load step.
- **`gain`** — how much of the error to correct per cycle. `1.0` is deadbeat and
  oscillates in practice; `0.8` settles in a couple of cycles.
- **`deadbandW`** — errors smaller than this are ignored entirely, so household
  noise doesn't cause constant setpoint churn.
- **`maxStepW`** — cap on setpoint movement per cycle, so a kettle switching on
  doesn't slam the inverter from 0 to 800 W.
- **`minWriteDeltaW`** — skip the POST when the new setpoint barely differs from
  the current one. Reduces writes to the Zendure by a lot.
- **`basis`** — `"actual"` regulates from `outputHomePower` (what the battery is
  really delivering); `"limit"` regulates from `outputLimit` and reproduces the
  original forum script. `"actual"` avoids the setpoint winding up to 800 W when
  the battery is too empty to follow it.

### `battery`

- **`cutoffSoc`** — below this SoC, output is forced to 0.
- **`hysteresis`** — off by default. Moving `minSoc` to force charge/discharge
  switching was [reported broken on Zendure firmware ≥ 1.0.23](zendure-shelly-direct-shelly-script.md)
  (rapid toggling between 10–20 % SoC). Only enable it if you are on older
  firmware or have verified the behaviour yourself.

## Prerequisite on the Zendure side

The Zendure must **not** be assigned to a HEMS integration in the Zendure app. A
HEMS continuously overwrites externally-written setpoints and this loop will
fight it.
