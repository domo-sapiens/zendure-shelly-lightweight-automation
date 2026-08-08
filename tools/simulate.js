// Simulate the Shelly mJS environment and exercise the control script.
const fs = require('fs');
const path = require('path').join(__dirname, '..', 'build', 'zendure-control.js');

let env;

function loadScript(opts) {
  env = {
    now: 0,
    timerFn: null,
    timerMs: 0,
    pending: [],      // queued Shelly.call callbacks
    writes: [],       // POST bodies actually sent
    logs: [],
    gridPower: opts.gridPower,
    props: Object.assign({}, opts.props),
  };

  const sandbox = {
    print: (...a) => env.logs.push(a.join('')),
    JSON,
    Timer: { set: (ms, repeat, fn) => { env.timerMs = ms; env.timerFn = fn; } },
    Shelly: {
      getComponentStatus: (kind, id) =>
        kind === 'em' && id === 0 ? { total_act_power: env.gridPower } : null,
      call: (method, params, cb) => {
        env.pending.push({ method, params, cb });
      },
    },
  };

  const code = fs.readFileSync(path, 'utf8');
  const vm = require('vm');
  vm.createContext(sandbox);
  vm.runInContext(code, sandbox);
  return sandbox;
}

// Deliver the oldest pending call with a successful response.
function deliver({ drop = false } = {}) {
  const call = env.pending.shift();
  if (!call) return null;
  if (drop) return call;           // simulate a callback that never fires
  if (call.method === 'HTTP.GET') {
    call.cb({ code: 200, body: JSON.stringify({ sn: 'TEST', properties: env.props }) }, 0, '');
  } else {
    env.writes.push(JSON.parse(call.params.body));
    call.cb({ code: 200, body: '{}' }, 0, '');
  }
  return call;
}

function tickN(n, opts = {}) {
  for (let i = 0; i < n; i++) {
    env.timerFn();
    if (!opts.dropAll) deliver();
    if (env.pending.length && !opts.dropAll) deliver(); // the POST, if any
  }
}

let failures = 0;
function check(name, cond, detail) {
  console.log(`${cond ? '  PASS' : '  FAIL'}  ${name}${detail ? '  -- ' + detail : ''}`);
  if (!cond) failures++;
}

const baseProps = {
  electricLevel: 50, minSoc: 100, socSet: 1000, acMode: 2,
  inputLimit: 0, outputLimit: 100, outputHomePower: 100,
};

// ---------------------------------------------------------------------------
console.log('\n[1] Reserve gate is enforced even when the error sits in the deadband');
// grid == target => error 0 => deadband early-return path. SoC below reserve.
loadScript({ gridPower: 20, props: { ...baseProps, electricLevel: 14, outputLimit: 300 } });
deliver(); // the startup tick()'s GET
deliver(); // its POST, if any
check('discharge forced to 0 inside deadband', env.writes.length === 1 && env.writes[0].properties.outputLimit === 0,
      'writes=' + JSON.stringify(env.writes.map(w => w.properties.outputLimit)));

console.log('\n[2] Reserve gate beats minWriteDelta (trickle discharge below reserve)');
loadScript({ gridPower: 20, props: { ...baseProps, electricLevel: 14, outputLimit: 3 } });
deliver(); deliver();
check('3W trickle still written down to 0', env.writes.length === 1 && env.writes[0].properties.outputLimit === 0,
      'writes=' + JSON.stringify(env.writes.map(w => w.properties.outputLimit)));

console.log('\n[3] Battery never discharged at or below 10%');
let violations = [];
for (let soc = 5; soc <= 30; soc++) {
  loadScript({ gridPower: 500, props: { ...baseProps, electricLevel: soc, outputLimit: 400 } });
  deliver(); deliver();
  const sp = env.writes.length ? env.writes[0].properties.outputLimit : 400;
  if (soc <= 10 && sp > 0) violations.push(`soc=${soc} -> ${sp}W`);
}
check('no discharge commanded at SoC <= 10% under heavy load', violations.length === 0, violations.join(', '));

console.log('\n[4] Hysteresis: no chatter at the floor');
// Blocked at 14%, then SoC "recovers" to 16-19% as the pack relaxes.
loadScript({ gridPower: 500, props: { ...baseProps, electricLevel: 14, outputLimit: 400 } });
deliver(); deliver();
const blockedAt14 = env.writes[0].properties.outputLimit === 0;
let resumedEarly = false;
for (const soc of [16, 17, 18, 19]) {
  env.props.electricLevel = soc;
  env.props.outputLimit = 0;
  env.writes.length = 0;
  env.timerFn(); deliver(); deliver();
  if (env.writes.length && env.writes[0].properties.outputLimit > 0) resumedEarly = true;
}
check('stays blocked while SoC recovers below resume threshold', blockedAt14 && !resumedEarly);
env.props.electricLevel = 20; env.props.outputLimit = 0; env.writes.length = 0;
env.timerFn(); deliver(); deliver();
check('resumes at resumeSoc (20%)', env.writes.length === 1 && env.writes[0].properties.outputLimit > 0,
      'writes=' + JSON.stringify(env.writes.map(w => w.properties.outputLimit)));

