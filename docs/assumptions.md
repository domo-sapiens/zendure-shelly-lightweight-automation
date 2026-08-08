# Assumptions and measured facts

Everything the tuning depends on. Split deliberately into **measured** (we
tested it, with the result) and **assumed** (someone asserted it, or it is a
default nobody has challenged). Assumptions are where wrong behaviour comes
from, so they are listed to be argued with.

Last reviewed: 2026-08-08.

---

## 1. Economics

| # | Statement | Status | Source |
| --- | --- | --- | --- |
| E1 | Flat electricity tariff, same price at all hours | **stated by owner** | 2026-08-08 |
| E2 | Import price ≈ **€0.35/kWh** | **ASSUMED — please correct** | my placeholder, never confirmed |
| E3 | Exported energy earns **nothing** | assumed | typical for an unregistered plug-in solar device in DE |
| E4 | The battery is charged **only** from solar, never from the grid | **stated by owner** | 2026-08-08 |

### What follows from these

Because of E4, stored energy is **free at the margin** — it cost nothing to
put in. So any watt imported from the grid that the battery *could* have
supplied is pure avoidable cost. At E2, **1 W of continuous bias ≈ €3/year**
(1 W × 8760 h × €0.35/kWh). That is the entire argument for driving the target
to zero.

**The symmetry that sets the target at exactly 0**, not slightly positive and
not slightly negative:

- Import 1 kWh → costs €0.35.
- Export 1 kWh → earns €0, *and* it is solar that would otherwise have been
  stored and displaced a later import. So exporting also costs €0.35 of
  foregone saving.

The two errors cost the same, so the optimum sits at zero with no bias in
either direction. **This equivalence breaks when the battery is full** — then
exported solar displaces nothing, is genuinely free, and overshoot is
harmless. It also breaks if E3 is wrong: with a real feed-in tariff, export
earns money and the optimum shifts negative.

> If E2 or E3 are wrong, the target value should be revisited. Nothing else in
> the design depends on them.

## 2. Site

| # | Statement | Status | Source |
| --- | --- | --- | --- |
| S1 | Standby baseload is **20–30 W**, present 24/7 (router, smart devices) | observed by owner | overnight observation, 2026-08-07 |
| S2 | Meter is 3-phase and **balancing** — it nets phases against each other, so only `total_act_power` matters | **stated by owner**, consistent with readings | the Zendure sits on one phase; the meter nets it against the others |
| S3 | Positive `total_act_power` = **importing** from grid | **measured** | 141.8 W positive while the battery was idle and the house was drawing |
| S4 | Output ceiling **800 W**, legal limit | stated by owner | device reports `inverseMaxPower: 800`, agrees |
| S5 | Solar is connected but **temporarily badly positioned**; produces in the afternoon only | **stated by owner** | panel-mount problem, to be resolved |
| S6 | The Zendure is **not** under a HEMS integration in the app | stated by owner | required, or a HEMS overwrites our setpoints |

S5 means **any data collected before the panels are repositioned understates
solar yield** and should not be used to judge the system's economics. It is
still valid for judging *control* quality.

## 3. Hardware behaviour

| # | Statement | Status | Source |
| --- | --- | --- | --- |
| H1 | Minimum usable battery output is **~10 W**, not the 30 W the app enforces | **MEASURED 2026-08-08** | see below |
| H2 | The Zendure outputs its `outputLimit` regardless of household need — it does not self-limit to load | **measured** | grid stayed positive while output tracked the limit exactly |
| H3 | `outputHomePower` tracks `outputLimit` within ~1 W when not saturated | **measured** | see table |
| H4 | SoC values `minSoc`/`socSet` are percent × 10; `electricLevel` is plain percent | measured | `minSoc: 100` = 10 %, `socSet: 1000` = 100 % |
| H5 | The device's own `minSoc` floor is **10 %** | measured | left untouched as an independent backstop |

### H1: the sub-30 W test

Control loop stopped, values written directly, ~12 s settle each:

