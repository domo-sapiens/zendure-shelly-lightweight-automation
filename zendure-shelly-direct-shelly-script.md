# Path 1: Direct Shelly Script (No Cloud, No Hub)

## Overview
A script runs directly on the Shelly Pro 3EM itself (via its built-in Scripts feature). It polls the Zendure 800 Plus's local HTTP API for status, reads live grid power from the meter, calculates a new output setpoint, and posts it back — all local HTTP, no cloud, no MQTT broker, no Home Assistant/ioBroker required.

## Requirements
- Shelly Pro 3EM and Zendure 800 Plus both on the same local Wi-Fi network, able to reach each other via HTTP.
- The Zendure device must **NOT** be configured under a HEMS (Home Energy Management System) integration in the Zendure app — a HEMS will continuously overwrite the values the script writes.

## How It Works
1. Shelly script calls `HTTP.GET http://<zendure-ip>/properties/report` every few seconds (default interval: 5000ms) to read current Zendure state (SoC, charge/discharge limits, output limits, etc.).
2. Script reads live grid power from the Shelly's own energy meter component (`Shelly.getComponentStatus("em", 0)`).
3. Calculates a new `outputLimit` for the Zendure based on current output limit + grid power / 2 (zero feed-in logic), clamped to 0–800W.
4. Posts the new value back via `HTTP.POST http://<zendure-ip>/properties/write`.
5. Optional "hysteresis" block adjusts `minSoc` (min state of charge) to protect the battery between configurable min/hysteresis/max SoC thresholds.

## Source / Script
- Forum write-up (German) with full script: https://forum.zendure.com/d/18610 — "How-To: Cloudless, Serverless: SolarFlow800 pro + Shelly pro 3em"
- Script also mirrored on Pastebin: https://pastebin.com/LPjMLV2Z (note: missing first line `function timerCode() {` — must be added manually)
- Zendure local API reference (zenSDK): https://github.com/Zendure/zenSDK

## Script Body (as posted)
```js
function timerCode() {
  Shelly.call(
    "HTTP.GET", {
      "url": "http://192.168.0.90/properties/report",
    },
    function(result) {
      var verbose = 2;
      var akku_min = 15;
      var akku_hyst = 20;
      var akku_max = 95;

      var zendu_stat = JSON.parse(result.body);
      if (verbose >= 3) {
        print("Seriennummer:    ", zendu_stat.sn);
        print("Entladegrenze:   ", zendu_stat.properties.minSoc/10, "%");
        print("Aufladegrenze:   ", zendu_stat.properties.socSet/10, "%");
        print("Ladestand:       ", zendu_stat.properties.electricLevel, "%");
        print("acMode:          ", zendu_stat.properties.acMode);
        print("inputLimit:      ", zendu_stat.properties.inputLimit, "W");
        print("outputLimit:     ", zendu_stat.properties.outputLimit, "W");
        print("outputHomePower: ", zendu_stat.properties.outputHomePower, "W");
      }

      var emStatus = Shelly.getComponentStatus("em", 0);
      if (!emStatus || typeof emStatus.total_act_power === "undefined") {
        if (verbose >= 1) print("Haupttimer: emStatus oder total_act_power nicht verfügbar.");
        return;
      }
      if (verbose >= 2) print("Shelly_Power:    ", emStatus.total_act_power, "W");
      var payload = {};
      var properties = {};
      payload.properties = properties;
      payload.sn = zendu_stat.sn;
      payload.properties.outputLimit = ~~(zendu_stat.properties.outputLimit + emStatus.total_act_power / 2);
      if (payload.properties.outputLimit < 0) {
        payload.properties.outputLimit = 0;
      }
      if (payload.properties.outputLimit > 800) {
        payload.properties.outputLimit = 800;
      }
      //AKKUSCHUTZ (battery protection)
      if (zendu_stat.properties.electricLevel <= 10)  {
        payload.properties.outputLimit = 0;
      }
      if (verbose >= 2) print("outputLimit:     ", payload.properties.outputLimit, "W");
      //HYSTERESE
      if (zendu_stat.properties.electricLevel <= akku_min && zendu_stat.properties.minSoc/10 === akku_min) {
        payload.properties.minSoc = akku_hyst * 10;
        if (verbose >= 2) print("HYSTERESE: Aufladung: ", payload.properties.minSoc/10, "%");
      }
      if (zendu_stat.properties.electricLevel > akku_hyst && zendu_stat.properties.minSoc/10 === akku_hyst) {
        payload.properties.minSoc = akku_min * 10;
        if (verbose >= 2) print("HYSTERESE: Entladung: ", payload.properties.minSoc/10, "%");
      }

      var myjson = JSON.stringify(payload);
      if (verbose >= 3) print("Post-Request: ", myjson);
      Shelly.call(
        "HTTP.POST", {
          "url": "http://192.168.0.90/properties/write",
          "body": myjson
        },
        function(result) {
          let response = JSON.parse(result.body);
          //print("Post-Response: ", response.data);
        }
      );

      if (verbose >= 3) print("done!");
    }
  );
}

//Intervall in Millisekunden: 5s --> 5000ms:
Timer.set( 5000, true, timerCode );
```

## What to Customize
- **Zendure IP address**: replace `192.168.0.90` on both the GET url line and the POST url line with your actual Zendure device IP.
- **Poll interval**: last line, `Timer.set(5000, true, timerCode)` — value in milliseconds.
- **Hysteresis block**: lines handling `akku_min`/`akku_hyst`/`akku_max` — can be deleted if not wanted; if kept, set your desired battery SoC thresholds (defaults: min 15%, hysteresis 20%, max 95%).

## Known Issues / Caveats
- Author states this is not production-hardened: minimal error handling, and the author is not a professional JS programmer.
- One user (ZenOlli) reported the published outputLimit formula seems off and suggested a simpler alternative: `payload.properties.outputLimit = emStatus.total_act_power;` — untested by them.
- Another user reported that after upgrading Zendure firmware to v1.0.23, the hysteresis charge/discharge switching broke, causing the battery to rapidly toggle between charge/discharge between 10–20% SoC — no known fix/downgrade path reported as of the thread's last update (2026-03-05).
- Also reported: the Zendure sometimes charges to 95% from the grid overnight, leaving no headroom for solar the next day — separate from the hysteresis bug.
- Script must be pasted starting from `function timerCode() {` — the Pastebin copy is missing that opening line.

## Separate Note: Shelly "Smart CT" Native Pairing (Different From This Approach)
There is also a built-in "Smart CT" pairing mode in the Zendure app that connects the Shelly to the Zendure using RDP-over-UDP. Per a Zendure community reply, this native pairing method **does** require a one-time cloud round-trip (both Shelly cloud and Zendure cloud) during initial pairing, even though day-to-day communication afterward may be local. The direct-script approach above avoids this entirely by not using that native pairing flow at all — it just talks to the Zendure's local REST API directly.

Source: https://forum.zendure.com/d/15192
