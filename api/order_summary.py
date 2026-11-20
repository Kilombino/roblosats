"""Final summary of a finished order and a robot's past orders (Roblosats rl31-beta14).

One block for every way an order ends, Lightning, onchain or hybrid, seen from one trader:
what it sent and received, its bond (returned, lost and why, still to recover), the
coordinator fee (and the dev share), mining/routing/swap fees, rewards credited for the
order and the transaction ids / payment hashes. The client shows it at the end of an order
and in "My past orders" (upstream RoboSats keeps no history for a robot).

Only the database is read (no node, no explorer), so listing past orders is cheap and works
whatever state the node is in.
"""

import re

from django.db import connection
from django.db.models import Q

from api.models import LNPayment, OnchainBond, OnchainEscrow, OnchainPayment, Order

FINISHED = (
    Order.Status.UCA,
    Order.Status.EXP,
    Order.Status.CCA,
    Order.Status.PAY,
    Order.Status.SUC,
    Order.Status.FAI,
    Order.Status.MLD,
    Order.Status.TLD,
)
HISTORY_LIMIT = 100
_TAKING = re.compile(r"Taking 'Order (\d+)'")
_REFERENCE = re.compile(r"Payment reference: ([0-9a-fA-F-]{36})")


def _ln_bond_state(bond):
    if bond is None:
        return "none"
    S = LNPayment.Status
    if bond.status == S.SETLED:
        return "lost"
    if bond.status in (S.RETNED, S.CANCEL):
        return "returned"
    if bond.status == S.LOCKED:
        return "locked"
    return "not_locked"


def _onchain_bond(bond):
    """(state, sats, returned_sats, mining_sats, txs) of an onchain bond."""
    from api.escrow.taproot import Transaction

    S = OnchainBond.Status
    sats = bond.value or 0
    txs = []
    if bond.txid:
        from api.escrow.bonds import coins_of

        for txid in dict.fromkeys(c[0] for c in coins_of(bond)):  # a topped-up bond: each payment
            txs.append({"label": "bond", "txid": txid})
    if bond.status == S.PENALIZED:
        if bond.spend_txid:
            txs.append({"label": "bond_penalty", "txid": bond.spend_txid})
        return "lost", sats, 0, 0, txs
    if bond.status == S.RETURNED:
        mining = 0
        returned = sats
        if bond.return_tx:
            try:
                out = sum(o.value for o in Transaction.parse(bytes.fromhex(bond.return_tx)).outputs)
                spent = sum(int(c[2]) for c in bond.return_coins) if bond.return_coins else sats
                mining = max(0, spent - out)
                returned = out
            except Exception:
                pass
        if bond.spend_txid:
            txs.append({"label": "bond_return", "txid": bond.spend_txid})
        return "returned", sats, returned, mining, txs
    if bond.status == S.RETURNABLE:
        return "recoverable", sats, 0, 0, txs
    if bond.status == S.IN_DEPOSIT:
        return "in_escrow", sats, 0, 0, txs
    if bond.status in (S.LOCKED, S.SIGN):
        return "locked", sats, 0, 0, txs
    return "not_locked", 0, 0, 0, txs


def _rewards(user, order):
    """Rewards credited to this robot for this order (reward ledger)."""
    from api.logics import LEDGER_TABLE, ensure_ledger

    try:
        ensure_ledger()
        with connection.cursor() as c:
            c.execute(
                f"SELECT COALESCE(SUM(amount), 0) FROM {LEDGER_TABLE} "
                "WHERE robot_id = %s AND order_id = %s AND amount > 0",
                [user.robot.pk, order.id],
            )
            return int(c.fetchone()[0])
    except Exception:
        return 0


def _is_buyer(order, role):
    maker_buys = order.type == Order.Types.BUY
    return maker_buys if role == "maker" else not maker_buys


