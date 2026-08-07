# Plan: logging collector, storage and dashboard

Architecture **B** from the concept discussion: an external collector polls both
devices independently and stores a time series. The control script on the Shelly
is never in the data path and is unaffected if the collector dies.

## 1. Host

**Raspberry Pi 3 B+, wired ethernet, running Raspberry Pi OS Lite (64-bit).**

Why Pi OS Lite over the alternatives: Python 3.11+ is in the base image and the
collector needs nothing beyond the standard library, so there is no package
management to maintain. DietPi is genuinely lighter but saves ~100 MB of RAM on a
machine with 1 GB, against a process that will use about 40 MB — the saving buys
nothing and costs familiarity. Ubuntu Server is heavier for no benefit here.
Alpine means a different toolchain for no gain.

64-bit rather than 32-bit: the 3 B+ is ARMv8, and the marginal memory overhead is
irrelevant at this workload.

Flash with Raspberry Pi Imager and use its settings dialog to preconfigure
hostname, your SSH public key, locale and timezone, so the Pi is headless from
first boot. Ethernet needs no configuration.

Setup steps once booted:

- DHCP reservation on the Fritz!Box, consistent with the Shelly and Zendure.
- `sudo apt update && sudo apt full-upgrade`, then install nothing further.
  uPlot ships as a single vendored JS file in the repo; there is no npm step.
- systemd unit for the collector with `Restart=always` and
  `RestartSec=10`, so it survives crashes and reboots.
- Confirm `systemd-timesyncd` is running. Every row is timestamped and a Pi with
  no RTC boots at epoch zero until NTP lands — samples written before sync would
  otherwise land in 1970.

### SD card wear

Worth handling, not worth panicking about. Raw volume is ~3.5 MB/day, which is
nothing; the concern is write amplification, since every commit dirties a page
plus WAL. Committing per sample would mean roughly 200 MB/day of actual flash
writes — still only a few percent of a cheap card's endurance per decade, but
easy to reduce tenfold:

- SQLite in WAL mode.
- **Batch commits**: buffer samples in memory, commit every 60 s (12 samples).
  A crash loses at most a minute of data, which does not matter for this.
- `/tmp` on tmpfs, journald size-capped or volatile, swap disabled.

A high-endurance card (SanDisk Max Endurance, Samsung PRO Endurance) is cheap
insurance but not a requirement given the above.

## 2. Resilience to a lossy link

The Shelly's wifi is weak, so **dropped requests are the normal case, not the
exception.** Two independent problems.

### 2a. The control script on the Shelly

Current state: a failed poll logs and skips the cycle. That is correct but
incomplete. Three gaps to close:

- **`state.busy` deadlock.** If a `Shelly.call` never invokes its callback — the
  exact thing a flaky link causes — `busy` stays `true` forever and the control
  loop silently stops regulating, with no error, until someone notices. Needs a
  watchdog: record the tick number when `busy` was set and force-clear it after
  N cycles. This is the highest-severity item in the whole plan, because it fails
  silently.
- **Stale setpoint during an outage.** Be clear-eyed about this one: if the
  Shelly cannot reach the Zendure, the POST fails for the same reason the GET
  did, so the script *cannot* correct anything. The Zendure holds its last
  setpoint. If that was 700 W and household load then collapses, you export until
  the link returns. There is no code fix on the Shelly for this. What we can do
  is bound the blast radius — `maxStepW` already prevents absurd setpoints — and
  recompute immediately on recovery. Whether this matters at all is an empirical
  question, which is why the next item exists.
- **Measure the outage rate.** Count consecutive and cumulative failures and
  expose them, so we learn how often and how long the link actually drops. If it
  turns out to be frequent or long, the fix is the RF path or relocating the
  control script — not more error handling.

### 2b. The collector

- **Poll each device independently, and never discard a good half.** If the
  Shelly times out but the Zendure answers, write the row with the Shelly columns
  null. Losing one device must not lose the other's sample.
- **Record failures as data.** Every row carries `shelly_ok` / `zendure_ok`
  flags. A gap should be a visible, queryable fact, not an absence of rows — you
  cannot distinguish "collector was down" from "nothing happened" otherwise.
- **Short timeouts (2–3 s), one quick retry, then give up for that tick.** Never
  queue a backlog; a late sample is worth less than an on-time next one.
- **Monotonic scheduling** so slow polls do not let the cadence drift.
- **Charts must break lines across gaps**, never interpolate across them. uPlot
  renders nulls as breaks natively.

