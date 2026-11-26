#!/bin/sh
# Configure a persistent, locally-administered Bluetooth public BD address
# derived from /etc/machine-id (or read from /etc/bluetooth/pista-bdaddr).
set -eu

CONF=/etc/bluetooth/pista-bdaddr
if [ -s "$CONF" ]; then
    BDADDR=$(tr -d " \t\r\n" < "$CONF")
else
    mkdir -p /etc/bluetooth
    MID=$(cat /etc/machine-id 2>/dev/null || echo "5049535441303031")
    H=$(printf "%s-pista-bt" "$MID" | sha256sum | cut -c1-12)
    B0=$(printf "%02X" $(( (0x$(echo "$H" | cut -c1-2) & 0xFE) | 0x02 )))
    B1=$(echo "$H" | cut -c3-4 | tr "a-f" "A-F")
    B2=$(echo "$H" | cut -c5-6 | tr "a-f" "A-F")
    B3=$(echo "$H" | cut -c7-8 | tr "a-f" "A-F")
    B4=$(echo "$H" | cut -c9-10 | tr "a-f" "A-F")
    B5=$(echo "$H" | cut -c11-12 | tr "a-f" "A-F")
    BDADDR="${B0}:${B1}:${B2}:${B3}:${B4}:${B5}"
    echo "$BDADDR" > "$CONF"
fi

for _ in 1 2 3 4 5 6 7 8 9 10; do
    if btmgmt --index 0 config 2>/dev/null | grep -q "missing options:.*public-address"; then
        btmgmt --index 0 public-addr "$BDADDR" && exit 0
    elif btmgmt --index 0 info 2>/dev/null | grep -q "Primary controller"; then
        exit 0
    fi
    sleep 0.5
done
