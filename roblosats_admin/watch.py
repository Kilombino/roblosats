"""Telegram alerts the coordinator gets from watching, not from a status change (0.9.7):

A) on-chain order events: bonds and deposit payments in the mempool / confirmed, the deposit
   fully signed and confirmed, releases, automatic bond returns, swap payouts and rescues;
B) the coordinator's own LND on-chain wallet: incoming / outgoing transactions, channel opens
   and closes (force closes too) and notable mining fees.

`watch` runs from Celery beat every minute. Every event has a key that is stored in
`AlertMark` the first time it is seen, so nothing is told twice (also across restarts). The
first run of each source only stores what is already there and sends nothing: no backfill
flood after an update or when the alerts are switched on. Events are stored even while their
toggle is off, so switching it on later does not flood either.

Pure functions (`order_events`, `wallet_events`) turn state into [(key, kind, data)] and are
what the tests exercise; the node and LND are only asked in `_order_state` / `lnd_snapshot`.
"""

import logging

from decouple import config
from django.db import IntegrityError
from django.utils import timezone

logger = logging.getLogger("roblosats_admin")

ONCHAIN, WALLET = "onchain", "wallet"
# Orders finished this long ago are no longer watched (their confirmations come in hours).
RECENT_DAYS = 3
# LND transactions older than this many blocks below the tip are not asked for.
WALLET_WINDOW = 2016


def notable_fee():
    """An outgoing transaction paying at least this mining fee (sats) is flagged."""
    return config("ALERT_NOTABLE_FEE_SATS", cast=int, default=2000)


# ---------------------------------------------------------------- dedupe


def started(source):
    from roblosats_admin.models import AlertMark

    return AlertMark.objects.filter(key=f"started:{source}").exists()


def fresh(keys):
    """Store `keys` and return the ones that were not stored before (in their order)."""
    from roblosats_admin.models import AlertMark

    keys = list(dict.fromkeys(keys))
    if not keys:
        return []
    known = set(AlertMark.objects.filter(key__in=keys).values_list("key", flat=True))
    new = []
    for key in keys:
        if key in known:
            continue
        try:
            AlertMark.objects.create(key=key)
        except IntegrityError:  # another worker stored it first: it tells, not us
            continue
        new.append(key)
    return new


def mark_started(source):
    from roblosats_admin.models import AlertMark

    AlertMark.objects.get_or_create(key=f"started:{source}")


def dispatch(source, events, send):
    """events: [(key, kind, data)]. On the first run of `source` only store them."""
    first = not started(source)
    new = set(fresh([k for k, _, _ in events]))
    if first:
        mark_started(source)
        return 0
    n = 0
    for key, kind, data in events:
        if key in new:
            send(kind, data)
            n += 1
    return n


# ---------------------------------------------------------------- A) on-chain orders


def _tx_status(txid):
    from api.order_summary import tx_status

    return tx_status(txid) if txid else None


def _auto_txid(bond):
    from roblosats_admin.onchain import _auto_return_txid

    return _auto_return_txid(bond)


def _order_state(order):
    """What `order_events` needs, asked to the database, the node and the mempool."""
    from api.escrow import flow
    from api.models import OnchainBond, OnchainEscrow

    escrow = None
    try:
        escrow = order.onchain_escrow
    except Exception:  # noqa: BLE001
        pass
    try:
        seen = flow.mempool_seen(order, escrow)
    except Exception:  # noqa: BLE001 (node down)
        seen = []
    bonds = []
    for b in order.onchain_bonds.all():
        returned = b.spend_txid if b.status == OnchainBond.Status.RETURNED else ""
        auto = bool(returned and (b.auto_return or {}).get("sigs") and returned == _auto_txid(b))
        bonds.append({
            "id": b.pk,
            "role": b.role,
            "status": int(b.status),
            "txid": b.txid or "",
            "sats": int(b.value or b.required_sats or 0),
            "auto_return_txid": returned if auto else "",
            "auto_return_status": _tx_status(returned) if auto else None,
        })
    e = None
    if escrow is not None:
        rescued = escrow.status == OnchainEscrow.Status.RESCUED or (
            escrow.release_kind == OnchainEscrow.ReleaseKind.RESCUE and escrow.release_txid
        )
        e = {
            "id": escrow.pk,
            "status": int(escrow.status),
            "sats": int(escrow.escrow_sats or 0),
            "funding_txid": escrow.funding_txid or "",
            "confirmed_height": escrow.confirmed_height or 0,
            "release_txid": escrow.release_txid or "",
            "release_status": _tx_status(escrow.release_txid),
            "release_kind": escrow.get_release_kind_display() if escrow.release_kind is not None else "",
            "leaf": (escrow.funding_signatures or {}).get("release_path") == "seller_leaf",
            "rescued": bool(rescued),
            "signed": escrow.status in (
                OnchainEscrow.Status.MEMPOOL, OnchainEscrow.Status.LOCKED, OnchainEscrow.Status.RELEASING,
                OnchainEscrow.Status.RELEASED, OnchainEscrow.Status.REFUNDED, OnchainEscrow.Status.RESCUED,
            ),
        }
    payout = None
    tx = getattr(order, "payout_tx", None) if order.is_swap else None
    if tx is not None and tx.txid:
        payout = {"txid": tx.txid, "sats": int(tx.sent_satoshis or tx.num_satoshis or 0),
                  "status": _tx_status(tx.txid)}
    return {"order": order.pk, "seen": seen, "bonds": bonds, "escrow": e, "payout": payout}


