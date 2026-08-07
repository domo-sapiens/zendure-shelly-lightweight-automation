#!/usr/bin/env python3
"""Build the control script from config and upload it to the Shelly Pro 3EM.

    tools/deploy.py                 # build + upload + start
    tools/deploy.py --build-only    # just render build/zendure-control.js
    tools/deploy.py --stop          # stop the script on the device
    tools/deploy.py --logs          # tail the script's print() output

Stdlib only, no pip install needed.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config", "config.local.json")
SRC_PATH = os.path.join(ROOT, "src", "zendure-control.js")
BUILD_PATH = os.path.join(ROOT, "build", "zendure-control.js")

PLACEHOLDER = "__CONFIG_JSON__"
CHUNK_SIZE = 1024


def load_config():
    if not os.path.exists(CONFIG_PATH):
        sys.exit(
            "Missing config/config.local.json.\n"
            "Copy config/config.example.json to it and fill in your values."
        )
    with open(CONFIG_PATH) as fh:
        return json.load(fh)


def render(cfg):
    with open(SRC_PATH) as fh:
        src = fh.read()
    if PLACEHOLDER not in src:
        sys.exit("src/zendure-control.js no longer contains %s" % PLACEHOLDER)

    # The Shelly does not need the deploy-only section, and its password has no
    # business being written into a script stored on the device.
    device_cfg = {k: v for k, v in cfg.items() if k != "shelly"}
    code = src.replace(PLACEHOLDER, json.dumps(device_cfg, indent=2))

    os.makedirs(os.path.dirname(BUILD_PATH), exist_ok=True)
    with open(BUILD_PATH, "w") as fh:
        fh.write(code)
    return code


def reachable(host):
    try:
        urllib.request.urlopen(
            "http://%s/rpc/Shelly.GetDeviceInfo" % host, timeout=3
        ).read()
        return True
    except Exception:
        return False


def resolve_host(cfg_section, label):
    """Prefer the static IP, fall back to the router-provided hostname."""
    host = cfg_section["host"]
    if reachable(host):
        return host
    alt = cfg_section.get("hostname")
    if alt and reachable(alt):
        print("%s: %s did not answer, using hostname %s" % (label, host, alt))
        return alt
    sys.exit("%s unreachable at %s%s" % (label, host, " or " + alt if alt else ""))


class Device:
    def __init__(self, host, password):
        self.base = "http://%s/rpc/" % host
        opener_handlers = []
        if password:
            mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
            mgr.add_password(None, "http://%s/" % host, "admin", password)
            opener_handlers.append(urllib.request.HTTPDigestAuthHandler(mgr))
        self.opener = urllib.request.build_opener(*opener_handlers)

    def call(self, method, params=None):
        data = json.dumps(params or {}).encode()
        req = urllib.request.Request(
            self.base + method, data=data,
            headers={"Content-Type": "application/json"},
        )
        try:
            with self.opener.open(req, timeout=10) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            sys.exit("%s failed: HTTP %s %s" % (method, exc.code, body))
        except urllib.error.URLError as exc:
            sys.exit("%s failed: cannot reach device (%s)" % (method, exc.reason))


def find_script(dev, name):
    for script in dev.call("Script.List").get("scripts", []):
        if script["name"] == name:
            return script["id"]
    return None


def upload(dev, name, code):
    script_id = find_script(dev, name)
    if script_id is None:
        script_id = dev.call("Script.Create", {"name": name})["id"]
        print("created script %s (id %d)" % (name, script_id))
    else:
        dev.call("Script.Stop", {"id": script_id})
        print("reusing script %s (id %d)" % (name, script_id))

    for offset in range(0, len(code), CHUNK_SIZE):
        dev.call("Script.PutCode", {
            "id": script_id,
            "code": code[offset:offset + CHUNK_SIZE],
            "append": offset > 0,
        })
    print("uploaded %d bytes" % len(code))

    # enable=true makes the script restart automatically after a device reboot.
    dev.call("Script.SetConfig", {"id": script_id, "config": {"enable": True}})
    dev.call("Script.Start", {"id": script_id})
    print("started")
    return script_id


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build-only", action="store_true")
    ap.add_argument("--stop", action="store_true")
    ap.add_argument("--logs", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    shelly = cfg["shelly"]
    name = shelly.get("scriptName", "zendure-zero-feedin")

    if args.build_only:
        render(cfg)
        print("wrote %s" % BUILD_PATH)
        return

    host = resolve_host(shelly, "Shelly")
    dev = Device(host, shelly.get("password", ""))

    if args.stop:
        script_id = find_script(dev, name)
        if script_id is None:
            sys.exit("no script named %r on the device" % name)
        dev.call("Script.SetConfig", {"id": script_id, "config": {"enable": False}})
        dev.call("Script.Stop", {"id": script_id})
        print("stopped %s (id %d)" % (name, script_id))
        return

    if args.logs:
        print("streaming logs, Ctrl-C to stop")
        req = urllib.request.Request("http://%s/debug/log" % host)
        with dev.opener.open(req) as resp:
            for line in resp:
                sys.stdout.write(line.decode(errors="replace"))
                sys.stdout.flush()
        return

    upload(dev, name, render(cfg))


if __name__ == "__main__":
    main()
