// Zero-feed-in control loop: Shelly Pro 3EM -> Zendure SolarFlow 800 Plus.
// Runs on the Shelly's own script engine (mJS, not full JS: no const, no arrow
// functions, no template literals, no Array.prototype.map/forEach).
//
// Do not edit the CFG line below by hand: tools/deploy.py substitutes it from
// config/config.local.json when building into build/.

var CFG = __CONFIG_JSON__;

var ZEN_REPORT = "http://" + CFG.zendure.host + "/properties/report";
var ZEN_WRITE = "http://" + CFG.zendure.host + "/properties/write";

var state = {
  // Cycle generation. Every tick claims a new number; a callback that finds the
  // number has moved on is a straggler from a cycle the watchdog already gave up
  // on, and must not touch shared state.
  cycle: 0,
  ticks: 0,
  busy: false,
  busySince: 0,

  // null until the first reading: the latch cannot be recovered across a
  // restart, so it is re-derived from SoC rather than assumed open.
  dischargeBlocked: null,

  pollErrors: 0,
  writeErrors: 0,
  consecutiveErrors: 0,
  skipped: 0,
  watchdogTrips: 0
};

function log(level, msg) {
  if (CFG.verbose >= level) print(msg);
}

function abs(v) {
  return v < 0 ? -v : v;
}

function clamp(v, lo, hi) {
  if (v < lo) return lo;
  if (v > hi) return hi;
  return v;
}

// Grid power at the meter, normalised so that positive = importing from grid.
// Returns null when the meter is not readable yet (e.g. right after boot).
function readGridPower() {
  var p = null;

  if (CFG.meter.profile === "triphase") {
    var st = Shelly.getComponentStatus("em", 0);
    if (st && typeof st.total_act_power !== "undefined") {
      p = st.total_act_power;
    }
  } else {
    var sum = 0;
    var ok = true;
    var i;
    for (i = 0; i < 3; i++) {
      var ph = Shelly.getComponentStatus("em1", i);
      if (!ph || typeof ph.act_power === "undefined") {
        ok = false;
        break;
      }
      sum = sum + ph.act_power;
    }
    if (ok) p = sum;
  }

  if (p === null) return null;
  if (!CFG.meter.importPositive) p = -p;
  return p;
}

// Reserve gate: stop discharging at or below reserveSoc, and do not resume until
// SoC has recovered to resumeSoc. The gap between the two is what stops the
// battery chattering on and off at the floor -- with output at 0 the pack
// voltage relaxes and the reported SoC ticks back up, which without hysteresis
// would immediately re-enable discharge and cause exactly the cycling wear the
// reserve exists to avoid.
//
// This only ever forces outputLimit to 0. Charging is governed by inputLimit,
// which this script does not touch, so a blocked battery can still charge.
function updateDischargeGate(props) {
  var soc = props.electricLevel;

  if (state.dischargeBlocked === null) {
    // First reading after a start, redeploy or reboot: the latch was lost with
    // the previous process. Between reserveSoc and resumeSoc the correct state
    // is ambiguous from SoC alone, so assume blocked -- otherwise a restart at
    // 18% silently resumes draining a battery that was deliberately being held.
    state.dischargeBlocked = soc < CFG.battery.resumeSoc;
    log(1, "reserve: starting at " + soc + "%, discharge " +
           (state.dischargeBlocked ? "blocked until " + CFG.battery.resumeSoc + "%"
                                   : "allowed"));
  }

  if (state.dischargeBlocked) {
    if (soc >= CFG.battery.resumeSoc) {
      state.dischargeBlocked = false;
      log(1, "reserve: SoC " + soc + "% reached resume threshold " +
             CFG.battery.resumeSoc + "%, discharge re-enabled");
    }
  } else if (soc <= CFG.battery.reserveSoc) {
    state.dischargeBlocked = true;
    log(1, "reserve: SoC " + soc + "% at or below reserve " +
           CFG.battery.reserveSoc + "%, discharge blocked");
  }

  return state.dischargeBlocked;
}