def order_events(state):
    """[(key, kind, data)] for one order's state (see `_order_state`)."""
    o = state["order"]
    out = []

    def add(key, kind, **data):
        out.append((key, kind, {"order": o, **data}))

    for s in state.get("seen") or []:
        what = "bond" if s.get("what") == "bond" else "deposit"
        add(f"oc:{what}-mempool:{o}:{s.get('txid')}", f"{what}_mempool",
            role=s.get("role", ""), sats=s.get("sats") or 0, txid=s.get("txid", ""), fee_rate=s.get("fee_rate"))
    for b in state.get("bonds") or []:
        # FUNDING = 0: not confirmed yet; anything after it has its coins confirmed
        if b["status"] not in (0, 7) and b["txid"]:
            add(f"oc:bond-confirmed:{b['id']}", "bond_confirmed", role=b["role"], sats=b["sats"], txid=b["txid"])
        if b["auto_return_txid"]:
            add(f"oc:return-sent:{b['id']}", "return_sent", role=b["role"], sats=b["sats"], txid=b["auto_return_txid"])
            if b["auto_return_status"] == "confirmed":
                add(f"oc:return-confirmed:{b['id']}", "return_confirmed", role=b["role"], txid=b["auto_return_txid"])
    e = state.get("escrow")
    if e:
        if e["signed"] and e["funding_txid"]:
            add(f"oc:deposit-signed:{e['id']}", "deposit_signed", sats=e["sats"], txid=e["funding_txid"])
        if e["confirmed_height"]:
            add(f"oc:deposit-confirmed:{e['id']}", "deposit_confirmed", sats=e["sats"], txid=e["funding_txid"],
                height=e["confirmed_height"])
        if e["release_txid"]:
            kind = "rescue_sent" if e["rescued"] else "release_sent"
            add(f"oc:{kind}:{e['id']}", kind, txid=e["release_txid"], how=e["release_kind"], leaf=e["leaf"])
            if e["release_status"] == "confirmed":
                add(f"oc:release-confirmed:{e['id']}", "release_confirmed", txid=e["release_txid"])
        elif e["rescued"]:
            add(f"oc:rescue_sent:{e['id']}", "rescue_sent", txid="", how=e["release_kind"], leaf=False)
    p = state.get("payout")
    if p:
        add(f"oc:payout-sent:{o}:{p['txid']}", "payout_sent", sats=p["sats"], txid=p["txid"])
        if p["status"] == "confirmed":
            add(f"oc:payout-confirmed:{o}:{p['txid']}", "payout_confirmed", sats=p["sats"], txid=p["txid"])
    return out


def watched_orders(now=None):
    """On-chain orders, hybrids and swaps still moving, or created in the last days (their
    release, return or payout confirmations come after they finish)."""
    from datetime import timedelta

    from django.db.models import Exists, OuterRef, Q

    from api.models import OnchainBond, Order

    now = now or timezone.now()
    live = [1, 2, 3, 6, 7, 8, 9, 10, 11, 13, 15, 16]
    return (
        Order.objects.annotate(_bonds=Exists(OnchainBond.objects.filter(order=OuterRef("pk"))))
        .filter(Q(escrow_type=Order.EscrowTypes.ONCHAIN) | Q(_bonds=True) | Q(is_swap=True))
        .filter(Q(status__in=live) | Q(created_at__gte=now - timedelta(days=RECENT_DAYS + 7)))
        .select_related("payout_tx")
    )


# ---------------------------------------------------------------- B) the coordinator's LND wallet