def _outcome(order, role, former_taker):
    if former_taker:
        return "taker_left"
    st = order.status
    S = Order.Status
    buyer = _is_buyer(order, role)
    if st == S.SUC:
        return "success"
    if st == S.PAY:
        return "sending" if buyer else "success"
    if st == S.FAI:
        return "routing_failed" if buyer else "success"
    if st == S.UCA:
        return "cancelled"
    if st == S.CCA:
        return "collab_cancelled"
    if st == S.EXP:
        return "expired"
    if st in (S.MLD, S.TLD):
        maker_lost = st == S.MLD
        return "dispute_lost" if maker_lost == (role == "maker") else "dispute_won"
    return "active"


def _lost_reason(order, role, outcome):
    if outcome == "dispute_lost":
        return "dispute_lost"
    if outcome == "taker_left":
        return "taker_left"
    if outcome == "cancelled":
        return "cancelled_after_taken"
    if outcome == "expired":
        # Only the trader at fault loses its bond (the order's expiry reason is not always
        # precise for onchain orders): the seller did not lock the escrow, the buyer did
        # not give its invoice / payout address and keys.
        return "invoice_not_submitted" if _is_buyer(order, role) else "escrow_not_locked"
    return "penalized"


def _base(order, user, role, former_taker):
    outcome = _outcome(order, role, former_taker)
    finished = order.contract_finalization_time or (
        order.expires_at if order.status in (Order.Status.EXP, Order.Status.UCA) else None
    )
    from api.escrow import flow

    hybrid = bool(order.taker_lightning) or flow.has_proxy(order)
    return {
        "order_id": order.id,
        "status": order.status,
        "outcome": outcome,
        "escrow_type": "hybrid" if hybrid else ("onchain" if flow.is_onchain(order) else "lightning"),
        "role": role,
        "is_buyer": _is_buyer(order, role),
        "currency": order.currency.currency if order.currency else None,
        "amount": str(order.amount) if order.amount is not None else None,
        "payment_method": order.payment_method,
        "created_at": order.created_at,
        "finished_at": finished,
        "contract": not former_taker
        and (order.status not in (Order.Status.UCA, Order.Status.EXP) or order.taker_id is not None),
        "trade_sats": order.last_satoshis or 0,
        "sent_sats": 0,
        "received_sats": 0,
        "returned_sats": 0,
        "bond_kind": "lightning",
        "bond_sats": 0,
        "bond_state": "none",
        "bond_returned_sats": 0,
        "bond_returned_to": "",
        "bond_lost_sats": 0,
        "bond_lost_reason": "",
        "bond_extra_sats": 0,
        "bond_extra_returned_sats": 0,
        "funding_leftover_returned_sats": 0,
        "escrow_extra_sats": 0,
        "escrow_extra_returned_sats": 0,
        "fee_from_bond": False,
        "fee_at_release": False,
        "coordinator_fee_sats": 0,
        "fee_refunded_sats": 0,
        "dev_fee_sats": 0,
        "mining_fee_sats": 0,
        "routing_fee_sats": 0,
        "swap_fee_sats": 0,
        "rewards_sats": _rewards(user, order),
        "txs": [],
        "hashes": [],
    }


def _apply_bond_leftovers(s, bonds):
    """Sats at a bond address that were not the bond: still there (bond_extra_sats, the owner
    gets them back from the order page, or alone after the bond's timelock) and sent back."""
    from api.escrow import bonds as bond_service

    for b in bonds:
        for item in b.leftover_txids or []:
            s["bond_extra_returned_sats"] = s.get("bond_extra_returned_sats", 0) + int(item.get("sats", 0))
            spent = sum(int(c[2]) for c in item.get("coins", []))
            s["mining_fee_sats"] += max(0, spent - int(item.get("sats", 0)))
            s["txs"].append({"label": "bond_leftover_return", "txid": item["txid"]})
        try:
            if b.status == OnchainBond.Status.RETURNABLE:
                mine = {(c[0], c[1]) for c in bond_service.coins_of(b)} | bond_service._in_flight(b)
                extra = sum(c[2] for c in bond_service.confirmed_coins(b.address) if (c[0], c[1]) not in mine)
            elif b.status == OnchainBond.Status.ABANDONED:
                extra = sum(c[2] for c in bond_service.confirmed_coins(b.address))
            else:
                extra = sum(c[2] for c in bond_service.leftovers(b))
        except Exception:
            extra = 0
        if extra:
            s["bond_extra_sats"] = s.get("bond_extra_sats", 0) + extra