### 2c. One thing not to misread on the charts

The collector reads the meter *over wifi*; the control script reads the same
meter *locally, over the internal bus*. So a gap in logged grid power does **not**
mean the control loop was blind at that moment — the loop almost certainly kept
regulating fine. The collector's gaps measure the collector's link, not the
loop's health. Worth remembering before drawing conclusions from a hole in a
chart.

### 2d. Load on the Zendure

The Zendure will now be polled by both the Shelly script and the Pi, doubling
requests against its small embedded HTTP server. Start the collector at 5 s to
match the control cadence; if the Zendure gets flaky or slow, drop the collector
to 10 s. Its own poll rate is not what the tuning depends on.

## 3. Storage

SQLite, WAL mode, one file.

Per sample: timestamp, `shelly_ok`, `zendure_ok`, grid `total_act_power` plus the
three per-phase powers (cheap, and useful given the balancing meter), and from
the Zendure `electricLevel`, `outputLimit`, `outputHomePower`, `packInputPower`,
`solarInputPower`, `gridInputPower`, `acMode`, `minSoc`, `socSet`.

Everything else is derived at query time, so no schema change is needed to add a
new chart later.

**Retention**: raw at 5 s for 14 days, then rolled into 1-minute averages kept
indefinitely. Raw-forever would be ~1.1 GB/year; the rollup is ~100 MB/year and
loses nothing anyone would use, since no one tunes a loop against five-second
data from four months ago.

## 4. Charts

### The delta panel

Your instinct is right, and the target band does *not* already cover it. When
grid power swings ±500 W, a 20 W target band is a hairline — the information is
technically on screen and practically invisible. A dedicated delta plot on its
own auto-scaled axis makes bias, oscillation and asymmetry legible.

But "the delta between actual power use and what the battery was able to
provide" is really **two different quantities**, and separating them is the whole
diagnostic value:

- **Regulation error** = `grid_power − targetGridW`. How well the loop holds its
  setpoint. This is what tuning `gain` and `deadbandW` moves.
- **Saturation gap** = `outputLimit − outputHomePower`. What we *asked* the
  battery for versus what it actually delivered. Non-zero means the battery could
  not comply — empty, at the 800 W ceiling, thermally limited, or still ramping.

They have different causes and different fixes. A mistuned gain shows up as
regulation error oscillating rhythmically through zero. A saturated battery shows
up as regulation error sitting on a floor it cannot cross, with the saturation
gap simultaneously non-zero. **Plot only one and you cannot tell "my gain is
wrong" from "my battery is flat"** — which are opposite responses.

So: one panel, both traces, with saturated periods shaded. And you are right that
it is all derived from raw columns, so this costs nothing at collection time.

This also gives the honest headline KPI: **regulation error excluding saturated
periods**. During saturation the error is unavoidable and says nothing about
tuning, so including it just makes the loop look worse than it is.

### The rest

- **Main chart**: grid power with the target band, `outputLimit` and
  `outputHomePower` overlaid. Setpoint windup and overshoot are directly visible.
- **SoC** over time, with the cutoff threshold marked.
- **KPIs**: percent of time inside the deadband (saturation excluded), writes per
  hour, kWh imported, and kWh exported — the feed-in we failed to prevent, which
  is the number this project exists to minimise.
- **Link health**: poll success rate per device, so 2a's outage question has an
  answer.

### Serving it

One process, two threads: collector writing, HTTP server reading. Single static
HTML page, no build step. Live view polls `/api/series?since=<ts>` every 5 s for
a few hundred bytes per update; SSE would be more elegant and is not worth the
complexity at 0.2 Hz. History view reads the 1-minute rollup behind a range
picker.

Bind to the LAN so you can reach it from a phone, and understand that it is
unauthenticated and read-only — LAN only, never port-forwarded.

## 5. Order of work

1. **Fix the `state.busy` watchdog** in the control script. Silent failure mode,
   fixed before anything runs unattended.
2. **Collector + SQLite + retention**, running on the Pi, *before* deploying the
   control script — so we capture a baseline of the current idle behaviour to
   measure the improvement against.
3. **Deploy the control script**, watch via `deploy.py --logs` for the first hour.
4. **API + live chart.**
5. **History view + rollups + delta panel.**
6. **Tune** `gain`, `deadbandW` and `targetGridW` against real curves.
