# /nodeapp/coordinators — Coordinator bridges of the self-hosted client

Roblosats writes no per-coordinator config by hand. At start, `robosats-client.sh` reads the
federation the app ships with (`static/federation.json`) and, for each federated coordinator with
a mainnet onion, in order:

- starts a socat Tor bridge on `127.0.0.1:<port>` (101, 102, … in federation order) to its onion;
- writes `/etc/nginx/conf.d/<alias>.upstream.conf` (`upstream mainnet_<alias>`) and
  `<alias>.locations.conf` from `locations.template` (ALIAS and PORT replaced): avatars, api, ws,
  relay and blossom, plus a 403 for the coordinator's admin panel.

`nginx.conf` includes `*.upstream.conf` and `*.locations.conf`. Adding a coordinator to the
federation (see roblosats-deploy `tools/add_coordinator.py`) is all it takes; mainnet only.
