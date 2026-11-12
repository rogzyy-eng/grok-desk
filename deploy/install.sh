#!/usr/bin/env bash
# Grok desk installer for Ubuntu 24.04 (run as root in the DigitalOcean Droplet Console).
# Safe to re-run: keeps existing secrets and data. Starts the desk in PAPER mode.
# Secrets (APP_TOKEN, VAPID private key) are generated HERE on the server into
# /etc/grok-desk/desk.env (root-only, mode 600) and are never printed to the console or logs.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
SRC_DIR="${GROK_DESK_SRC:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
APP=/opt/grok-desk; DATA=/var/lib/grok-desk; ETC=/etc/grok-desk
say(){ printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
[ "$(id -u)" = 0 ] || { echo "Run as root"; exit 1; }
. /etc/os-release; [ "${VERSION_ID:-}" = "24.04" ] || echo "warning: tested on Ubuntu 24.04, found ${VERSION_ID:-?}"

say "1/8 System packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip curl ufw gnupg debian-keyring debian-archive-keyring apt-transport-https ca-certificates >/dev/null

say "2/8 Caddy (automatic HTTPS)"
if ! command -v caddy >/dev/null; then
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq && apt-get install -y -qq caddy >/dev/null
fi

say "3/8 App user and code"
id desk >/dev/null 2>&1 || useradd --system --home "$DATA" --shell /usr/sbin/nologin desk
mkdir -p "$APP" "$DATA" "$ETC"
[ "$(readlink -f "$SRC_DIR")" = "$APP" ] || cp -a "$SRC_DIR"/. "$APP"/
rm -rf "$APP/data" "$APP/proof" "$APP/dist"
chown -R root:root "$APP"; chmod -R a+rX "$APP"
chown -R desk:desk "$DATA"; chmod 750 "$DATA"

say "4/8 Python venv + dependencies"
[ -x "$APP/.venv/bin/python" ] || python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install -q --upgrade pip
"$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"
"$APP/.venv/bin/python" -c 'import sys; print("python", sys.version.split()[0])'

say "5/8 Address + secrets"
IP="$(curl -fs -m 3 http://169.254.169.254/metadata/v1/interfaces/public/0/ipv4/address || true)"
[ -n "$IP" ] || IP="$(curl -fs -m 5 https://api.ipify.org || true)"
[ -n "$IP" ] || IP="167.172.98.187"
HOST="${IP//./-}.sslip.io"
umask 077
if [ ! -s "$ETC/desk.env" ]; then
  "$APP/.venv/bin/python" - "$ETC/desk.env" "$HOST" <<'PY'
import secrets, sys
sys.path.insert(0, "/opt/grok-desk")
from webpush import generate_vapid_pair
priv, pub = generate_vapid_pair()
host = sys.argv[2]
with open(sys.argv[1], "w") as f:
    f.write(f"APP_TOKEN={secrets.token_urlsafe(48)}\nVAPID_PRIVATE_KEY={priv}\nVAPID_PUBLIC_KEY={pub}\n"
            f"VAPID_SUB=https://{host}\nPUBLIC_URL=https://{host}\nTG_TOKEN=\nTG_CHAT_ID=\nTYPESAFE_API_KEY=\n")
PY
  echo "secrets generated (not shown) -> $ETC/desk.env"
else
  echo "keeping existing $ETC/desk.env"
fi
chown root:root "$ETC" "$ETC/desk.env"; chmod 700 "$ETC"; chmod 600 "$ETC/desk.env"
umask 022
cat > /usr/local/sbin/grok-desk-url <<'SH'
#!/bin/sh
# Prints the private app link (contains your access code). Root only.
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }
. /etc/grok-desk/desk.env
echo "${PUBLIC_URL}/#t=${APP_TOKEN}"
SH
chown root:root /usr/local/sbin/grok-desk-url; chmod 700 /usr/local/sbin/grok-desk-url

say "6/8 Caddy site https://$HOST"
cat > /etc/caddy/Caddyfile <<CADDY
{
	admin localhost:2019
}
$HOST {
	encode gzip
	header -Server
	reverse_proxy 127.0.0.1:8787 {
		flush_interval -1
	}
}
# Fallback: a Let's Encrypt IP certificate (6-day 'shortlived' profile)
$IP {
	tls {
		issuer acme {
			profile shortlived
		}
	}
	reverse_proxy 127.0.0.1:8787 {
		flush_interval -1
	}
}
CADDY
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null
systemctl enable caddy >/dev/null 2>&1; systemctl restart caddy

say "7/8 Firewall (22, 80, 443)"
ufw allow 22/tcp >/dev/null; ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null; ufw status | sed -n '1,8p'

say "8/8 Desk service (PAPER mode)"
install -m 644 "$APP/deploy/grok-desk.service" /etc/systemd/system/grok-desk.service
systemctl daemon-reload; systemctl enable grok-desk >/dev/null 2>&1; systemctl restart grok-desk
for i in $(seq 1 30); do curl -fs -m 2 http://127.0.0.1:8787/healthz >/dev/null && break; sleep 2; done
curl -fs -m 2 http://127.0.0.1:8787/healthz >/dev/null && echo "desk: running" || { echo "desk failed to start:"; journalctl -u grok-desk -n 30 --no-pager; exit 1; }
printf 'HTTPS certificate: '
OK=""
for i in $(seq 1 45); do
  if curl -fs -m 5 "https://$HOST/healthz" >/dev/null; then OK=1; break; fi; sleep 4
done
[ -n "$OK" ] && echo "ok (https://$HOST)" || echo "still pending — check: journalctl -u caddy -n 40 --no-pager"

cat <<DONE

============================================================
 Grok desk is installed and running in PAPER mode.
 App address : https://$HOST
 To show your private app link (with access code) run:
     grok-desk-url
 Status      : systemctl status grok-desk --no-pager
 Logs        : journalctl -u grok-desk -f
 The desk never places orders and never asks for your FOMO password.
============================================================
DONE