| commanded | delivered | pack input |
| --- | --- | --- |
| 40 W | 40 W | 40 W |
| 30 W | 30 W | 30 W |
| 25 W | 24 W | 24 W |
| 20 W | 19 W | 19 W |
| 15 W | 15 W | 15 W |
| **10 W** | **10 W** | **10 W** |
| 5 W | **0 W** | 4 W |
| 0 W | 0 W | 0 W |

**Conclusion:** the app's 30 W minimum is a software restriction. The hardware
tracks accurately to 10 W, then collapses — at 5 W the pack draws 4 W
internally while delivering nothing.

**Consequence:** setpoints of 1–9 W are a dead zone that the device silently
treats as 0. No special handling is needed — `basis: "actual"` regulates from
`outputHomePower`, so it sees the real 0 and corrects on the next cycle rather
than believing a setpoint that isn't happening. But it does mean the loop
cannot hold grid at zero if household load ever drops below ~10 W. Per S1 it
does not.

## 4. Control design

| # | Choice | Value | Why |
| --- | --- | --- | --- |
| C1 | `targetGridW` | **0** | Section 1: import and export cost the same, so no bias is justified. |
| C2 | `deadbandW` | **5** | Was 15, which let the loop rest anywhere in 5–35 W. Widening the band is *equivalent to raising the target* — this was the real source of the observed ~40 W average, not the target itself. |
| C3 | `minWriteDeltaW` | **3** | Must be below the deadband or it silently re-widens it. |
| C4 | `gain` | 0.8 | **ASSUMED.** Never tuned against data. 1.0 is deadbeat and rings; 0.8 was a guess at "settles in a couple of cycles". |
| C5 | `maxStepW` | 200 | **ASSUMED.** Bounds the setpoint after a lost-comms window and stops load steps slamming the inverter. Also caps ramp-down speed, so it directly sets how much is exported when a big load switches off. |
| C6 | `intervalMs` | 5000 | **ASSUMED.** Inherited from the forum script. Transient response is bounded by this — a load step is uncorrected for up to 5 s. |
| C7 | `basis` | `"actual"` | Regulating from `outputHomePower` rather than `outputLimit` stops the setpoint winding up when the battery cannot comply. |
| C8 | `reserveSoc` / `resumeSoc` | 15 % / 20 % | Owner asked never to discharge to ≤10 %. The gap prevents chatter at the floor. |

**C4, C5 and C6 are the untuned ones.** They were inherited or guessed, and
they are what the collected data is for. Do not treat them as considered
choices.

## 5. Where the losses actually come from

Two separate contributions, worth not confusing:

- **Steady-state bias** — the loop resting above zero. Costs ≈ €3/year per
  watt, continuously. Fixed by C1 and C2.
- **Transient error** — load steps the loop has not caught up with yet.
  Bounded by C5 and C6, not by the target. A load switching off exports until
  the setpoint ramps down: at `maxStepW: 200` and a 5 s interval, falling from
  800 W to 0 takes ~20 s.

Tuning the target without touching the transient path only fixes the first.
Which one dominates in practice is **an open question** the logged data will
answer — that is the point of having it.

## 6. Open questions

1. **What is the real import price?** (E2) Everything in section 1 is quoted
   against a placeholder.
2. **Is `gain: 0.8` stable at `deadbandW: 5`?** A narrower band means the loop
   acts more often; if 0.8 is too aggressive it will now show up as ringing
   where the old wide band hid it.
3. **How often does the Zendure tolerate being written?** A narrower deadband
   means more writes to a small embedded HTTP server. Watch for failed writes.
4. **How much energy goes to transients vs bias?** Determines whether to spend
   effort on `intervalMs`/`maxStepW` at all.
5. **What does the reserve gate do on real hardware?** Unit-tested, never yet
   exercised — SoC has not dropped to 15 %.
6. **Does the 10 W floor drift** with temperature or SoC? Measured once, at
   69 % SoC, at room temperature.

## 7. How to change any of this

Edit `config/config.local.json`, then:

```bash
python3 tools/deploy.py
```

Nothing in `src/zendure-control.js` needs editing to retune — every value in
section 4 is configuration. Update this document when a value changes, and move
rows from *assumed* to *measured* as evidence arrives.