def lnd_snapshot():
    """The LND wallet as plain dicts, or None when the node is not LND or does not answer."""
    if config("LNVENDOR", cast=str, default="LND") != "LND":
        return None
    try:
        from api.lightning import lightning_pb2, lightning_pb2_grpc
        from api.lightning.lnd import LNDNode

        stub = lightning_pb2_grpc.LightningStub(LNDNode.channel)
        tip = stub.GetInfo(lightning_pb2.GetInfoRequest(), timeout=20).block_height
        txs = stub.GetTransactions(
            lightning_pb2.GetTransactionsRequest(start_height=max(0, tip - WALLET_WINDOW), end_height=-1),
            timeout=30,
        ).transactions
        chans = stub.ListChannels(lightning_pb2.ListChannelsRequest(), timeout=20).channels
        pend = stub.PendingChannels(lightning_pb2.PendingChannelsRequest(), timeout=20)
        closed = stub.ClosedChannels(lightning_pb2.ClosedChannelsRequest(), timeout=20).channels
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Wallet alerts: LND did not answer ({e})")
        return None
    close_type = {0: "cooperative", 1: "local_force", 2: "remote_force", 3: "breach", 4: "funding_canceled",
                  5: "abandoned"}
    return {
        "tip": tip,
        "txs": [
            {"txid": t.tx_hash, "amount": int(t.amount), "fee": int(t.total_fees),
             "confs": int(t.num_confirmations), "label": t.label or ""}
            for t in txs
        ],
        "open": [{"point": c.channel_point, "peer": c.remote_pubkey, "capacity": int(c.capacity)} for c in chans],
        "opening": [
            {"point": p.channel.channel_point, "peer": p.channel.remote_node_pub, "capacity": int(p.channel.capacity)}
            for p in pend.pending_open_channels
        ],
        "closing": [
            {"point": p.channel.channel_point, "peer": p.channel.remote_node_pub, "capacity": int(p.channel.capacity),
             "force": True, "txid": p.closing_txid}
            for p in pend.pending_force_closing_channels
        ] + [
            {"point": p.channel.channel_point, "peer": p.channel.remote_node_pub, "capacity": int(p.channel.capacity),
             "force": False, "txid": getattr(p, "closing_txid", "")}
            for p in pend.waiting_close_channels
        ],
        "closed": [
            {"point": c.channel_point, "peer": c.remote_pubkey, "capacity": int(c.capacity),
             "type": close_type.get(int(c.close_type), str(c.close_type)), "settled": int(c.settled_balance),
             "txid": c.closing_tx_hash}
            for c in closed
        ],
    }


def wallet_events(snap, fee_floor=None):
    """[(key, kind, data)] from an LND snapshot. A transaction first seen confirmed is told once,
    as confirmed; one seen in the mempool is told then and again when it confirms."""
    fee_floor = notable_fee() if fee_floor is None else fee_floor
    out = []
    for t in snap.get("txs") or []:
        data = dict(t)
        direction = "in" if t["amount"] > 0 else "out"
        if t["confs"] > 0:
            # the "seen" key too, so a later restart does not tell it as new in the mempool
            out.append((f"w:tx-seen:{t['txid']}", None, None))
            out.append((f"w:tx-confirmed:{t['txid']}", f"tx_{direction}_confirmed", data))
        else:
            out.append((f"w:tx-seen:{t['txid']}", f"tx_{direction}_mempool", data))
        if direction == "out" and t["fee"] >= fee_floor > 0:
            out.append((f"w:fee:{t['txid']}", "fee_notable", data))
    for c in snap.get("opening") or []:
        out.append((f"w:chan-opening:{c['point']}", "chan_opening", c))
    for c in snap.get("open") or []:
        out.append((f"w:chan-open:{c['point']}", "chan_open", c))
    for c in snap.get("closing") or []:
        out.append((f"w:chan-closing:{c['point']}", "chan_closing", c))
    for c in snap.get("closed") or []:
        out.append((f"w:chan-closed:{c['point']}", "chan_closed", c))
    return out


# ---------------------------------------------------------------- run


def _sender(event):
    from roblosats_admin import alerts

    def send(kind, data):
        if kind is None:
            return
        alerts.queue(event, data.get("order"), {"kind": kind, **{k: v for k, v in data.items() if k != "order"}})

    return send


def watch():
    """One pass over both sources. Returns {"onchain": n, "wallet": n} alerts queued."""
    out = {ONCHAIN: 0, WALLET: 0}
    events = []
    for order in watched_orders():
        try:
            events += order_events(_order_state(order))
        except Exception as e:  # noqa: BLE001 (one broken order never stops the others)
            logger.warning(f"On-chain alerts: order {order.pk} skipped ({e})")
    out[ONCHAIN] = dispatch(ONCHAIN, events, _sender(ONCHAIN))
    snap = lnd_snapshot()
    if snap is not None:
        out[WALLET] = dispatch(WALLET, wallet_events(snap), _sender(WALLET))
    return out
