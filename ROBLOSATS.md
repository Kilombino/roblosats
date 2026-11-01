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

- **Prices** (`api/utils.py`, `nonkyc_rates`): BTCB2 in USD from two venues, NonKYC (its
  BTCB2/USDT and BTCB2/BTC x BTC/USD books count as one venue) and Neoxa. Every quote is the
  order-book midpoint with a spread of at most 5 % (`BTCB2_MAX_SPREAD`), never the last trade.
  The price is the mean of the two venues, and it never moves more than `BTCB2_MAX_STEP`
  (5 %) per refresh. With fewer than `BTCB2_MIN_VENUES` (2) answering, or if they differ by
  more than `BTCB2_MAX_VENUE_DEVIATION` (10 %), the last stored price is kept for
  `BTCB2_SINGLE_VENUE_GRACE` seconds (3600), counted from the last refresh that had both
  venues agreeing; after that the remaining venue (or, if they still disagree, the one closer
  to the stored price) is followed again, still at most 5 % per refresh. The time of that
  last good refresh is kept in Redis (`roblosats:btcb2_quorum_at`); if Redis loses it, the
  hour starts again. With no venue at all the stored price is kept (`BTCB2_FALLBACK_USD` only
  on a first start with nothing stored). Every other fiat currency is that price times
  yadio's USD rate. Set

  ```
  MARKET_PRICE_APIS = 'https://mempool.kilombino.com/nonkyc-ticker'
  NEOXA_TICKER_URL  = 'https://mempool.kilombino.com/neoxa-ticker'
  ```

  Those two are proxies on the Roblosats maintainer's mempool server, because NonKYC and
  Neoxa block Tor exits: using them means trusting that server with your price. A
  coordinator with clearnet access can point straight at the venues instead:

  ```
  MARKET_PRICE_APIS = 'https://api.nonkyc.io/api/v2/ticker/BTCB2_USDT'
  NEOXA_TICKER_URL  = 'https://neoxa.exchange/api/exchange/ticker/BTCB2_USDC'
  ```

  `MARKET_PRICE_APIS` pointing at Neoxa alone (the setting before 22 oct 2026) still works,
  but it is a single venue: switch to the two above.
- **PostgreSQL only**: order actions are serialized with PostgreSQL advisory locks and the
  reward ledger uses PostgreSQL triggers; the coordinator refuses to run order actions on
  any other database.
- **Fee rates** for on-chain payouts come from the chain's own mempool (`MEMPOOL_FEES_URL`,
  default https://mempool.kilombino.com), not mempool.space.
- **Devfund**: the `DEVFUND` fraction of each successful order's proceeds goes by keysend to the
  Roblosats maintainer's node (`devfund_pubkey.json` in this branch), which keeps Roblosats and its
  security fixes going. It is optional: `DEVFUND=0` sends nothing.
- **Trade limits** (`MIN_TRADE`, `MAX_TRADE`) come from the environment: a sat of this chain is
  worth about 1/100 of one on the SHA-256 chain, so the same fiat amounts need more sats.
- **Federation**: its own, starting with Kilobot only; no third-party platforms or lnproxies
  (they are on the other chain).
- **Brand**: name, robot logo and icons, links (GitHub, Telegram community, BLAKE2b LN explorer).
- **Android**: application id `com.roblosats`, so it installs next to RoboSats.
- **Fix**: the web client froze with fewer than three coordinators (it waited forever for a
  third distinct relay).
- **Coordinator admin** (`roblosats_admin`, in the image): django-unfold theme, a dashboard,
  and a **dispute panel** at `/coordinator/api/order/disputes/` and
  `/coordinator/api/order/<id>/dispute/`: who buys and who sells, the chat each party attached,
  their statements, messages from the coordinator to the robots (app notification, Nostr DM,
  Telegram) and buttons that run the existing dispute actions after a confirmation showing who
  receives how many sats. Messages start with `<COORDINATOR_ALIAS> coordinator (order N): `.
  A disputed order's admin page opens the dispute page (`?raw=1` for the Django form).
  The panel follows the browser language (en, es, de, fr, it, pt, ru, zh-Hans, ja) and has a
  language selector.
- `Dockerfile.roblosats` builds the coordinator image with the web client inside.

## Updating to a new RoboSats release

```sh
git fetch upstream --tags
git rebase --onto vX.Y.Z-alpha v0.8.7-alpha roblosats   # previous base → new tag
# rebuild: docker build -f Dockerfile.roblosats -t roblosats:X.Y.Z-rl1 .
```
