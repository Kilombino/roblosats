## Roblosats - Buy and sell Bitcoin (BLAKE2b) privately
[![release](https://img.shields.io/github/v/release/Kilombino/roblosats)](https://github.com/Kilombino/roblosats/releases)
[![AGPL-3.0 license](https://img.shields.io/badge/license-AGPL--3.0-blue)](https://github.com/Kilombino/roblosats/blob/roblosats/LICENSE)
[![Telegram](https://img.shields.io/badge/chat-telegram-brightgreen)](https://t.me/roblosats)

Roblosats is a simple and private way to exchange bitcoin on the BLAKE2b chain (BTC) for national currencies, and for Spamcoin, peer to peer. It is [RoboSats](https://github.com/RoboSats/robosats) for the BLAKE2b chain: the same robots, the same Lightning hold invoices that minimize custody and trust, the same privacy. The generated robot avatars help users stick to best privacy practices. See [ROBLOSATS.md](ROBLOSATS.md) for what changes and how it follows upstream.

<div align="center">
  <img width="35%" alt="Roblosats" src="https://raw.githubusercontent.com/Kilombino/roblosats/roblosats/fastlane/metadata/en-US/images/icon.png">
</div>

## Try it out!
- **Web:** [roblosats.kilombino.com](https://roblosats.kilombino.com)
- **Tor:** [roblowuc54m3gjphogvbpcqrbwffedaayji2x243w5ei2h4wwsj2rbid.onion](http://roblowuc54m3gjphogvbpcqrbwffedaayji2x243w5ei2h4wwsj2rbid.onion) (open with [Tor Browser](https://www.torproject.org/download/))
- **Community:** [t.me/roblosats](https://t.me/roblosats)

*Use Tor Browser and the .onion for the best privacy.*

Roblosats for Android: from [GitHub releases](https://github.com/Kilombino/roblosats/releases) (arm64-v8a for almost every recent phone) or [Zapstore](https://zapstore.dev).

On your own node:
- **Umbrel:** community app store `https://github.com/Kilombino/umbrel-store` → Roblosats
- **StartOS:** registry `https://registry.kilombino.com` → Roblosats

## Coordinators

Roblosats is run by a federation of coordinators, each with its own Lightning node on the BLAKE2b chain. The app joins all their order books: Kilobot, marcanoTrades and Rose Garden today. To run one, see [roblosats-deploy](https://github.com/Kilombino/roblosats-deploy) and its [coordinator guide](https://github.com/Kilombino/roblosats-deploy/blob/roblosats/COORDINATOR.md); to join the federation, open an issue here with your alias, onion, Nostr public key, node id and the output of `lncli signmessage "Roblosats federation: <your onion host>"`.

## How to use it

Roblosats works as RoboSats does, so RoboSats' guides apply (with BTC on the BLAKE2b chain where they say bitcoin):
- **Written guides:** [English](https://learn.robosats.org/read/en) · [Español](https://learn.robosats.org/read/es) · [Deutsch](https://learn.robosats.org/read/de) · [Français](https://learn.robosats.org/read/fr) · [Русский](https://learn.robosats.org/read/ru)
- **Video guides:** [English](https://learn.robosats.org/watch/en/) · [Español](https://learn.robosats.org/watch/es/) · [Deutsch](https://learn.robosats.org/watch/de) · [Português](https://learn.robosats.org/watch/pt) · [Polski](https://learn.robosats.org/watch/pl) · [Français](https://learn.robosats.org/watch/fr) · [Русский](https://learn.robosats.org/watch/ru)

## How it works

Alice wants to buy bitcoin privately:
1. Alice generates an avatar (AdequateAlice01) using her private random token.
2. Alice stores safely the token in case she needs to recover AdequateAlice01 in the future.
3. Alice makes a new order and locks a small hold invoice to publish it (maker bond).
4. Bob wants to sell, sees Alice's order in the book and takes it.
5. Bob scans a small hold invoice as his taker bond. The contract is final.
6. Bob posts the traded sats with a hold invoice, while Alice submits her payout invoice (or an on-chain address).
7. On a private chat, Bob tells Alice how to send him fiat.
8. Alice pays Bob, then they confirm the fiat has been sent and received.
9. Bob's trade hold invoice is charged and the sats are sent to Alice.
10. Bob and Alice's bonds return automatically, since they complied with the rules.
11. The bonds would be charged (lost) in case of unilateral cancellation or cheating (lost dispute).

## Contribute

Check [/development](/development). Roblosats keeps its own patches on top of each RoboSats release ([ROBLOSATS.md](ROBLOSATS.md)); fixes that also apply to RoboSats are offered upstream.

## Credits
Roblosats is a fork of [RoboSats](https://github.com/RoboSats/robosats), the Robotic Satoshis Open Source Project, whose concept of a simple custody-minimized lightning exchange with hold invoices is inspired in [P2PLNBOT](https://github.com/grunch/p2plnbot) by @grunch.

## License

Roblosats, like the Robotic Satoshis Open Source Project it comes from, is released under the terms of the AGPL3.0 license. See [LICENSE](LICENSE) for more details.