def _apply_bond(s, order, user, role, ln_bond):
    """Fills the bond fields from this trader's bond(s): onchain bonds of the order (all its
    takes: a taker that left may have had one), else the Lightning hold invoice."""
    from api.escrow import flow

    if ln_bond is not None and flow.is_bond_proxy(ln_bond):
        proxied = flow.proxied_bond(ln_bond)
        ln_bond = None
        bonds = [proxied] if proxied is not None else []
    else:
        bonds = list(OnchainBond.objects.filter(order=order, owner=user).order_by("created_at"))
        if role == "taker" and order.taker_lightning and order.taker == user:
            bonds = []
    if bonds:
        s["bond_kind"] = "onchain"
        states = []
        for b in bonds:
            state, sats, back, mining, txs = _onchain_bond(b)
            if state == "not_locked":
                continue
            states.append(state)
            s["bond_sats"] += sats
            s["bond_returned_sats"] += back
            s["mining_fee_sats"] += mining
            if state == "lost":
                s["bond_lost_sats"] += sats
            s["txs"] += txs
        # rl31-beta18: coins at the bond address(es) that were not the bond
        _apply_bond_leftovers(s, bonds)
        # the most telling state: lost > recoverable > locked > in_escrow > returned
        for st in ("lost", "recoverable", "locked", "in_escrow", "returned"):
            if st in states:
                s["bond_state"] = st
                break
        else:
            s["bond_state"] = "not_locked"
        return
    s["bond_kind"] = "lightning"
    state = _ln_bond_state(ln_bond)
    s["bond_state"] = state
    if ln_bond is None or state == "not_locked":
        return
    s["bond_sats"] = int(ln_bond.num_satoshis)
    s["hashes"].append({"label": "bond_hash", "value": ln_bond.payment_hash})
    if state == "returned":
        s["bond_returned_sats"] = s["bond_sats"]
    elif state == "lost":
        if s["outcome"] == "dispute_won":
            # Lightning disputes settle every bond; the winner gets them back as rewards.
            s["bond_state"] = "rewards"
        else:
            s["bond_lost_sats"] = s["bond_sats"]


