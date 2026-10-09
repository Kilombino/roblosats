# Roblosats

Roblosats is [RoboSats](https://github.com/RoboSats/robosats) for Bitcoin on the **BLAKE2b
chain**: the same peer-to-peer exchange (robots, Lightning hold invoices as escrow and bonds,
PGP chat, Tor), running on that chain's own Lightning network.

- Web: https://roblosats.kilombino.com and
  `http://roblowuc54m3gjphogvbpcqrbwffedaayji2x243w5ei2h4wwsj2rbid.onion`
- First coordinator: **Kilobot**, `http://kiloblo3ibbey3wridofauizwb467y5osfaycxoecgsf66mggsrw6ryd.onion`
  (clearnet: https://kilobot.kilombino.com)
- Community: https://t.me/roblosats
- Coordinator deployment: https://github.com/Kilombino/roblosats-deploy (branch `roblosats`)
- Maintainer: Kilombino

## What is different from RoboSats

All changes live on the `roblosats` branch as a small set of commits on top of an upstream
release tag, so each new RoboSats release can be taken by rebasing that branch.

- **Prices** (`api/utils.py`): BTCB2 from Neoxa (the exchange where it trades), in USD; every
  other fiat currency is that price times yadio's USD rate. Set
  `MARKET_PRICE_APIS=https://mempool.kilombino.com/neoxa-ticker` (a proxy that works over Tor).
- **Fee rates** for on-chain payouts come from the chain's own mempool (`MEMPOOL_FEES_URL`,
  default https://mempool.kilombino.com), not mempool.space.
- **No devfund keysend** when `DEVFUND=0` (RoboSats' devfund node is not on this Lightning network).
- **Trade limits** (`MIN_TRADE`, `MAX_TRADE`) come from the environment: a sat of this chain is
  worth about 1/100 of one on the SHA-256 chain, so the same fiat amounts need more sats.
- **Federation**: its own, starting with Kilobot only; no third-party platforms or lnproxies
  (they are on the other chain).
- **Brand**: name, robot logo and icons, links (GitHub, Telegram community, BLAKE2b LN explorer).
- **Android**: application id `com.roblosats`, so it installs next to RoboSats.
- **Fix**: the web client froze with fewer than three coordinators (it waited forever for a
  third distinct relay).
- `Dockerfile.roblosats` builds the coordinator image with the web client inside.

## Updating to a new RoboSats release

```sh
git fetch upstream --tags
git rebase --onto vX.Y.Z-alpha v0.8.7-alpha roblosats   # previous base → new tag
# rebuild: docker build -f Dockerfile.roblosats -t roblosats:X.Y.Z-rl1 .
```
