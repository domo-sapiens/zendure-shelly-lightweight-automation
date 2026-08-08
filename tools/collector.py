#!/usr/bin/env python3
"""Poll the Shelly Pro 3EM and the Zendure independently, store a time series.

    tools/collector.py            # run until stopped
    tools/collector.py --once     # take one sample, print it, exit
    tools/collector.py --stats    # summarise what is in the database

Deliberately outside the control path: this only reads. If it dies, the Shelly
keeps regulating. Stdlib only, so a Raspberry Pi OS Lite image needs nothing
installed.
"""

import argparse
import json
import logging
import os
import re
import signal
import sqlite3
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config", "config.local.json")

log = logging.getLogger("collector")

SAMPLE_COLUMNS = [
    "ts", "shelly_ok", "zendure_ok",
    "grid_w", "grid_a_w", "grid_b_w", "grid_c_w",
    "soc", "output_limit", "output_home_w",
    "pack_input_w", "solar_input_w", "grid_input_w",
    "ac_mode", "min_soc", "soc_set",
    # DC side. pack_power_w is what the battery actually gives up, so
    # pack_power_w vs output_home_w is inverter efficiency -- the measurement
    # needed to test whether the app's 30W floor exists for efficiency reasons.
    "pack_power_w", "bat_v", "bat_a", "pack_temp_c", "dev_temp_c",
]

# Columns added after the first deployment, applied to existing databases.
MIGRATIONS = [
    ("pack_power_w", "REAL"),
    ("bat_v", "REAL"),
    ("bat_a", "REAL"),
    ("pack_temp_c", "REAL"),
    ("dev_temp_c", "REAL"),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
  ts            INTEGER PRIMARY KEY,
  shelly_ok     INTEGER NOT NULL,
  zendure_ok    INTEGER NOT NULL,
  grid_w        REAL,
  grid_a_w      REAL,
  grid_b_w      REAL,
  grid_c_w      REAL,
  soc           INTEGER,
  output_limit  INTEGER,
  output_home_w INTEGER,
  pack_input_w  INTEGER,
  solar_input_w INTEGER,
  grid_input_w  INTEGER,
  ac_mode       INTEGER,
  min_soc       INTEGER,
  soc_set       INTEGER,
  pack_power_w  REAL,
  bat_v         REAL,
  bat_a         REAL,
  pack_temp_c   REAL,
  dev_temp_c    REAL
);

