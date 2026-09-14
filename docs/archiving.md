# Archiving the data, and resuming later

The logging host is a Raspberry Pi that may be needed for something else. This
describes how to take the data off it, keep it safely on a workstation, and pick
logging back up later without losing the ability to compare against what came
before.

## The shape of the plan

**One continuous database, plus an immutable snapshot.**

When logging resumes, restore the archive onto the new host and let the
collector keep appending to it. The gap in timestamps marks the boundary by
itself, so "before and after" is just a date-range selection in the dashboard
you already have. The compressed snapshot stays untouched as a reference of the
state at archive time.

The alternative — a separate database per era — was rejected. It forces a
`dbPath` switch to look at the other era, the dashboard can only show one at a
time, and every comparison becomes a manual join. A single series with a visible
gap is strictly easier to work with, and the charts already break lines across
gaps rather than interpolating.

## Where it lives

Outside the repository, at `~/zendure-archive` by default. Two reasons:

- **It must never be committed.** The repository is public and the database runs
  to tens of megabytes. `.gitignore` also blocks `*.db` and `*.db.gz` as a
  second line of defence, but keeping the data out of the working copy entirely
  is the real protection.
- **It should outlive the working copy**, which may be deleted or re-cloned.

## Taking the archive

```bash
tools/archive.py --note "before repurposing the Pi; original panel position"
```

That will:

1. Ask SQLite **on the Pi** for a consistent snapshot via `VACUUM INTO`.
   Not `cp`: the database is in WAL mode, so copying the `.db` alone can miss
   everything still sitting in the `-wal`, and copying all three files while the
   collector is writing can capture a torn state. `VACUUM INTO` produces a
   clean, compacted copy **while the collector keeps running** — no need to stop
   logging to archive it.
2. Run `PRAGMA integrity_check` and refuse to continue if it fails.
3. Record a SHA-256 of the uncompressed database, then gzip it.
4. Write a `manifest.json` with row counts, date ranges, the schema's column
   list, and **both** configs — the one on the host and the one in the
   repository.

That last point matters. `config.local.json` is gitignored, so a host can drift
from the repository without anything flagging it, and the data is only
interpretable next to the settings that actually produced it. If the two differ,
the tool says so.

## Checking it later

```bash
tools/archive.py --list
tools/archive.py --verify zendure-2026-09-14
```

`--verify` decompresses, re-hashes, compares against the manifest, and runs an
integrity check. Worth doing occasionally: bit rot on a long-lived file is quiet
until the day you need the data.

## Reading the data without the Pi

```bash
tools/archive.py --browse zendure-2026-09-14
```

Serves the normal dashboard at `http://localhost:8099` against the archive, so
every chart and the efficiency analysis keep working after the hardware is gone.
The "last sample" age will read as stale — the data is historical, and that is
the honest thing for it to say.

## Resuming later

1. Set the new host up per [pi-setup.md](pi-setup.md).
2. Restore the database and put it at `collector.dbPath`:

   ```bash
   tools/archive.py --restore zendure-2026-09-14
   scp ~/zendure-archive/zendure-2026-09-14/zendure.db \
       <user>@<host>:/var/lib/zendure-log/zendure.db
   ```

3. **Copy `config/config.local.json` to the host as well.** It is gitignored, so
   `git pull` will not carry it, and a host running a stale copy is exactly the
   failure this project already hit once: the dashboard displayed a target and
   deadband a month out of date, and raw samples expired at 14 days because the
   file still said so.
4. Start the collector. It appends; the gap speaks for itself.

Take a fresh archive before any subsequent migration, rather than overwriting an
existing one.

## Retention, before you archive

`collector.rawRetentionDays` governs how long 5-second raw samples survive;
one-minute rollups are kept indefinitely regardless. Check the value **on the
host**, not in the repository, and raise it before archiving if you want to keep
fine detail — anything already expired is gone.
