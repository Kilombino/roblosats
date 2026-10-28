#!/bin/sh

# Roblosats self-hosted client: one container that serves the app and reaches every coordinator
# of the federation over Tor.
# 1) socat: one Tor bridge per coordinator, 127.0.0.1:<port> → its onion (TOR_PROXY_IP:TOR_PROXY_PORT).
# 2) nginx: the app, and /mainnet/<alias>/{api,ws,relay,blossom,avatars} to that coordinator's bridge.
# The coordinators come from the federation the app ships with (static/federation.json): a coordinator
# added to the federation needs no change here.

set -e
FED=/usr/src/robosats/static/federation.json
CONF=/etc/nginx/conf.d
mkdir -p "$CONF"
rm -f "$CONF"/*.upstream.conf "$CONF"/*.locations.conf

port=101
jq -r 'to_entries[] | select(.value.federated == true and (.value.mainnet.onion // "") != "")
       | "\(.key) \(.value.mainnet.onion | sub("^https?://"; "") | sub("/.*$"; ""))"' "$FED" |
while read -r alias onion; do
    case "$alias" in *[!a-z0-9]*) echo "skipping coordinator with a bad alias: $alias"; continue ;; esac
    case "$onion" in *.onion) ;; *) echo "skipping $alias: not an onion"; continue ;; esac
    printf 'upstream mainnet_%s {\n    server 127.0.0.1:%s;\n}\n' "$alias" "$port" > "$CONF/$alias.upstream.conf"
    sed -e "s/ALIAS/$alias/g" -e "s/PORT/$port/g" "$CONF/locations.template" > "$CONF/$alias.locations.conf"
    echo "$alias $onion $port" >> /tmp/bridges
    port=$((port + 1))
done

# PLAIN_HTTP=1: the platform (StartOS, Umbrel) puts its own HTTPS in front; plain HTTP inside it.
if [ "${PLAIN_HTTP:-0}" = 1 ]; then
    sed -i -e 's/listen 12596 ssl;/listen 12596;/' -e '/ssl_certificate/d' -e '/ssl_protocols/d' -e '/error_page 497/d' /etc/nginx/nginx.conf
fi

mkdir -p /etc/nginx/ssl
if [ ! -f /etc/nginx/ssl/server.crt ]; then
    openssl req -x509 -nodes -newkey rsa:2048 -days 3650 \
        -keyout /etc/nginx/ssl/server.key -out /etc/nginx/ssl/server.crt \
        -subj "/CN=roblosats_client" -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"
fi

while read -r alias onion port; do
    echo "bridge $alias → $onion on 127.0.0.1:$port"
    socat "tcp4-LISTEN:${port},reuseaddr,fork,keepalive,bind=127.0.0.1" \
        "SOCKS5-CONNECT:${TOR_PROXY_IP:-127.0.0.1}:${onion}:80,socksport=${TOR_PROXY_PORT:-9050}" &
done < /tmp/bridges
rm -f /tmp/bridges

exec nginx
