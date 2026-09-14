#!/usr/bin/env python3
"""Archive the collector's database off the Pi so the host can be repurposed.

    tools/archive.py                     # pull, verify, compress, write manifest
    tools/archive.py --list              # what is archived
    tools/archive.py --restore <name>    # decompress an archive for use
    tools/archive.py --browse <name>     # serve the dashboard against an archive

The archive lives OUTSIDE the repository (default ~/zendure-archive) for two
reasons: it must never be committed -- the repo is public and the data is tens
of megabytes -- and it should survive the working copy being deleted or
re-cloned.

Design note: this deliberately produces ONE continuous database rather than
per-era snapshots. When logging resumes, restore the archive onto the new host
and keep appending; the gap in timestamps marks the boundary by itself, and any
comparison is then a date-range selection in the existing dashboard rather than
a second database to switch between. The compressed snapshot is kept as an
immutable reference of the state at archive time.
"""

import argparse
import datetime as dt
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config", "config.local.json")
DEFAULT_ARCHIVE = os.path.expanduser("~/zendure-archive")


def load_config():
    with open(CONFIG_PATH) as fh:
        return json.load(fh)


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, text=True,
                          capture_output=True, **kw).stdout


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def db_stats(path):
    import sqlite3
    db = sqlite3.connect(path)
    db.execute("PRAGMA query_only=1")
    out = {}
    for table in ("samples", "samples_1m"):
        try:
            n, lo, hi = db.execute(
                "SELECT COUNT(*), MIN(ts), MAX(ts) FROM %s" % table).fetchone()
        except sqlite3.Error:
            continue
        out[table] = {
            "rows": n,
            "first_ts": lo, "last_ts": hi,
            "first": dt.datetime.fromtimestamp(lo).isoformat() if lo else None,
            "last": dt.datetime.fromtimestamp(hi).isoformat() if hi else None,
        }
    out["columns"] = [r[1] for r in db.execute("PRAGMA table_info(samples)")]
    out["integrity"] = db.execute("PRAGMA integrity_check").fetchone()[0]
    db.close()
    return out


def pull(host, remote_db, workdir):
    """Copy the database off the host as a clean, self-consistent snapshot.

    VACUUM INTO rather than cp: the live database is in WAL mode, so a plain
    copy of the .db file alone can miss everything still in the -wal, and
    copying all three while the collector writes can capture a torn state.
    VACUUM INTO asks SQLite itself for a consistent, compacted copy while the
    collector keeps running -- no need to stop logging to archive it.
    """
    remote_tmp = "/tmp/zendure-archive-%d.db" % os.getpid()
    print("  asking SQLite on %s for a consistent snapshot..." % host)
    run(["ssh", host, "python3", "-c",
         "import sqlite3,sys;d=sqlite3.connect(sys.argv[1]);"
         "d.execute('VACUUM INTO ?',(sys.argv[2],));d.close()",
         remote_db, remote_tmp])
    local = os.path.join(workdir, "zendure.db")
    print("  copying...")
    run(["scp", "-q", "%s:%s" % (host, remote_tmp), local])
    run(["ssh", host, "rm", "-f", remote_tmp])
    return local


def remote_config(host, path):
    """The config that actually produced this data, not the repo's copy.

    config.local.json is gitignored, so a host can drift from the repository
    without anything flagging it -- and the data is only interpretable
    alongside the settings that generated it.
    """
    try:
        return json.loads(run(["ssh", host, "cat", path]))
    except subprocess.CalledProcessError:
        return None


