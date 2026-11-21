"""0.9.12: payments sent to an on-chain bond or escrow funding address on the WRONG chain.

Kilobot order #28 (21 nov 2026): a taker paid its 30,000-sat on-chain bond with Spamcoin, on the
SHA-256 chain ("Spamchain"). Both chains use the same addresses, so the wallet accepted it, but
the coordinator's node (BLAKE2b) never saw it and the take expired. The coins are not lost: the
bond's refund leaf (owner alone after refund_csv blocks) and the funding address (the owner's key)
work on that chain too, and the app's "Recover without the coordinator" can now spend them there.

This module watches, through an Esplora API of the SHA-256 chain (SPAMCHAIN_ESPLORA_URL, default
mempool.space; over Tor when the coordinator uses Tor; SPAMCHAIN_WATCH=False turns it off), the
addresses of on-chain bonds and escrow funding addresses still in play, and when something lands
there: logs it on the order, tells the robot (Telegram/Nostr/webhook) how to take it back, and
alerts the coordinator (Telegram, on-chain group). Each payment (outpoint) is reported once
(OnchainBond.wrong_chain / OnchainEscrow.wrong_chain).

The coordinator never signs anything for these coins: a cooperative return on the other chain
would need a signature that the BLAKE2b chain may also accept (its anti-replay sighash is
opt-in), so a robot could use it to take back a real bond on our chain. Only the owner-alone
paths are offered (see the app's rescue tool)."""

import logging
import time
from datetime import timedelta

from decouple import config
from django.utils import timezone

logger = logging.getLogger(__name__)

DEFAULT_ESPLORA = "https://mempool.space/api"


def enabled():
    return config("SPAMCHAIN_WATCH", cast=bool, default=True)


def esplora_base():
    return (config("SPAMCHAIN_ESPLORA_URL", default=DEFAULT_ESPLORA) or DEFAULT_ESPLORA).rstrip("/")


def _session():
    import requests

    s = requests.Session()
    use_tor = config("SPAMCHAIN_USE_TOR", cast=bool, default=config("USE_TOR", cast=bool, default=True))
    if use_tor:
        proxy = "socks5h://" + config("TOR_PROXY", default="127.0.0.1:9050")
        s.proxies = {"http": proxy, "https": proxy}
    return s


def utxos(address, session=None):
    """[(txid, vout, sats, height|None)] at this address on the SHA-256 chain."""
    s = session or _session()
    r = s.get(f"{esplora_base()}/address/{address}/utxo", timeout=40)
    if r.status_code == 404:
        return []
    r.raise_for_status()
    out = []
    for u in r.json():
        st = u.get("status") or {}
        out.append((u["txid"], int(u["vout"]), int(u["value"]), st.get("block_height") if st.get("confirmed") else None))
    return out


def watched(now=None):
    """(obj, order, owner_user, what, role, address, refund_csv) of every address still worth
    watching: bonds not penalized/returned/in a deposit, created in the last
    SPAMCHAIN_WATCH_DAYS (14), and the funding addresses of escrows not deposited yet."""
    from api.escrow import service
    from api.escrow.addresses import script_to_address
    from api.models import OnchainBond, OnchainEscrow

    now = now or timezone.now()
    since = now - timedelta(days=config("SPAMCHAIN_WATCH_DAYS", cast=int, default=14))
    B = OnchainBond.Status
    for b in OnchainBond.objects.filter(
        created_at__gte=since, status__in=[B.FUNDING, B.SIGN, B.LOCKED, B.RETURNABLE, B.ABANDONED]
    ).select_related("order", "owner"):
        yield b, b.order, b.owner, "bond", b.role, b.address, int(b.refund_csv)
    E = OnchainEscrow.Status
    hrp = service.network_hrp()
    for e in OnchainEscrow.objects.filter(
        created_at__gte=since, status__in=[E.KEYS, E.FUNDING, E.ABANDONED]
    ).select_related("order"):
        from api.logics import Logics

        for role in service.ROLES:
            if not (e.funding_signatures or {}).get(f"{role}_funding_pubkey"):
                continue
            order = e.order
            user = order.maker if (role == "seller") == Logics.is_seller(order, order.maker) else order.taker
            if user is None:
                continue
            addr = script_to_address(service.funding_spk(e, role), hrp)
            yield e, order, user, "funding", role, addr, 0


def check_all(session=None, now=None):
    """One pass. Returns how many new wrong-chain payments were reported."""
    if not enabled():
        return 0
    s = session or _session()
    n = 0
    seen_addr = {}
    for obj, order, user, what, role, address, csv in list(watched(now)):
        try:
            found = seen_addr[address] if address in seen_addr else utxos(address, s)
            seen_addr[address] = found
        except Exception as e:  # the explorer down: next pass
            logger.warning(f"spamchain watch: {address}: {e}")
            continue
        known = {(i["txid"], int(i["vout"])) for i in (obj.wrong_chain or []) if i.get("address") == address}
        new = [u for u in found if (u[0], u[1]) not in known]
        if not new:
            continue
        items = list(obj.wrong_chain or [])
        for txid, vout, sats, height in new:
            items.append({"address": address, "role": role, "what": what, "txid": txid, "vout": vout,
                          "sats": sats, "height": height, "at": int(time.time())})
        obj.wrong_chain = items
        obj.save(update_fields=["wrong_chain"])
        sats = sum(u[2] for u in new)
        report(order, user, what, role, address, sats, new, csv)
        n += len(new)
    return n


def report(order, user, what, role, address, sats, coins, csv=0):
    from api.tasks import send_notification

    where = "bond" if what == "bond" else f"{role} escrow funding"
    order.log(
        f"**Wrong chain**: {sats} Sats sent to the {where} address {address} on the SHA-256 chain (Spamcoin), "
        f"not on BTC (BLAKE2b): {', '.join(f'{c[0]}:{c[1]}' for c in coins)}. The coordinator cannot use it; "
        "the robot can take it back with the app's rescue tool (Spamchain)",
        level="WARN",
    )
    try:
        send_notification.delay(order_id=order.id, message=f"wrong_chain:{user.id}:{what}:{sats}:{csv}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"wrong chain notice not queued: {e}")
    try:
        from django.apps import apps

        if apps.is_installed("roblosats_admin"):
            from roblosats_admin import alerts

            alerts.queue("onchain", order.id, {"kind": "wrong_chain", "role": role, "sats": int(sats),
                                               "what": what, "txid": coins[0][0] if coins else ""})
    except Exception:  # noqa: BLE001
        pass