def _apply_lightning_trade(s, order):
    """Lightning escrow: what the seller sent and the buyer received, fees and hashes."""
    if not s["contract"] or s["outcome"] in ("taker_left",):
        return
    S = LNPayment.Status
    esc = order.trade_escrow
    if s["is_buyer"]:
        if order.status in (Order.Status.SUC, Order.Status.PAY, Order.Status.FAI):
            if order.is_swap and order.payout_tx is not None:
                tx = order.payout_tx
                s["received_sats"] = int(tx.sent_satoshis or 0)
                if order.swap_surcharge_sats:
                    # rl31-beta21, hybrid B: the seller paid the swap; only what its surcharge
                    # did not cover (a higher mining fee) came out of the buyer's payout
                    from api import hybrid_swap

                    covered = hybrid_swap.covered_by_seller(order, tx)
                    s["mining_fee_sats"] += max(0, hybrid_swap.swap_cost(tx) - covered)
                    s["swap_paid_by_seller_sats"] = covered
                else:
                    s["mining_fee_sats"] += int(tx.mining_fee_sats or 0)
                    s["swap_fee_sats"] = max(0, int((tx.num_satoshis or 0) - (tx.mining_fee_sats or 0) - (tx.sent_satoshis or 0)))
                s["coordinator_fee_sats"] = max(0, int(order.last_satoshis - (tx.num_satoshis or 0)))
                if tx.txid:
                    # 0.9.7: with its status (in the mempool / confirmed)
                    s["txs"].append({"label": "payout", "txid": tx.txid, "status": tx_status(tx.txid) or (
                        "confirmed" if tx.status == OnchainPayment.Status.CONFI else "mempool")})
                elif tx.status == OnchainPayment.Status.QUEUE:
                    s["txs"].append({"label": "payout", "txid": "", "status": "queued"})
            elif order.payout is not None:
                p = order.payout
                budget = int(p.routing_budget_sats or 0)
                s["received_sats"] = int(p.num_satoshis) if p.status == S.SUCCED or order.status == Order.Status.SUC else 0
                s["routing_fee_sats"] = budget
                s["coordinator_fee_sats"] = max(0, int(order.last_satoshis - p.num_satoshis - budget))
                s["hashes"].append({"label": "payout_hash", "value": p.payment_hash})
                if p.preimage:
                    s["hashes"].append({"label": "payout_preimage", "value": p.preimage})
    elif esc is not None:
        if esc.status == S.SETLED and s["outcome"] in ("success", "dispute_lost"):
            s["sent_sats"] = int(esc.num_satoshis)
            surcharge = int(order.swap_surcharge_sats or 0)
            if s["outcome"] == "success":
                s["coordinator_fee_sats"] = max(0, int(esc.num_satoshis - order.last_satoshis - surcharge))
            if surcharge:
                # rl31-beta21, hybrid B: the buyer's swap this seller paid (net of what came back)
                s["swap_surcharge_sats"] = surcharge
                s["swap_surcharge_refund_sats"] = int(order.swap_surcharge_refund_sats or 0)
                s["swap_for_buyer_sats"] = max(0, surcharge - int(order.swap_surcharge_refund_sats or 0))
        elif esc.status in (S.RETNED, S.CANCEL):
            s["returned_sats"] = int(esc.num_satoshis)
        if esc.status in (S.SETLED, S.RETNED, S.CANCEL, S.LOCKED):
            s["hashes"].append({"label": "escrow_hash", "value": esc.payment_hash})