-- One-minute rollup. Raw rows expire; these are kept indefinitely.
CREATE TABLE IF NOT EXISTS samples_1m (
  ts            INTEGER PRIMARY KEY,
  n             INTEGER NOT NULL,
  shelly_ok_n   INTEGER NOT NULL,
  zendure_ok_n  INTEGER NOT NULL,
  grid_w        REAL,
  grid_w_min    REAL,
  grid_w_max    REAL,
  soc           REAL,
  output_limit  REAL,
  output_home_w REAL,
  pack_input_w  REAL,
  solar_input_w REAL,
  grid_input_w  REAL,
  pack_power_w  REAL,
  bat_v         REAL,
  bat_a         REAL,
  pack_temp_c   REAL,
  dev_temp_c    REAL
);
"""


# --------------------------------------------------------------------------
# config


def load_config(path):
    if not os.path.exists(path):
        sys.exit(
            "Missing %s.\nCopy config/config.example.json to it and fill it in."
            % path
        )
    with open(path) as fh:
        cfg = json.load(fh)
    cfg.setdefault("collector", {})
    c = cfg["collector"]
    c.setdefault("intervalS", 5)
    c.setdefault("timeoutS", 3)
    c.setdefault("commitEveryS", 60)
    c.setdefault("rawRetentionDays", 365)
    c.setdefault("maintenanceEveryS", 3600)
    c.setdefault("healthEveryS", 600)
    c.setdefault("dbPath", os.path.join(ROOT, "data", "zendure.db"))
    return cfg


# --------------------------------------------------------------------------
# polling


def fetch_json(url, timeout, retries=1):
    """Return parsed JSON, or None. One quick retry, then give up for this tick.

    Never raises: a lossy wifi link is the expected condition here, not an
    exceptional one, and a missed sample must not disturb the schedule.
    """
    for _ in range(retries + 1):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except Exception as exc:  # noqa: BLE001 - all failures are equivalent here
            last = exc
    log.debug("fetch failed: %s (%s)", url, last)
    return None


def poll_shelly(cfg):
    """Grid power, normalised so positive means importing."""
    host = cfg["shelly"]["host"]
    t = cfg["collector"]["timeoutS"]
    sign = 1 if cfg["meter"]["importPositive"] else -1

    if cfg["meter"]["profile"] == "triphase":
        st = fetch_json("http://%s/rpc/EM.GetStatus?id=0" % host, t)
        if not st or "total_act_power" not in st:
            return None
        return {
            "grid_w": sign * st["total_act_power"],
            "grid_a_w": sign * st.get("a_act_power"),
            "grid_b_w": sign * st.get("b_act_power"),
            "grid_c_w": sign * st.get("c_act_power"),
        }

    phases = []
    for i in range(3):
        st = fetch_json("http://%s/rpc/EM1.GetStatus?id=%d" % (host, i), t)
        if not st or "act_power" not in st:
            return None
        phases.append(sign * st["act_power"])
    return {
        "grid_w": sum(phases),
        "grid_a_w": phases[0],
        "grid_b_w": phases[1],
        "grid_c_w": phases[2],
    }


def snake(name):
    """CamelCase -> snake_case, safe as a SQL identifier.

    SQLite identifiers are case-insensitive, so `BatVolt` and `batvolt` would
    collide; normalising to one form avoids ever creating both.
    """
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", s)
    return re.sub(r"[^A-Za-z0-9_]", "_", s).lower()


def sql_type(v):
    if isinstance(v, bool):
        return "INTEGER"
    if isinstance(v, int):
        return "INTEGER"
    if isinstance(v, float):
        return "REAL"
    return "TEXT"


def capture_all(rep):
    """Every scalar the device reports, under z_ (device) and zp_ (pack 0).

    Deliberately indiscriminate. The device exposes ~60 scalars; storing the
    lot costs a few hundred bytes a row and means a question asked in six
    months is answerable from data already on disk, rather than needing a
    schema change and then a six-month wait.

    Only pack 0 is captured. With one pack installed that is everything; a
    second pack would need a schema decision rather than silently averaging.
    """
    out = {}
    for k, v in (rep.get("properties") or {}).items():
        if isinstance(v, (dict, list)):
            continue
        out["z_" + snake(k)] = int(v) if isinstance(v, bool) else v
    packs = rep.get("packData") or []
    if packs:
        for k, v in packs[0].items():
            if isinstance(v, (dict, list)):
                continue
            out["zp_" + snake(k)] = int(v) if isinstance(v, bool) else v
    return out


def s16(v):
    """batcur is an unsigned field carrying a signed int16.

    Discharging reads as e.g. 65509, which is -27 (-2.7 A). Read naively it is
    a nonsensical 6.5 kA, and any efficiency computed from it is garbage.
    """
    if v is None:
        return None
    return v - 65536 if v > 32767 else v


def poll_zendure(cfg):
    host = cfg["zendure"]["host"]
    rep = fetch_json(
        "http://%s/properties/report" % host, cfg["collector"]["timeoutS"]
    )
    if not rep or "properties" not in rep:
        return None
    p = rep["properties"]
    out = {
        "soc": p.get("electricLevel"),
        "output_limit": p.get("outputLimit"),
        "output_home_w": p.get("outputHomePower"),
        "pack_input_w": p.get("packInputPower"),
        "solar_input_w": p.get("solarInputPower"),
        "grid_input_w": p.get("gridInputPower"),
        "ac_mode": p.get("acMode"),
        "min_soc": p.get("minSoc"),
        "soc_set": p.get("socSet"),
        "dev_temp_c": p["hyperTmp"] / 100.0 if p.get("hyperTmp") else None,
        "bat_v": p["BatVolt"] / 100.0 if p.get("BatVolt") else None,
    }

    packs = rep.get("packData") or []
    if packs:
        pk = packs[0]
        cur = s16(pk.get("batcur"))
        out["pack_power_w"] = pk.get("power")
        out["bat_a"] = cur / 10.0 if cur is not None else None
        out["pack_temp_c"] = (
            pk["maxTemp"] / 100.0 if pk.get("maxTemp") else None
        )
        if pk.get("totalVol"):
            out["bat_v"] = pk["totalVol"] / 100.0

    out.update(capture_all(rep))
    return out


def sample(cfg):
    """One reading. Each device fails independently; a good half is still kept."""
    shelly = poll_shelly(cfg)
    zendure = poll_zendure(cfg)

    row = {c: None for c in SAMPLE_COLUMNS}
    row["ts"] = int(time.time())
    row["shelly_ok"] = 1 if shelly else 0
    row["zendure_ok"] = 1 if zendure else 0
    if shelly:
        row.update(shelly)
    if zendure:
        row.update(zendure)
    return row


# --------------------------------------------------------------------------
# storage


def open_db(path):
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    # WAL plus NORMAL keeps SD-card write amplification down; combined with
    # batched commits it is roughly a tenth of the flash traffic of the default
    # journal committing per sample.
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    # Must be set before the tables exist, otherwise the incremental_vacuum
    # after each expiry pass is silently a no-op and the file only ever grows.
    db.execute("PRAGMA auto_vacuum=INCREMENTAL")
    db.executescript(SCHEMA)
    # Add columns introduced after a database was first created. ALTER TABLE
    # ADD COLUMN backfills NULL, so existing rows survive with gaps rather than
    # the history being thrown away.
    for table in ("samples", "samples_1m"):
        have = {r[1] for r in db.execute("PRAGMA table_info(%s)" % table)}
        for name, sqltype in MIGRATIONS:
            if name not in have:
                db.execute("ALTER TABLE %s ADD COLUMN %s %s"
                           % (table, name, sqltype))
                log.info("migrated: added %s.%s", table, name)
    db.commit()
    return db


MAX_COLUMNS = 200


def ensure_columns(db, rows):
    """Add columns for keys the schema has not seen yet.

    Lets the device grow new fields across a firmware update without anyone
    editing a schema; they simply start being recorded. Capped so a
    misbehaving device cannot grow the table without bound.
    """
    have = {r[1].lower() for r in db.execute("PRAGMA table_info(samples)")}
    wanted = {}
    for r in rows:
        for k, v in r.items():
            if k.lower() not in have and v is not None:
                wanted.setdefault(k, sql_type(v))
    if not wanted:
        return
    if len(have) + len(wanted) > MAX_COLUMNS:
        log.error("refusing to add %d columns: would exceed the %d cap",
                  len(wanted), MAX_COLUMNS)
        return
    for name, typ in sorted(wanted.items()):
        db.execute('ALTER TABLE samples ADD COLUMN "%s" %s' % (name, typ))
    log.info("added %d columns: %s", len(wanted), ", ".join(sorted(wanted)))


def flush(db, buffer):
    if not buffer:
        return 0
    ensure_columns(db, buffer)
    # Group by key set so a sample that lost a device (and therefore has fewer
    # keys) does not force NULLs onto a batch that has them.
    groups = {}
    for r in buffer:
        groups.setdefault(tuple(sorted(r)), []).append(r)
    n = 0
    for cols, rows in groups.items():
        sql = "INSERT OR REPLACE INTO samples (%s) VALUES (%s)" % (
            ",".join('"%s"' % c for c in cols), ",".join("?" * len(cols)))
        db.executemany(sql, [tuple(r[c] for c in cols) for r in rows])
        n += len(rows)
    db.commit()
    buffer.clear()
    return n


def maintain(db, retention_days):
    """Roll completed minutes into samples_1m, then expire old raw rows.

    Order matters: rolling up before deleting is what stops the expiry from
    destroying data that was never summarised.
    """
    cutoff_minute = (int(time.time()) // 60) * 60  # exclude the current minute
    db.execute(
        """
        INSERT OR REPLACE INTO samples_1m
        SELECT (ts/60)*60,
               COUNT(*), SUM(shelly_ok), SUM(zendure_ok),
               AVG(grid_w), MIN(grid_w), MAX(grid_w),
               AVG(soc), AVG(output_limit), AVG(output_home_w),
               AVG(pack_input_w), AVG(solar_input_w), AVG(grid_input_w),
               AVG(pack_power_w), AVG(bat_v), AVG(bat_a),
               AVG(pack_temp_c), AVG(dev_temp_c)
        FROM samples WHERE ts < ?
        GROUP BY (ts/60)*60
        """,
        (cutoff_minute,),
    )
    expire_before = int(time.time()) - retention_days * 86400
    cur = db.execute("DELETE FROM samples WHERE ts < ?", (expire_before,))
    deleted = cur.rowcount
    db.commit()
    if deleted > 0:
        db.execute("PRAGMA incremental_vacuum")
        db.commit()
    log.info("maintenance: rolled up to %d, expired %d raw rows",
             cutoff_minute, max(deleted, 0))


# --------------------------------------------------------------------------
# run loop


class Stopper:
    def __init__(self):
        self.stop = False
        signal.signal(signal.SIGINT, self._handle)
        signal.signal(signal.SIGTERM, self._handle)

    def _handle(self, *_):
        log.info("shutdown requested, flushing")
        self.stop = True


def run(cfg):
    c = cfg["collector"]
    db = open_db(c["dbPath"])
    stopper = Stopper()
    buffer = []

    stats = {"n": 0, "shelly_fail": 0, "zendure_fail": 0, "written": 0}
    now = time.monotonic()
    next_sample = now
    next_commit = now + c["commitEveryS"]
    # First pass soon after start, but never later than the configured cadence.
    next_maint = now + min(60, c["maintenanceEveryS"])
    next_health = now + c["healthEveryS"]

    log.info("collecting every %ss into %s", c["intervalS"], c["dbPath"])

    while not stopper.stop:
        row = sample(cfg)
        buffer.append(row)
        stats["n"] += 1
        if not row["shelly_ok"]:
            stats["shelly_fail"] += 1
        if not row["zendure_ok"]:
            stats["zendure_fail"] += 1

        now = time.monotonic()
        if now >= next_commit:
            stats["written"] += flush(db, buffer)
            next_commit = now + c["commitEveryS"]
        if now >= next_maint:
            flush(db, buffer)
            maintain(db, c["rawRetentionDays"])
            next_maint = now + c["maintenanceEveryS"]
        if now >= next_health:
            log.info(
                "health: samples=%d shellyFail=%d zendureFail=%d written=%d",
                stats["n"], stats["shelly_fail"], stats["zendure_fail"],
                stats["written"],
            )
            next_health = now + c["healthEveryS"]

        # Monotonic deadlines, so a slow poll does not let the cadence drift.
        # If we fell behind by more than a whole interval, resynchronise rather
        # than sprinting to catch up on samples whose moment has passed.
        next_sample += c["intervalS"]
        delay = next_sample - time.monotonic()
        if delay < 0:
            log.debug("behind schedule by %.1fs, resyncing", -delay)
            next_sample = time.monotonic()
            delay = 0
        # Wake up often enough that SIGTERM is honoured promptly.
        end = time.monotonic() + delay
        while not stopper.stop and time.monotonic() < end:
            time.sleep(min(0.25, end - time.monotonic()))

    n = flush(db, buffer)
    log.info("flushed %d buffered samples on shutdown", n)
    db.close()


def show_stats(cfg):
    db = open_db(cfg["collector"]["dbPath"])
    for table in ("samples", "samples_1m"):
        row = db.execute(
            "SELECT COUNT(*), MIN(ts), MAX(ts) FROM %s" % table
        ).fetchone()
        count, lo, hi = row
        if not count:
            print("%-12s empty" % table)
            continue
        span = (hi - lo) / 3600.0
        print("%-12s %7d rows  %s .. %s  (%.1f h)" % (
            table, count,
            time.strftime("%Y-%m-%d %H:%M", time.localtime(lo)),
            time.strftime("%Y-%m-%d %H:%M", time.localtime(hi)),
            span,
        ))
    ok = db.execute(
        "SELECT AVG(shelly_ok)*100, AVG(zendure_ok)*100 FROM samples"
    ).fetchone()
    if ok[0] is not None:
        print("link success  shelly %.1f%%  zendure %.1f%%" % (ok[0], ok[1]))
    size = os.path.getsize(cfg["collector"]["dbPath"]) / 1e6
    print("db size       %.1f MB" % size)
    db.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true",
                    help="take one sample, print it, do not write")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--config", default=CONFIG_PATH)
    ap.add_argument("--db", help="override collector.dbPath")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    cfg = load_config(args.config)
    if args.db:
        cfg["collector"]["dbPath"] = args.db

    if args.once:
        row = sample(cfg)
        print(json.dumps(row, indent=2))
        if not row["shelly_ok"] or not row["zendure_ok"]:
            sys.exit(1)
        return
    if args.stats:
        show_stats(cfg)
        return
    run(cfg)


if __name__ == "__main__":
    main()
