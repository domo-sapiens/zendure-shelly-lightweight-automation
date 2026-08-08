# Collector host setup (Raspberry Pi)

Target: Raspberry Pi 3 B+, wired ethernet, Raspberry Pi OS Lite (64-bit).
Everything below uses only what is already in the base image — no `pip install`.

Host-specific values (hostname, user, password, LAN address) live in
`config/pi-access.local.md`, which is gitignored. This file stays generic.

## 1. Flash

Use [Raspberry Pi Imager](https://www.raspberrypi.com/software/) and its OS
customisation dialog to preset hostname, user, SSH public key, locale and
timezone. Choose `Raspberry Pi OS (other)` → `Raspberry Pi OS Lite (64-bit)`.
Skip Wi-Fi; this host is wired.

Set the timezone correctly. Every row is timestamped, and a wrong timezone
only becomes obvious once the charts look subtly wrong.

## 2. First boot

Add a DHCP reservation on the router, then:

```bash
ssh zl@zendure-log
sudo apt update && sudo apt full-upgrade -y
```

Confirm the clock is synchronised before collecting anything. The Pi has no RTC
and boots at epoch zero, so samples taken before NTP lands would be stamped in
1970:

```bash
timedatectl status
```

`System clock synchronized: yes` is what you need.

Then make that true on **every** boot, not just this one:

```bash
sudo systemctl enable --now systemd-time-wait-sync.service
```

This is not optional, and the service unit alone does not cover it. The unit
orders itself `After=time-sync.target`, but that target is reached whether or
not the clock has actually converged — verified by rebooting and finding the
collector already running while `timedatectl` still said
`synchronized: no`. `systemd-time-wait-sync` is the piece that genuinely holds
the target until NTP lands, and it is not enabled by default.

Costs roughly 20 seconds of boot time. Worth it: without it, every reboot risks
a handful of samples stamped at whatever the clock read beforehand, silently
corrupting the series at exactly the moments you most want to inspect.

## 3. Reduce SD card wear

The database uses WAL with batched commits, which keeps write volume modest.
These trim the rest of the background writes:

```bash
sudo systemctl disable --now dphys-swapfile
sudo mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nStorage=volatile\nRuntimeMaxUse=32M\n' | sudo tee /etc/systemd/journald.conf.d/99-volatile.conf
sudo systemctl restart systemd-journald
```

`Storage=volatile` keeps the journal in RAM, so `journalctl` only covers the
current boot. That is the right trade here — the collector's own history lives
in SQLite, not the journal — but remember it when debugging a crash across a
reboot. Use `Storage=persistent` with `SystemMaxUse=64M` instead if you need
logs to survive.

## 4. Install

```bash
git clone https://github.com/domo-sapiens/zendure-shelly-lightweight-automation.git
cd zendure-shelly-lightweight-automation
```

`config/config.local.json` is gitignored, so copy it from the Mac:

```bash
scp config/config.local.json zl@zendure-log:~/zendure-shelly-lightweight-automation/config/
```

Check both devices answer from the Pi before installing the service — this
catches a wrong address or a firewall while it is still easy to see:

```bash
python3 tools/collector.py --once
```

It prints one sample and exits non-zero if either device failed.

## 5. Run it as a service

```bash
sudo cp deploy/zendure-collector.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now zendure-collector
systemctl status zendure-collector
```

The unit hardcodes `User=zl` and the path under `/home/zl`. Edit both if you
used a different user.

`StateDirectory=zendure-log` creates `/var/lib/zendure-log` with the right
owner, which is where `collector.dbPath` points. Do not put the database under
`/home` — `ProtectHome=read-only` in the unit will block writes there.

Watch it:

```bash
journalctl -u zendure-collector -f
```

A `health:` line every 10 minutes reports sample count and per-device failure
counts. Those failure counts are the measurement of the Shelly's weak Wi-Fi
link — check them after a day before drawing conclusions about the control
loop's reliability.

## 6. Check what has accumulated

```bash
python3 tools/collector.py --stats
```

Reports row counts and time span for raw and rolled-up data, per-device link
success rate, and database size.

## Operational notes

- **Restarts are safe.** Buffered samples are flushed on SIGTERM, and at worst
  a restart loses under a minute of data.
- **Raw samples expire after 14 days**; one-minute rollups are kept
  indefinitely. Maintenance runs hourly and rolls up before expiring, so
  expiry never destroys data that was not summarised first.
- **The collector never writes to either device.** It only reads. If it dies,
  the Shelly carries on regulating — that separation is the point of running it
  here instead of on the Shelly.
- **Gaps in logged grid power do not mean the control loop was blind.** The
  collector reads the meter over Wi-Fi; the control script reads it locally over
  the internal bus. Collector gaps measure the collector's link.
