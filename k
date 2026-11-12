#!/bin/bash
# Set a new APP_TOKEN chosen by the owner. Nothing is printed or logged.
set -e
read -r -p "Yeni kod (en az 20 karakter: a-z 0-9 -): " C
if ! [[ "$C" =~ ^[A-Za-z0-9_-]{20,}$ ]]; then echo "Gecersiz: en az 20 karakter, sadece harf/rakam/tire."; exit 1; fi
sed -i "s/^APP_TOKEN=.*/APP_TOKEN=$C/" /etc/grok-desk/desk.env
systemctl restart grok-desk
sleep 3
systemctl is-active grok-desk >/dev/null && echo "Tamam. Kod degisti, uygulamada bu kodu gir." || echo "Servis baslamadi: journalctl -u grok-desk -n 30"
history -c 2>/dev/null || true
