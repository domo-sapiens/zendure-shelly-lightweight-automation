#!/usr/bin/env python3
"""Read-only web dashboard over the collector's database.

    tools/dashboard.py            # serve on the configured port

A separate process from the collector on purpose. It opens the database
read-only, so a bug here cannot corrupt the series, and if it crashes the
collector keeps collecting. SQLite in WAL mode handles the concurrent reader.

Stdlib only; the charts are hand-drawn on canvas so there is no library to
vendor, download or keep up to date.
"""

import argparse
import json
import logging
import os
import sqlite3
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config", "config.local.json")
WEB_DIR = os.path.join(ROOT, "web")

log = logging.getLogger("dashboard")

SERIES_COLUMNS = [
    "grid_w", "output_limit", "output_home_w", "soc",
    "pack_power_w", "solar_input_w", "pack_temp_c", "dev_temp_c",
]

# Raw resolution is only useful over short windows; beyond this, serve the
# rollup so the browser is not asked to draw a hundred thousand points.
RAW_MAX_MINUTES = 360


def load_config(path):
    with open(path) as fh:
        cfg = json.load(fh)
    cfg.setdefault("dashboard", {})
    cfg["dashboard"].setdefault("port", 8088)
    cfg["dashboard"].setdefault("bindHost", "0.0.0.0")
    return cfg


def connect(db_path):
    """Open the collector's database without being able to modify it.

    Deliberately not `mode=ro`: the database is in WAL mode, and a read-only
    connection cannot create the -shm index it needs, so mode=ro fails outright
    unless the writer happens to have it mapped already. `query_only` gives the
    same guarantee -- any write raises -- while still allowing normal WAL
    reads whether or not the collector is running.
    """
    db = sqlite3.connect(db_path, timeout=5, check_same_thread=False)
    db.execute("PRAGMA query_only=1")
    db.row_factory = sqlite3.Row
    return db