// How much AC output the current solar input can support on its own.
//
// The reserve gate exists to stop the *battery* being drained. Solar arriving
// right now was never in the battery, so serving the house from it costs the
// reserve nothing -- while blocking it forces a grid import and pushes that
// solar through a charge/discharge round trip instead. Measured, that round
// trip turns 160 W of solar into 103 W of AC later, against 131 W if used
// directly: 27 % of the energy thrown away for nothing.
//
// Converted through the measured loss model (H8) and then derated, because
// solarInputPower is measured on the PV side and some of it is lost reaching
// the inverter. Erring low means the battery keeps charging slightly rather
// than quietly discharging below its reserve.
function solarPassthroughCap(props) {
  if (!CFG.solar.passthroughWhenBlocked) return 0;
  var dc = props.solarInputPower;
  if (typeof dc === "undefined" || dc === null || dc <= 0) return 0;

  var usable = dc * CFG.solar.derate - CFG.inverter.overheadW;
  if (usable <= 0) return 0;
  var cap = ~~(usable / CFG.inverter.slope - CFG.solar.marginW);
  return cap > 0 ? cap : 0;
}

// The value we regulate *from*. "actual" tracks what the battery really pushes
// out (outputHomePower), which stops the setpoint from winding up when the
// battery cannot reach its limit. "limit" reproduces the original forum script.
function controlBasis(props) {
  if (CFG.control.basis === "limit") return props.outputLimit;
  if (typeof props.outputHomePower === "undefined") return props.outputLimit;
  return props.outputHomePower;
}

function computeSetpoint(props, grid, blocked, ceiling) {
  // The ceiling is resolved before the deadband, so a reserve breach cannot be
  // skipped over by a cycle that happens to land inside the deadband.
  if (ceiling <= 0) return 0;

  var error = grid - CFG.control.targetGridW;

  if (error > -CFG.control.deadbandW && error < CFG.control.deadbandW) {
    // Inside the deadband: hold, but never above the ceiling. Solar falling
    // away must still pull the setpoint down even when grid power is on target.
    return clamp(props.outputLimit, CFG.zendure.minOutputW, ceiling);
  }

  var target = controlBasis(props) + CFG.control.gain * error;

  // Limit how far the setpoint may move in a single cycle.
  var delta = target - props.outputLimit;
  if (delta > CFG.control.maxStepW) target = props.outputLimit + CFG.control.maxStepW;
  if (delta < -CFG.control.maxStepW) target = props.outputLimit - CFG.control.maxStepW;

  return clamp(~~target, CFG.zendure.minOutputW, ceiling);
}

// Optional charge/discharge hysteresis by moving minSoc on the device. Disabled
// by default: reported broken on Zendure firmware >= 1.0.23 (rapid toggling at
// 10-20% SoC). Independent of the reserve gate above, which is enforced here on
// the Shelly and needs no cooperation from the Zendure.
// Returns the new minSoc in device units (percent * 10), or null for no change.
function computeMinSoc(props) {
  var h = CFG.battery.hysteresis;
  if (!h.enabled) return null;

  var currentMinSoc = props.minSoc / 10;
  if (props.electricLevel <= h.minSoc && currentMinSoc === h.minSoc) {
    log(2, "hysteresis: switching to charge, minSoc -> " + h.hystSoc + "%");
    return h.hystSoc * 10;
  }
  if (props.electricLevel > h.hystSoc && currentMinSoc === h.hystSoc) {
    log(2, "hysteresis: switching to discharge, minSoc -> " + h.minSoc + "%");
    return h.minSoc * 10;
  }
  return null;
}

function release(myCycle) {
  if (myCycle !== state.cycle) return false; // straggler, watchdog moved on
  state.busy = false;
  return true;
}

function writeToZendure(myCycle, sn, outputLimit, minSoc) {
  var props = { outputLimit: outputLimit };
  if (minSoc !== null) props.minSoc = minSoc;

  var body = JSON.stringify({ sn: sn, properties: props });
  log(3, "POST " + body);

  Shelly.call(
    "HTTP.POST",
    {
      url: ZEN_WRITE,
      body: body,
      timeout: CFG.http.timeoutS,
      headers: { "Content-Type": "application/json" }
    },
    function (res, errCode, errMsg) {
      if (!release(myCycle)) return;
      if (errCode !== 0) {
        state.writeErrors++;
        log(1, "write failed: " + errMsg);
        return;
      }
      if (res.code !== 200) {
        state.writeErrors++;
        log(1, "write rejected: HTTP " + res.code);
        return;
      }
      log(3, "write ok");
    }
  );
}

