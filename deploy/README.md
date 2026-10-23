# Running BW Departures on a server

How the public site runs (https://bw-departures.duckdns.org) and how to set it
up again on a new server. Everything here is copied from the running server;
nothing in this folder is secret.

```
visitor ── https ──▶ Caddy (certificate, http → https) ──▶ the app (Waitress, 127.0.0.1:8000, user bw)
                     /etc/caddy/Caddyfile                    /srv/bw/app  (this repository)
                                                             └─▶ the timetable service EFA
```

| File here | Goes to | What it does |
|---|---|---|
| `Caddyfile` | `/etc/caddy/Caddyfile` | https for the site's name, everything passed to the app; waits for the app during an update instead of showing an error; no server names in answers; browsers use https only (a year) |
| `bw-departures.service` | `/etc/systemd/system/` | runs the app (`serve.py`) as user `bw`, starts it at boot, restarts it if it stops; it may write only its stop data |
| `bw-stops-import.service`, `.timer` | `/etc/systemd/system/` | rebuilds the stop data from NVBW every Wednesday about 04:00 (keeps the old data if the download fails) |
| `bw-update` | `/usr/local/bin/bw-update` | the update command, see below |
| `52bw-reboot` | `/etc/apt/apt.conf.d/` | the automatic security updates may restart the server at 04:30 when an update needs it |
| `../.env.example` | `/srv/bw/app/.env` | the app's server settings |

## Day to day

Log in: `ssh -i ~/.ssh/bw_strato root@<server address>`

| Command | |
|---|---|
| `bw-update` | fetch the newest version from GitHub (`main`), run the unit tests on it, switch, check that the site answers; if it does not, the version before comes back by itself |
| `bw-update rollback` | back to the version before the last switch (again: forward again) |
| `bw-update status` | which version runs and whether the app answers |
| `journalctl -u bw-departures -n 50` | the app's last log lines |
| `systemctl start bw-stops-import` | new stop data now (otherwise Wednesday night) |
| `tail /srv/bw/update.log` | every update and rollback |

When a file in this folder changes, after `bw-update` copy it to its place:
`install -m 755 /srv/bw/app/deploy/bw-update /usr/local/bin/`, or for a service
file `cp /srv/bw/app/deploy/<file> /etc/systemd/system/ && systemctl daemon-reload`,
for the Caddyfile `cp ... /etc/caddy/Caddyfile && caddy validate --config /etc/caddy/Caddyfile && systemctl reload caddy`.

## A new server, step by step

Needed: a VPS with Ubuntu 24.04, an SSH key that logs in as root, a name
(e.g. DuckDNS) pointing to the server's IPv4 (and IPv6) address, and the name
registered with Stadia Maps (otherwise the map tiles are refused, error 401).
About half an hour. As root on the server:

**1. The system**
```bash
apt-get update && apt-get -y full-upgrade
timedatectl set-timezone Europe/Berlin
ufw allow OpenSSH && ufw allow 80/tcp && ufw allow 443/tcp && ufw allow 443/udp && ufw --force enable
fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile && echo "/swapfile none swap sw 0 0" >> /etc/fstab
apt-get install -y python3-venv git curl
```
(Automatic security updates are on in Ubuntu already; `52bw-reboot` comes in step 3.)

**2. The app, as its own user**
```bash
useradd --system --create-home --home-dir /srv/bw --shell /usr/sbin/nologin bw
cd /srv/bw && sudo -u bw git clone https://github.com/muhammad-guluzade/BWTransportManagementSystem.git app
sudo -u bw python3 -m venv /srv/bw/venv
sudo -u bw /srv/bw/venv/bin/pip install -r /srv/bw/app/requirements.txt
cd /srv/bw/app && sudo -u bw cp .env.example .env && chmod 600 .env
sudo -u bw /srv/bw/venv/bin/python -m unittest          # must end with OK
sudo -u bw /srv/bw/venv/bin/python import_stops.py      # about a minute
```

**3. Services, update command, night restarts**
```bash
cd /srv/bw/app/deploy
cp bw-departures.service bw-stops-import.service bw-stops-import.timer /etc/systemd/system/
install -m 755 bw-update /usr/local/bin/
cp 52bw-reboot /etc/apt/apt.conf.d/
systemctl daemon-reload && systemctl enable --now bw-departures bw-stops-import.timer
bw-update status                                        # Site: answers
```

**4. Caddy (https)**
```bash
apt-get install -y debian-keyring debian-archive-keyring apt-transport-https gnupg
curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
chmod o+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg /etc/apt/sources.list.d/caddy-stable.list
apt-get update && apt-get install -y caddy
cp /srv/bw/app/deploy/Caddyfile /etc/caddy/Caddyfile    # another name? change the first line
caddy validate --config /etc/caddy/Caddyfile && systemctl reload caddy
journalctl -u caddy -n 30 --no-pager | grep -i "certificate obtained"
```

**5. Check from outside**: the site opens with https and the map shows; `http://`
goes on to `https://`; `/api/v1/` answers.

## Tests

The server was checked on 21-22 October 2026 (Python 3.12 on Ubuntu 24.04):
unit tests, the browser test suites through an SSH tunnel, attacks against
the public address, 60 visitors at once, a 25-minute soak, every way through
`bw-update`, a full restart. `bw-update` has a switch for testing its way
back: `BW_PRETEND_BROKEN=<version> bw-update` treats that version as not
answering.