class Api:
    def __init__(self, cfg):
        self.cfg = cfg
        self.db_path = cfg["collector"]["dbPath"]

    def _db(self):
        return connect(self.db_path)

    def latest(self):
        db = self._db()
        row = db.execute(
            "SELECT * FROM samples ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        db.close()
        if not row:
            return {"ok": False}
        d = dict(row)
        d["ok"] = True
        d["age_s"] = int(time.time()) - d["ts"]
        out, dc = d.get("output_home_w"), d.get("pack_power_w")
        d["efficiency"] = round(100.0 * out / dc, 1) if out and dc else None
        d["target_w"] = self.cfg["control"]["targetGridW"]
        d["deadband_w"] = self.cfg["control"]["deadbandW"]
        d["reserve_soc"] = self.cfg["battery"]["reserveSoc"]
        d["resume_soc"] = self.cfg["battery"]["resumeSoc"]
        return d

    def series(self, minutes, since=None):
        table = "samples" if minutes <= RAW_MAX_MINUTES else "samples_1m"
        start = since if since else int(time.time()) - minutes * 60
        cols = ",".join(SERIES_COLUMNS)
        db = self._db()
        rows = db.execute(
            "SELECT ts,shelly_ok,zendure_ok,%s FROM %s WHERE ts > ? ORDER BY ts"
            % (cols, table),
            (start,),
        ).fetchall()
        db.close()

        out = {"table": table, "ts": []}
        for c in SERIES_COLUMNS:
            out[c] = []
        out["ok"] = []
        for r in rows:
            out["ts"].append(r["ts"])
            # A failed poll is a real, visible gap: null so the chart breaks the
            # line rather than drawing straight through missing time.
            zok = r["zendure_ok"] if table == "samples" else (r["zendure_ok_n"] or 0) > 0
            sok = r["shelly_ok"] if table == "samples" else (r["shelly_ok_n"] or 0) > 0
            out["ok"].append(1 if (zok and sok) else 0)
            for c in SERIES_COLUMNS:
                out[c].append(r[c])
        return out

    def health(self):
        db = self._db()
        now = int(time.time())
        day = db.execute(
            """SELECT COUNT(*) n, AVG(shelly_ok)*100 s, AVG(zendure_ok)*100 z
               FROM samples WHERE ts > ?""", (now - 86400,)
        ).fetchone()
        hour = db.execute(
            """SELECT COUNT(*) n, AVG(shelly_ok)*100 s, AVG(zendure_ok)*100 z
               FROM samples WHERE ts > ?""", (now - 3600,)
        ).fetchone()
        span = db.execute(
            "SELECT MIN(ts) lo, MAX(ts) hi FROM samples"
        ).fetchone()
        rollup = db.execute("SELECT COUNT(*) n FROM samples_1m").fetchone()
        db.close()
        size = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0
        return {
            "now": now,
            "last_sample_age_s": (now - span["hi"]) if span["hi"] else None,
            "hour": {"n": hour["n"], "shelly": hour["s"], "zendure": hour["z"]},
            "day": {"n": day["n"], "shelly": day["s"], "zendure": day["z"]},
            "raw_rows": day["n"],
            "rollup_rows": rollup["n"],
            "db_bytes": size,
            "oldest": span["lo"],
        }

    def energy(self, hours):
        """Imported and exported kWh, and what the import cost.

        Integrates grid power over the sample interval. Import and export are
        summed separately because they are not interchangeable: exported solar
        earns nothing, imported energy is billed.
        """
        db = self._db()
        start = int(time.time()) - hours * 3600
        rows = db.execute(
            """SELECT ts, grid_w FROM samples
               WHERE ts > ? AND shelly_ok=1 AND grid_w IS NOT NULL
               ORDER BY ts""", (start,)
        ).fetchall()
        db.close()

        imp = exp = 0.0
        prev_ts = None
        for r in rows:
            if prev_ts is not None:
                dt = r["ts"] - prev_ts
                # A long gap means the collector was down, not that power was
                # constant across it. Do not integrate across those.
                if 0 < dt <= 30:
                    wh = r["grid_w"] * dt / 3600.0
                    if wh >= 0:
                        imp += wh
                    else:
                        exp -= wh
            prev_ts = r["ts"]
        price = self.cfg.get("economics", {}).get("importPriceEurPerKwh", 0.3159)
        return {
            "hours": hours,
            "import_kwh": imp / 1000.0,
            "export_kwh": exp / 1000.0,
            "import_cost_eur": imp / 1000.0 * price,
            "price": price,
            "samples": len(rows),
        }

    def efficiency(self, hours):
        """Inverter efficiency against output power, steady-state only.

        Samples taken while the setpoint is moving compare an AC reading and a
        DC reading from different moments, which produces meaningless outliers
        (both >100% and absurdly low). Only intervals where outputLimit was
        unchanged from the previous sample are used.
        """
        db = self._db()
        start = int(time.time()) - hours * 3600
        rows = db.execute(
            """
            WITH s AS (
              SELECT ts, output_home_w, pack_power_w, output_limit, pack_temp_c,
                     LAG(output_limit) OVER (ORDER BY ts) prev_limit,
                     LAG(ts)           OVER (ORDER BY ts) prev_ts
              FROM samples
              WHERE ts > ? AND zendure_ok=1
                AND pack_power_w > 0 AND output_home_w > 0
            )
            SELECT output_home_w w, pack_power_w dc, pack_temp_c t
            FROM s
            WHERE prev_limit = output_limit AND ts - prev_ts <= 15
            """, (start,)
        ).fetchall()
        db.close()

        # 10 W bins, so the region below the app's 30 W floor is resolved.
        bins = {}
        for r in rows:
            b = int(r["w"] // 10) * 10
            bins.setdefault(b, []).append((100.0 * r["w"] / r["dc"], r["t"]))

        out = []
        for b in sorted(bins):
            vals = sorted(v[0] for v in bins[b])
            temps = [v[1] for v in bins[b] if v[1] is not None]
            n = len(vals)
            out.append({
                "bin_w": b,
                "n": n,
                "median": vals[n // 2],
                "p10": vals[int(n * 0.1)],
                "p90": vals[int(n * 0.9)],
                "temp": sum(temps) / len(temps) if temps else None,
            })
        return {"hours": hours, "bins": out, "samples": len(rows)}


class Handler(BaseHTTPRequestHandler):
    api = None
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log.debug(fmt, *args)

    def _send(self, code, body, ctype):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj):
        self._send(200, json.dumps(obj), "application/json")

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)

        def num(name, default):
            try:
                return int(q.get(name, [default])[0])
            except (TypeError, ValueError):
                return default

        try:
            if u.path in ("/", "/index.html"):
                with open(os.path.join(WEB_DIR, "index.html"), "rb") as fh:
                    return self._send(200, fh.read(), "text/html; charset=utf-8")
            if u.path == "/api/latest":
                return self._json(self.api.latest())
            if u.path == "/api/series":
                since = num("since", 0) or None
                return self._json(self.api.series(num("minutes", 60), since))
            if u.path == "/api/health":
                return self._json(self.api.health())
            if u.path == "/api/energy":
                return self._json(self.api.energy(num("hours", 24)))
            if u.path == "/api/efficiency":
                return self._json(self.api.efficiency(num("hours", 168)))
            self._send(404, "not found", "text/plain")
        except FileNotFoundError:
            self._send(404, "not found", "text/plain")
        except Exception as exc:  # noqa: BLE001
            log.exception("request failed: %s", self.path)
            self._send(500, json.dumps({"error": str(exc)}), "application/json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=CONFIG_PATH)
    ap.add_argument("--port", type=int)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    cfg = load_config(args.config)
    port = args.port or cfg["dashboard"]["port"]
    host = cfg["dashboard"]["bindHost"]

    Handler.api = Api(cfg)
    server = ThreadingHTTPServer((host, port), Handler)
    log.info("dashboard on http://%s:%d/ (read-only, unauthenticated)", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")


if __name__ == "__main__":
    main()
