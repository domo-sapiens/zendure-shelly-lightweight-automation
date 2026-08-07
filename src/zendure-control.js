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
  lastWritten: -1,
  consecutiveErrors: 0,
  busy: false
};

function log(level, msg) {
  if (CFG.verbose >= level) print(msg);
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

// The value we regulate *from*. "actual" tracks what the battery really pushes
// out (outputHomePower), which stops the setpoint from winding up when the
// battery cannot reach its limit. "limit" reproduces the original forum script.
function controlBasis(props) {
  if (CFG.control.basis === "limit") return props.outputLimit;
  if (typeof props.outputHomePower === "undefined") return props.outputLimit;
  return props.outputHomePower;
}

function computeSetpoint(props, grid) {
  var error = grid - CFG.control.targetGridW;

  if (error > -CFG.control.deadbandW && error < CFG.control.deadbandW) {
    return props.outputLimit; // inside deadband: hold current setpoint
  }

  var target = controlBasis(props) + CFG.control.gain * error;

  // Limit how far the setpoint may move in a single cycle.
  var delta = target - props.outputLimit;
  if (delta > CFG.control.maxStepW) target = props.outputLimit + CFG.control.maxStepW;
  if (delta < -CFG.control.maxStepW) target = props.outputLimit - CFG.control.maxStepW;

  target = clamp(~~target, CFG.zendure.minOutputW, CFG.zendure.maxOutputW);

  // Battery protection wins over everything else.
  if (props.electricLevel <= CFG.battery.cutoffSoc) target = 0;

  return target;
}

// Optional charge/discharge hysteresis by moving minSoc. Disabled by default:
// reported broken on Zendure firmware >= 1.0.23 (rapid toggling at 10-20% SoC).
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

function writeToZendure(sn, outputLimit, minSoc) {
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
      state.busy = false;
      if (errCode !== 0) {
        log(1, "write failed: " + errMsg);
        return;
      }
      if (res.code !== 200) {
        log(1, "write rejected: HTTP " + res.code);
        return;
      }
      state.lastWritten = outputLimit;
      log(3, "write ok");
    }
  );
}

function tick() {
  // Skip this cycle if the previous round trip has not finished; mJS allows
  // only a handful of concurrent RPC calls.
  if (state.busy) {
    log(2, "previous cycle still in flight, skipping");
    return;
  }
  state.busy = true;

  Shelly.call(
    "HTTP.GET",
    { url: ZEN_REPORT, timeout: CFG.http.timeoutS },
    function (res, errCode, errMsg) {
      if (errCode !== 0 || !res || res.code !== 200) {
        state.busy = false;
        state.consecutiveErrors++;
        log(1, "report read failed (" + state.consecutiveErrors + "x): " +
               (errCode !== 0 ? errMsg : "HTTP " + res.code));
        return;
      }

      var report = JSON.parse(res.body);
      if (!report || !report.properties) {
        state.busy = false;
        state.consecutiveErrors++;
        log(1, "report malformed");
        return;
      }
      state.consecutiveErrors = 0;

      var props = report.properties;
      var grid = readGridPower();
      if (grid === null) {
        state.busy = false;
        log(1, "meter not readable, skipping cycle");
        return;
      }

      log(3, "soc=" + props.electricLevel + "% minSoc=" + props.minSoc / 10 +
             "% socSet=" + props.socSet / 10 + "% acMode=" + props.acMode +
             " inputLimit=" + props.inputLimit + "W");
      log(2, "grid=" + grid + "W outputLimit=" + props.outputLimit +
             "W outputHomePower=" + props.outputHomePower + "W soc=" +
             props.electricLevel + "%");

      var setpoint = computeSetpoint(props, grid);
      var minSoc = computeMinSoc(props);

      var change = setpoint - props.outputLimit;
      if (change < 0) change = -change;
      if (change < CFG.control.minWriteDeltaW && minSoc === null) {
        state.busy = false;
        log(3, "no meaningful change (" + change + "W), not writing");
        return;
      }

      log(2, "-> outputLimit " + props.outputLimit + "W => " + setpoint + "W");
      writeToZendure(report.sn, setpoint, minSoc);
    }
  );
}

log(1, "zendure zero-feed-in starting: zendure=" + CFG.zendure.host +
       " interval=" + CFG.control.intervalMs + "ms target=" +
       CFG.control.targetGridW + "W");

Timer.set(CFG.control.intervalMs, true, tick);
tick();