function logHealth() {
  if (CFG.control.healthEveryCycles <= 0) return;
  if (state.ticks % CFG.control.healthEveryCycles !== 0) return;
  log(1, "health: ticks=" + state.ticks +
         " pollErr=" + state.pollErrors +
         " writeErr=" + state.writeErrors +
         " skipped=" + state.skipped +
         " watchdog=" + state.watchdogTrips +
         " blocked=" + state.dischargeBlocked);
}

function tick() {
  state.ticks++;
  logHealth();

  // Watchdog. A Shelly.call whose callback never fires -- which a marginal wifi
  // link does cause -- would otherwise leave busy set forever and silently stop
  // the loop regulating, with no error anywhere. Give up on the stuck cycle and
  // start a fresh one; bumping state.cycle makes any late callback a no-op.
  if (state.busy) {
    var stuckFor = state.ticks - state.busySince;
    if (stuckFor < CFG.control.busyTimeoutCycles) {
      state.skipped++;
      log(2, "previous cycle still in flight, skipping");
      return;
    }
    state.watchdogTrips++;
    log(1, "watchdog: cycle stuck for " + stuckFor +
           " ticks, abandoning it and resuming");
  }

  state.cycle++;
  var myCycle = state.cycle;
  state.busy = true;
  state.busySince = state.ticks;

  Shelly.call(
    "HTTP.GET",
    { url: ZEN_REPORT, timeout: CFG.http.timeoutS },
    function (res, errCode, errMsg) {
      if (myCycle !== state.cycle) {
        log(2, "late report callback from abandoned cycle, ignoring");
        return;
      }

      if (errCode !== 0 || !res || res.code !== 200) {
        release(myCycle);
        state.pollErrors++;
        state.consecutiveErrors++;
        log(1, "report read failed (" + state.consecutiveErrors + "x): " +
               (errCode !== 0 ? errMsg : "HTTP " + res.code));
        return;
      }

      var report = JSON.parse(res.body);
      if (!report || !report.properties) {
        release(myCycle);
        state.pollErrors++;
        state.consecutiveErrors++;
        log(1, "report malformed");
        return;
      }
      state.consecutiveErrors = 0;

      var props = report.properties;
      var grid = readGridPower();
      if (grid === null) {
        release(myCycle);
        log(1, "meter not readable, skipping cycle");
        return;
      }

      var blocked = updateDischargeGate(props);
      // Below the reserve the battery must not be drained, but solar arriving
      // now can still be passed straight to the house.
      var ceiling = blocked ? solarPassthroughCap(props) : CFG.zendure.maxOutputW;

      log(3, "soc=" + props.electricLevel + "% minSoc=" + props.minSoc / 10 +
             "% socSet=" + props.socSet / 10 + "% acMode=" + props.acMode +
             " inputLimit=" + props.inputLimit + "W");
      log(2, "grid=" + grid + "W outputLimit=" + props.outputLimit +
             "W outputHomePower=" + props.outputHomePower + "W soc=" +
             props.electricLevel + "% solar=" + props.solarInputPower + "W" +
             (blocked ? " [reserve, solarCap=" + ceiling + "W]" : ""));

      var setpoint = computeSetpoint(props, grid, blocked, ceiling);
      var minSoc = computeMinSoc(props);

      // Anything that lowers the setpoint while blocked must be written
      // immediately, however small: the minWriteDelta economy must not leave
      // the battery trickling out below its reserve when solar fades.
      var mustWrite = blocked && setpoint < props.outputLimit;
      var change = abs(setpoint - props.outputLimit);

      if (!mustWrite && change < CFG.control.minWriteDeltaW && minSoc === null) {
        release(myCycle);
        log(3, "no meaningful change (" + change + "W), not writing");
        return;
      }

      log(2, "-> outputLimit " + props.outputLimit + "W => " + setpoint + "W" +
             (mustWrite ? " (reserve)" : ""));
      writeToZendure(myCycle, report.sn, setpoint, minSoc);
    }
  );
}

log(1, "zendure zero-feed-in starting: zendure=" + CFG.zendure.host +
       " interval=" + CFG.control.intervalMs + "ms target=" +
       CFG.control.targetGridW + "W reserve=" + CFG.battery.reserveSoc +
       "%/" + CFG.battery.resumeSoc + "%");

Timer.set(CFG.control.intervalMs, true, tick);
tick();