// ---------------------------------------------------------------------------
console.log('\n[5] Watchdog recovers from a callback that never fires');
loadScript({ gridPower: 500, props: { ...baseProps } });
env.pending.length = 0;            // startup GET (tick 1) never comes back
// Walk ticks until a fresh call is issued; record which tick recovered.
let recoveredAtTick = null;
for (let i = 0; i < 6 && recoveredAtTick === null; i++) {
  env.timerFn();                   // this is tick i+2
  if (env.pending.length > 0) recoveredAtTick = i + 2;
}
const recovered = recoveredAtTick !== null;
check('new cycle starts after watchdog timeout', recovered,
      'recovered at tick ' + recoveredAtTick);
check('did not give up early (busyTimeoutCycles=3)', recoveredAtTick === 4,
      'expected tick 4, got ' + recoveredAtTick);
if (recovered) {
  deliver(); deliver();
  check('regulation resumes after recovery', env.writes.length === 1,
        'writes=' + JSON.stringify(env.writes.map(w => w.properties.outputLimit)));
}
check('watchdog trip was logged', env.logs.some(l => l.indexOf('watchdog') >= 0));

console.log('\n[6] Late callback from an abandoned cycle cannot corrupt state');
loadScript({ gridPower: 500, props: { ...baseProps } });
const stale = env.pending.shift();          // startup GET, held back
for (let i = 0; i < 4; i++) { env.timerFn(); env.pending.length = 0; }
env.timerFn();                               // fresh cycle now in flight
const inflight = env.pending.length;
stale.cb({ code: 200, body: JSON.stringify({ sn: 'TEST', properties: env.props }) }, 0, '');
check('stale callback produced no write', env.writes.length === 0);
check('stale callback did not release the in-flight cycle',
      env.logs.some(l => l.indexOf('late report callback') >= 0));

// ---------------------------------------------------------------------------
console.log('\n[7] Normal regulation still works');
// Expectations are derived from the deployed config, not hardcoded, so
// retuning target/deadband/gain does not turn these into false failures.
const cfg = loadScript({ gridPower: 0, props: { ...baseProps } }).CFG;
const { targetGridW: TGT, deadbandW: DB, gain: G, maxStepW: STEP } = cfg.control;
console.log(`      (config: target=${TGT}W deadband=${DB}W gain=${G} maxStep=${STEP}W)`);

// Just inside the deadband by construction, whatever the deadband is set to.
loadScript({ gridPower: TGT + DB * 0.5, props: { ...baseProps } });
deliver(); deliver();
check('deadband suppresses the write', env.writes.length === 0,
      'wrote ' + JSON.stringify(env.writes.map(w => w.properties.outputLimit)));

// Comfortably outside it: the setpoint must move to absorb the excess import.
const excess = Math.max(100, DB * 4);
loadScript({ gridPower: TGT + excess, props: { ...baseProps, outputLimit: 100, outputHomePower: 100 } });
deliver(); deliver();
const wrote = env.writes.length === 1 ? env.writes[0].properties.outputLimit : null;
const expected = Math.min(100 + Math.trunc(G * excess), 100 + STEP);
check('setpoint absorbs the excess import', wrote !== null && Math.abs(wrote - expected) <= 1,
      `expected ~${expected}, got ${wrote}`);
check('and moves in the correct direction', wrote !== null && wrote > 100, 'got ' + wrote);

loadScript({ gridPower: 5000, props: { ...baseProps, outputLimit: 0, outputHomePower: 0 } });
deliver(); deliver();
check(`maxStepW caps the jump at ${STEP}W`,
      env.writes.length === 1 && env.writes[0].properties.outputLimit === STEP,
      'got ' + (env.writes.length ? env.writes[0].properties.outputLimit : 'none'));

// The dead zone measured on real hardware: 1-9W is commanded but delivers 0.
// basis:"actual" must regulate from the real 0, not the phantom setpoint.
console.log('\n[8] Sub-10W dead zone does not wind the setpoint up');
loadScript({ gridPower: TGT + 20, props: { ...baseProps, outputLimit: 5, outputHomePower: 0 } });
deliver(); deliver();
const dz = env.writes.length ? env.writes[0].properties.outputLimit : null;
check('regulates from delivered 0W, not the commanded 5W',
      dz !== null && Math.abs(dz - Math.trunc(G * 20)) <= 1,
      `expected ~${Math.trunc(G * 20)}, got ${dz}`);

console.log(failures === 0 ? '\nALL PASS\n' : `\n${failures} FAILURE(S)\n`);
process.exit(failures ? 1 : 0);