def _apply_onchain_trade(s, order, user, role_in_order):
    """Onchain escrow: deposit and release, from the plan the deposit followed."""
    from api.escrow import flow, service
    from api.escrow.taproot import Transaction

    escrow = flow.escrow_of(order)
    if escrow is None or s["outcome"] == "taker_left":
        return
    try:
        erole = service.role_of(order, user)
    except Exception:
        erole = "buyer" if s["is_buyer"] else "seller"
    plan = escrow.funding_signatures.get("plan", {})
    # rl31-beta20: sats left on the funding address and sent back (also with no deposit)
    if escrow.funding_signatures.get(f"sweep_{erole}_txid"):
        try:
            info = flow.sweep_info(escrow, erole)
        except Exception:
            info = {"escrow_sweep_txid": escrow.funding_signatures[f"sweep_{erole}_txid"]}
        s["funding_leftover_returned_sats"] = int(info.get("escrow_sweep_sats") or 0)
        if info.get("escrow_sweep_spent_sats") is not None:
            s["mining_fee_sats"] += max(0, int(info["escrow_sweep_spent_sats"]) - s["funding_leftover_returned_sats"])
        s["txs"].append({"label": "funding_leftover_return", "txid": info["escrow_sweep_txid"]})
    if escrow.funding_txid:
        s["txs"].append({"label": "deposit", "txid": escrow.funding_txid})
    if not escrow.funding_txid or "trade_sats" not in plan and not escrow.release_txid:
        return
    bonds, trade = flow._bond_values(escrow)
    # rl31-beta20: a funding excess that rode in the escrow and comes back with the bond
    extra = flow._extras(escrow)[erole]
    s["escrow_extra_sats"] = extra
    if "seller_fee" in plan:
        seller_fee, buyer_fee = plan.get("seller_fee", 0), plan.get("buyer_fee", 0)
    else:  # plans before rl31-beta9 kept no fees: what the split asked for then
        seller_fee, buyer_fee = service.fee_split(order)
    spent = plan.get("spent") or []

    def paid(role):
        """What a trader put into the deposit from its funding address (not its bond)."""
        if f"{role}_pays" in plan and not (role == "buyer" and plan.get("buyer_fee_from_bond")):
            return int(plan[f"{role}_pays"])
        return sum(int(spent[i][0]) for i in plan.get(f"{role}_inputs", []) if i < len(spent))

    at_release = erole == "buyer" and bool(plan.get("buyer_fee_at_release"))
    plan2 = flow.fee_plan(escrow) >= 2
    charged = flow.charged_fees(escrow, order)
    fee = charged[erole]
    if at_release and (fee or not plan2):
        s["fee_at_release"] = True
    s["fee_from_bond"] = erole == "buyer" and bool(plan.get("buyer_fee_from_bond")) and (fee > 0 or not plan2)
    s["coordinator_fee_sats"] = int(fee)
    if plan2:
        # fee plan 2 (rl31-beta15): the fees rode in the escrow; a cancel gave them back
        total = charged["seller"] + charged["buyer"]
        dev = flow.dev_paid(escrow)
        if dev and total and fee:
            s["dev_fee_sats"] = int(round(dev * fee / total))
        if escrow.funding_signatures.get("cancel"):
            s["fee_refunded_sats"] = flow.fees_held(escrow)[erole]
    else:
        in_deposit = seller_fee + (0 if plan.get("buyer_fee_at_release") else buyer_fee)
        if escrow.dev_fee_sats and in_deposit and not at_release:
            s["dev_fee_sats"] = int(round(escrow.dev_fee_sats * fee / in_deposit))
    if f"{erole}_mining" in plan:
        s["mining_fee_sats"] += int(plan.get(f"{erole}_mining") or 0)
    elif erole == "seller":
        s["mining_fee_sats"] += max(0, paid("seller") - trade - seller_fee)
    elif not at_release:
        s["mining_fee_sats"] += max(0, paid("buyer") - buyer_fee)
    if erole == "seller":
        s["sent_sats"] = int(paid("seller") - extra or trade + seller_fee + int(plan.get("seller_mining") or 0))
    if not escrow.release_txid:
        return
    s["txs"].append({"label": "release", "txid": escrow.release_txid, "status": tx_status(escrow.release_txid)})
    try:
        tx = Transaction.parse(bytes.fromhex(escrow.release_tx))
    except Exception:
        return
    my_spk = escrow.funding_signatures.get(f"{erole}_payout_spk", "")
    received = sum(o.value for o in tx.outputs if o.script_pubkey.hex() == my_spk)
    if extra and received >= extra:
        received -= extra
        s["escrow_extra_returned_sats"] = extra
    release_mining = escrow.escrow_sats - sum(o.value for o in tx.outputs)
    cancel = bool(escrow.funding_signatures.get("cancel"))
    K = OnchainEscrow.ReleaseKind
    kind = escrow.release_kind
    outcome = flow.dispute_outcome(escrow)
    if outcome == "completed":  # 0.9.10: released as the cooperative release would have been
        kind = K.COOPERATIVE
    elif outcome.startswith("penalize_"):  # 0.9.10: no trade, the guilty side's bond penalized
        guilty = outcome.split("_", 1)[1]
        data = escrow.funding_signatures
        if data.get("release_fee_payer", "seller") == erole:
            s["mining_fee_sats"] += max(0, int(release_mining))
        my_bond_in = bonds.get(erole, 0)
        reward = int(data.get("dispute_reward_sats") or 0)
        if erole == guilty:
            if s["bond_kind"] == "onchain":
                s["bond_lost_sats"] += my_bond_in
                if s["bond_state"] == "in_escrow":
                    s["bond_state"] = "lost"
        else:
            if s["bond_kind"] == "onchain":
                s["bond_returned_sats"] += my_bond_in
                s["bond_returned_to"] = data.get(f"{erole}_payout_address", "")
                if s["bond_state"] == "in_escrow":
                    s["bond_state"] = "returned"
            s["rewards_sats"] += reward
        if erole == "seller":
            back = received - s.get("fee_refunded_sats", 0) - (0 if erole == guilty else my_bond_in + reward)
            s["returned_sats"] = int(max(0, back))
        return
    if kind == K.SELLER_WINS and cancel and s["outcome"] == "collab_cancelled":
        s["outcome"] = "dispute_returned"  # a dispute solved by giving everything back
    # payouts[0] paid the release's mining fee: the winner / the buyer on a normal release,
    # the seller when the escrow went back to it.
    if kind == K.COOPERATIVE:
        payer = "seller" if cancel else "buyer"
    elif kind == K.BUYER_WINS:
        payer = "buyer"
    else:
        payer = "seller"
    if erole == payer:
        s["mining_fee_sats"] += max(0, int(release_mining))
    my_bond_in = bonds.get(erole, 0)
    lost_in_release = 0
    if kind == K.BUYER_WINS and erole == "seller" or kind == K.SELLER_WINS and not cancel and erole == "buyer":
        lost_in_release = my_bond_in
    elif kind == K.RESCUE and erole == "buyer":
        lost_in_release = my_bond_in
    if erole == "buyer":
        if kind == K.BUYER_WINS:
            # net of the release's mining fee, which the winner's output paid (as in a normal release)
            s["received_sats"] = int(trade - (max(0, release_mining) if erole == payer else 0))
        elif kind == K.COOPERATIVE and not cancel:
            s["received_sats"] = int(max(0, received - my_bond_in))
        if lost_in_release == 0 and s["bond_kind"] == "onchain":
            s["bond_returned_sats"] += min(my_bond_in, received)
    else:
        if lost_in_release == 0 and s["bond_kind"] == "onchain":
            s["bond_returned_sats"] += min(my_bond_in, received)
            if received - my_bond_in > 0:
                # the seller's refunded fee (plan 2) has a line of its own
                s["returned_sats"] = int(received - my_bond_in - s.get("fee_refunded_sats", 0))
        if kind == K.COOPERATIVE and not cancel:
            s["returned_sats"] = 0
    if lost_in_release and s["bond_kind"] == "onchain":
        s["bond_lost_sats"] += lost_in_release
    elif my_bond_in and received and s["bond_kind"] == "onchain":
        # 0.9.9-1: where the bond came back with the release (the trader's payout address)
        s["bond_returned_to"] = escrow.funding_signatures.get(f"{erole}_payout_address", "")
    # Bonds that went into the deposit: settled by the release.
    if s["bond_state"] == "in_escrow" and s["bond_kind"] == "onchain":
        s["bond_state"] = "lost" if lost_in_release else "returned"
    if s["outcome"] == "dispute_won":
        # the winner's share of the loser's bond, paid in the release itself (gross: the
        # release's mining fee is already in the trade amount line and in the mining fees)
        paid_mining = max(0, release_mining) if erole == payer else 0
        s["rewards_sats"] += int(max(0, received + paid_mining - trade - my_bond_in))
        if erole == "seller":
            s["returned_sats"] = int(trade - paid_mining)