def cmd_archive(args):
    cfg = load_config()
    archive_dir = args.dir
    os.makedirs(archive_dir, exist_ok=True)

    stamp = dt.datetime.now().strftime("%Y-%m-%d")
    name = args.name or "zendure-%s" % stamp
    workdir = os.path.join(archive_dir, name)
    if os.path.exists(workdir) and not args.force:
        sys.exit("%s already exists (use --force to replace)" % workdir)
    os.makedirs(workdir, exist_ok=True)

    print("archiving %s -> %s" % (args.host, workdir))
    local = pull(args.host, args.remote_db, workdir)

    stats = db_stats(local)
    if stats.get("integrity") != "ok":
        sys.exit("integrity check failed: %s" % stats.get("integrity"))
    print("  integrity: ok")
    for t in ("samples", "samples_1m"):
        if t in stats:
            s = stats[t]
            print("  %-11s %9s rows   %s .. %s"
                  % (t, "{:,}".format(s["rows"]), (s["first"] or "?")[:16],
                     (s["last"] or "?")[:16]))

    raw_sha = sha256(local)
    raw_size = os.path.getsize(local)

    print("  compressing...")
    gz = local + ".gz"
    with open(local, "rb") as fin, gzip.open(gz, "wb", compresslevel=6) as fout:
        shutil.copyfileobj(fin, fout, 1 << 20)
    os.remove(local)
    gz_size = os.path.getsize(gz)

    manifest = {
        "name": name,
        "archived_at": dt.datetime.now().isoformat(timespec="seconds"),
        "source_host": args.host,
        "source_path": args.remote_db,
        "note": args.note,
        "database": {
            "file": os.path.basename(gz),
            "sha256_uncompressed": raw_sha,
            "bytes_uncompressed": raw_size,
            "bytes_compressed": gz_size,
        },
        "stats": stats,
        "config_on_host": remote_config(args.host, args.remote_config),
        "config_in_repo": cfg,
        "restore": "tools/archive.py --restore %s" % name,
    }
    with open(os.path.join(workdir, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=2)

    if manifest["config_on_host"] and manifest["config_on_host"] != cfg:
        print("  NOTE: the host's config differs from the repository's copy.")
        print("        Both are recorded in the manifest; the host's is the one")
        print("        that actually produced this data.")

    print("  %.1f MB -> %.1f MB compressed (%.0f%%)"
          % (raw_size / 1e6, gz_size / 1e6, 100.0 * gz_size / raw_size))
    print("\nwritten to %s" % workdir)
    print("verify any time with: tools/archive.py --verify %s" % name)


def cmd_list(args):
    if not os.path.isdir(args.dir):
        print("no archive directory at %s" % args.dir)
        return
    found = False
    for name in sorted(os.listdir(args.dir)):
        mpath = os.path.join(args.dir, name, "manifest.json")
        if not os.path.exists(mpath):
            continue
        found = True
        m = json.load(open(mpath))
        s = m["stats"].get("samples", {})
        print("%s" % name)
        print("   archived   %s from %s" % (m["archived_at"][:16], m["source_host"]))
        print("   raw rows   %s   %s .. %s"
              % ("{:,}".format(s.get("rows", 0)), (s.get("first") or "?")[:16],
                 (s.get("last") or "?")[:16]))
        print("   size       %.1f MB compressed"
              % (m["database"]["bytes_compressed"] / 1e6))
        if m.get("note"):
            print("   note       %s" % m["note"])
    if not found:
        print("no archives in %s" % args.dir)


def _expand(args, name):
    """Decompress to a working .db next to the archive, if not already there."""
    workdir = os.path.join(args.dir, name)
    mpath = os.path.join(workdir, "manifest.json")
    if not os.path.exists(mpath):
        sys.exit("no archive named %r in %s" % (name, args.dir))
    m = json.load(open(mpath))
    gz = os.path.join(workdir, m["database"]["file"])
    out = gz[:-3]
    if not os.path.exists(out):
        print("  decompressing %s..." % os.path.basename(gz))
        with gzip.open(gz, "rb") as fin, open(out, "wb") as fout:
            shutil.copyfileobj(fin, fout, 1 << 20)
    return m, out


def cmd_verify(args):
    m, path = _expand(args, args.verify)
    got = sha256(path)
    want = m["database"]["sha256_uncompressed"]
    print("  expected %s" % want)
    print("  actual   %s" % got)
    if got != want:
        sys.exit("CHECKSUM MISMATCH -- the archive is damaged")
    stats = db_stats(path)
    print("  integrity: %s" % stats["integrity"])
    print("  rows: %s raw, %s rolled up"
          % ("{:,}".format(stats["samples"]["rows"]),
             "{:,}".format(stats["samples_1m"]["rows"])))
    print("  OK")


def cmd_restore(args):
    m, path = _expand(args, args.restore)
    print("\nrestored to:\n  %s" % path)
    print("\nTo resume logging on a new host, copy this file to its")
    print("collector.dbPath and start the collector -- it will append. The gap")
    print("in timestamps marks where the old data ends.")


def cmd_browse(args):
    m, path = _expand(args, args.browse)
    cfg = load_config()
    cfg["collector"]["dbPath"] = path
    cfg["dashboard"]["port"] = args.port
    tmpcfg = os.path.join(os.path.dirname(path), "browse-config.json")
    with open(tmpcfg, "w") as fh:
        json.dump(cfg, fh, indent=2)
    print("serving the archive at http://localhost:%d/  (Ctrl-C to stop)" % args.port)
    print("note: 'last sample' will read as stale -- the data is historical.")
    os.execv(sys.executable,
             [sys.executable, os.path.join(ROOT, "tools", "dashboard.py"),
              "--config", tmpcfg])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=DEFAULT_ARCHIVE,
                    help="archive directory (default: %s)" % DEFAULT_ARCHIVE)
    ap.add_argument("--host", default="zl@zendure-log")
    ap.add_argument("--remote-db", default="/var/lib/zendure-log/zendure.db")
    ap.add_argument("--remote-config",
                    default="/home/zl/zendure-shelly-lightweight-automation/"
                            "config/config.local.json")
    ap.add_argument("--name", help="archive name (default: zendure-<date>)")
    ap.add_argument("--note", default="", help="why this snapshot exists")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--verify", metavar="NAME")
    ap.add_argument("--restore", metavar="NAME")
    ap.add_argument("--browse", metavar="NAME")
    ap.add_argument("--port", type=int, default=8099)
    args = ap.parse_args()

    if args.list:
        return cmd_list(args)
    if args.verify:
        return cmd_verify(args)
    if args.restore:
        return cmd_restore(args)
    if args.browse:
        return cmd_browse(args)
    return cmd_archive(args)


if __name__ == "__main__":
    main()