def tx_status(txid):
    """0.9.7: "confirmed" / "mempool" for an onchain transaction, from the coordinator's node
    (cached a minute), or None when the node does not answer or does not know it."""
    if not txid:
        return None
    from django.core.cache import cache

    key = f"txstatus:{txid}"
    cached = cache.get(key)
    if cached:
        return cached
    try:
        from api.escrow import service

        info = service.rpc("getrawtransaction", [txid, True])
    except Exception:
        return None
    status = "confirmed" if int(info.get("confirmations") or 0) > 0 else "mempool"
    cache.set(key, status, 60 if status == "mempool" else 3600)
    return status


def final_summary(order, user, former_taker=False):
    """The final summary of `order` for `user` (maker, taker, or a taker that left it)."""
    if order.maker == user:
        role = "maker"
    elif order.taker == user or former_taker:
        role = "taker"
    else:
        return None
    s = _base(order, user, role, former_taker)
    if role == "maker":
        ln_bond = order.maker_bond
    elif former_taker:
        ln_bond = _left_ln_bond(order, user)
    else:
        ln_bond = order.taker_bond
    _apply_bond(s, order, user, role, ln_bond)
    from api.escrow import flow

    if flow.is_onchain(order):
        _apply_onchain_trade(s, order, user, role)
    else:
        _apply_lightning_trade(s, order)
    if s["bond_lost_sats"] > 0 or s["bond_state"] == "lost":
        s["bond_lost_reason"] = _lost_reason(order, role, s["outcome"])
    # Dedupe txids (a bond txid may also be the deposit's parent; keep the first label).
    seen, txs = set(), []
    for t in s["txs"]:
        if not t["txid"]:
            if t.get("status") == "queued":  # 0.9.7: a payout the coordinator has not sent yet
                txs.append(t)
            continue
        if t["txid"] not in seen:
            seen.add(t["txid"])
            txs.append(t)
    s["txs"] = txs
    return s


def _left_ln_bond(order, user):
    for b in LNPayment.objects.filter(
        sender=user, concept=LNPayment.Concepts.TAKEBOND, status=LNPayment.Status.SETLED
    ):
        if _bond_order_id(b) == order.id:
            return b
    return None


def _bond_order_id(bond):
    m = _TAKING.search(bond.description or "")
    if m:
        return int(m.group(1))
    m = _REFERENCE.search(bond.description or "")
    if m:
        o = Order.objects.filter(reference=m.group(1)).only("id").first()
        return o.id if o else None
    return None


def past_orders(user, limit=HISTORY_LIMIT):
    """Finished orders of a robot (maker or taker) plus the orders it took and then left
    losing its bond (the order went back to the book without it), newest first."""
    orders = {
        o.id: (o, False)
        for o in Order.objects.filter(Q(maker=user) | Q(taker=user), status__in=FINISHED)
        .select_related("currency", "maker_bond", "taker_bond", "trade_escrow", "payout", "payout_tx")
        .order_by("-created_at")[:limit]
    }
    left = set()
    for b in OnchainBond.objects.filter(owner=user, role="taker").exclude(order__taker=user).values_list("order_id", flat=True):
        left.add(b)
    for b in LNPayment.objects.filter(
        sender=user,
        concept=LNPayment.Concepts.TAKEBOND,
        status=LNPayment.Status.SETLED,
        order_taken__isnull=True,
    ):
        oid = _bond_order_id(b)
        if oid:
            left.add(oid)
    for o in Order.objects.filter(id__in=left - set(orders)).exclude(taker=user).select_related("currency"):
        orders[o.id] = (o, True)
    result = []
    for o, former in orders.values():
        try:
            s = final_summary(o, user, former_taker=former)
        except Exception as e:  # one broken order must not hide the others
            s = {"order_id": o.id, "status": o.status, "outcome": "unknown", "created_at": o.created_at, "error": str(e)[:200]}
        if s is None:
            continue
        if former and s.get("bond_state") not in ("lost", "recoverable", "locked"):
            continue  # a take that cost nothing: not a past order of this robot
        result.append(s)
    result.sort(key=lambda s: s.get("created_at") or 0, reverse=True)
    return result[:limit]
