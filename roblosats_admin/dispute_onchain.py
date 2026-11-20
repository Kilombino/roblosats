"""Dispute page for on-chain escrows (Roblosats 0.9.9-1).

Until 0.9.9 the dispute page checked every order as a Lightning one (escrow settled in LND,
Lightning bonds, a payout invoice) and refused every on-chain dispute. Here the page checks
what the on-chain actions of api/admin.py need and says, with the same functions those actions
use (api.escrow.flow.dispute_split / return_split, _bond_values, _extras, fees_held), who gets
how many sats at which address, who still has to sign and what the coordinator keeps.

Kinds (roblosats_admin.onchain.kind):
- ON-CHAIN: escrow and both bonds inside the escrow output.
- HYBRID A: escrow on chain, the taker's bond is a Lightning hold invoice (settled when the
  dispute opened).
- HYBRID B: the order became a Lightning order; only the maker's bond is on chain, behind a
  proxy LNPayment. The Lightning plan applies, plus a note (hybrid_b_note).

Nothing here moves money: the buttons still call the actions of api/admin.py.
"""

from roblosats_admin.disputes import fmt_sats, is_open_dispute, role_name
from roblosats_admin.i18n import _

ONCHAIN_DECISIONS = ("buyer", "seller", "return")


def applies(order):
    """True when the dispute is resolved on chain (on-chain escrow or hybrid A)."""
    from api.escrow import flow

    return flow.is_onchain(order)


def _nick(user):
    return user.username if user else "?"


def _users(order):
    from roblosats_admin.disputes import roles

    r = roles(order)
    return {"buyer": r["buyer"], "seller": r["seller"]}


def _address(escrow, role):
    return (escrow.funding_signatures or {}).get(f"{role}_payout_address") or ""


def _ln_bond(order, role):
    """The Lightning bond of 'buyer'/'seller' in a hybrid A order, or None."""
    from api.escrow import flow

    if flow.role_is_ln(order, role):
        return order.taker_bond
    return None


def _missing_common(order, escrow):
    from api.escrow import flow
    from api.models import OnchainEscrow

    missing = []
    if not is_open_dispute(order):
        missing.append(_("the order is no longer in dispute"))
    if escrow is None:
        missing.append(_("the order has no on-chain escrow"))
        return missing
    if escrow.status not in (OnchainEscrow.Status.LOCKED, OnchainEscrow.Status.RELEASING):
        missing.append(
            _("the on-chain escrow is not confirmed and unspent (status: %(status)s)")
            % {"status": _(escrow.get_status_display())}
        )
    plan = (escrow.funding_signatures or {}).get("plan") or {}
    bond_ids = plan.get("bond_ids") or {}
    for role in ("seller", "buyer"):
        if role in bond_ids:
            continue
        if _ln_bond(order, role) is not None:
            continue
        missing.append(_("the %(role)s's bond is not in the escrow") % {"role": role_name(role)})
    if not missing:
        try:
            flow._bond_values(escrow)
        except Exception:  # noqa: BLE001 (a bond row is gone)
            missing.append(_("the bonds of the escrow cannot be read"))
    return missing


def _need_address(escrow, role, missing):
    if not _address(escrow, role):
        if role == "buyer":
            missing.append(_("the buyer has not given an on-chain address to receive the sats"))
        else:
            missing.append(_("the seller has not given an on-chain refund address"))


def _signer_line(order, role, what):
    nick = _nick(_users(order)[role])
    if what == "return":
        text = _(
            "It goes out on chain through the seller's leaf with the coordinator: the seller's "
            "app (%(nick)s) must sign the return, then the coordinator adds its signature and "
            "broadcasts it. The buyer does not sign anything."
        )
    elif role == "buyer":
        text = _(
            "It goes out on chain through the buyer's leaf with the coordinator: the buyer's "
            "app (%(nick)s) must sign the release, then the coordinator adds its signature and "
            "broadcasts it. The seller does not sign anything."
        )
    else:
        text = _(
            "It goes out on chain through the seller's leaf with the coordinator: the seller's "
            "app (%(nick)s) must sign the release, then the coordinator adds its signature and "
            "broadcasts it. The buyer does not sign anything."
        )
    return text % {"nick": nick}


def _parts(items):
    return " + ".join(f"{label} {fmt_sats(sats)}" for label, sats in items if sats)


def _winner_plan(order, escrow, decision, lines, missing):
    from api.escrow import flow

    buyer_wins = decision == "buyer"
    winner = "buyer" if buyer_wins else "seller"
    loser = "seller" if buyer_wins else "buyer"
    _need_address(escrow, winner, missing)
    if missing:
        return
    outs, info = flow.dispute_split(order, escrow, buyer_wins)
    users = _users(order)
    lines.append(_signer_line(order, winner, "win"))
    for who, sats in outs:
        if who == winner:
            parts = _parts(
                [
                    (_("trade amount") if winner == "buyer" else _("their escrow back"), info["trade"]),
                    (_("their bond"), info["bonds"][winner]),
                    (_("reward from the other bond"), info["reward"]),
                    (_("leftover of their deposit"), info["extras"][winner]),
                ]
            )
            lines.append(
                _(
                    "The %(role)s %(nick)s receives %(sats)s sats at %(address)s (%(parts)s), "
                    "minus the release's mining fee, which comes out of this output."
                )
                % {
                    "role": role_name(winner),
                    "nick": _nick(users[winner]),
                    "sats": fmt_sats(sats),
                    "address": _address(escrow, winner),
                    "parts": parts,
                }
            )
        elif who == loser:
            lines.append(
                _(
                    "The %(role)s %(nick)s only gets back the leftover of their deposit, "
                    "%(sats)s sats at %(address)s."
                )
                % {
                    "role": role_name(loser),
                    "nick": _nick(users[loser]),
                    "sats": fmt_sats(sats),
                    "address": _address(escrow, loser),
                }
            )
        elif who == "coordinator":
            lines.append(
                _(
                    "The coordinator receives %(sats)s sats at its fee address: %(penalty)s "
                    "kept from the loser's bond + %(fees)s of trade fees."
                )
                % {
                    "sats": fmt_sats(sats),
                    "penalty": fmt_sats(info["penalty"]),
                    "fees": fmt_sats(info["fees"] - info["dev"]),
                }
            )
        elif who == "dev":
            lines.append(_("The development fund receives %(sats)s sats.") % {"sats": fmt_sats(sats)})
    if info["bonds"][loser]:
        lines.append(
            _("The %(role)s %(nick)s loses their on-chain bond (%(bond)s sats).")
            % {"role": role_name(loser), "nick": _nick(users[loser]), "bond": fmt_sats(info["bonds"][loser])}
        )
    if not any(who == "coordinator" for who, _s in outs) and not info["penalty"]:
        lines.append(_("The coordinator receives nothing on chain."))
    ln = _ln_bond(order, loser)
    if ln is not None:
        import decouple

        split = decouple.config("SLASHED_BOND_REWARD_SPLIT", cast=float, default=0.5)
        lines.append(
            _(
                "The %(role)s's bond is a Lightning bond (%(sats)s sats), settled when the "
                "dispute opened: the coordinator keeps it and credits %(reward)s sats of it to "
                "the %(winner)s as a reward, withdrawn from the app."
            )
            % {
                "role": role_name(loser),
                "sats": fmt_sats(ln.num_satoshis),
                "reward": fmt_sats(int(ln.num_satoshis * split)),
                "winner": role_name(winner),
            }
        )
    ln = _ln_bond(order, winner)
    if ln is not None:
        lines.append(
            _(
                "The %(role)s's Lightning bond (%(sats)s sats), settled when the dispute opened, "
                "is credited back to them as a reward, withdrawn from the app."
            )
            % {"role": role_name(winner), "sats": fmt_sats(ln.num_satoshis)}
        )


def _return_plan(order, escrow, lines, missing):
    from api.escrow import flow
    from api.escrow.service import EscrowError

    _need_address(escrow, "seller", missing)
    if missing:
        return
    try:
        outs = flow.return_split(escrow)
    except EscrowError:
        _need_address(escrow, "buyer", missing)
        return
    bonds, trade = flow._bond_values(escrow)
    held = flow.fees_held(escrow)
    ex = flow._extras(escrow)
    users = _users(order)
    lines.append(_signer_line(order, "seller", "return"))
    paid = dict(outs)
    for who, sats in outs:
        if who == "seller":
            parts = _parts(
                [
                    (_("trade amount"), trade),
                    (_("their bond"), bonds["seller"]),
                    (_("their fee"), held["seller"]),
                    (_("leftover of their deposit"), ex["seller"]),
                ]
            )
            lines.append(
                _(
                    "The seller %(nick)s gets back %(sats)s sats at %(address)s (%(parts)s), "
                    "minus the mining fee of the return, which comes out of this output."
                )
                % {
                    "nick": _nick(users["seller"]),
                    "sats": fmt_sats(sats),
                    "address": _address(escrow, "seller"),
                    "parts": parts,
                }
            )
        else:
            parts = _parts(
                [
                    (_("their bond"), bonds["buyer"]),
                    (_("their fee"), held["buyer"]),
                    (_("leftover of their deposit"), ex["buyer"]),
                ]
            )
            lines.append(
                _("The buyer %(nick)s gets back %(sats)s sats at %(address)s (%(parts)s).")
                % {
                    "nick": _nick(users["buyer"]),
                    "sats": fmt_sats(sats),
                    "address": _address(escrow, "buyer"),
                    "parts": parts,
                }
            )
    to_buyer = bonds["buyer"] + held["buyer"] + ex["buyer"]
    if to_buyer and "buyer" not in paid:
        lines.append(
            _(
                "The buyer's part (%(sats)s sats) is below the dust limit of their address: it "
                "cannot be an output of its own and goes with the seller's."
            )
            % {"sats": fmt_sats(to_buyer)}
        )
    if flow.fee_plan(escrow) >= 2:
        lines.append(_("The coordinator charges nothing: the trade fees are given back."))
    else:
        lines.append(
            _("The coordinator keeps only the fee already paid in the deposit (%(sats)s sats).")
            % {"sats": fmt_sats(escrow.coordinator_fee_sats)}
        )
    ln = _ln_bond(order, "buyer") or _ln_bond(order, "seller")
    if ln is not None:
        lines.append(
            _(
                "The taker's Lightning bond (%(sats)s sats) goes back to them: credited as a "
                "reward if it was already settled when the dispute opened, unlocked otherwise."
            )
            % {"sats": fmt_sats(ln.num_satoshis)}
        )
    lines.append(_("The order ends as collaboratively cancelled."))


def plan(order, decision):
    """Same shape as disputes.plan: {lines, missing, warning, possible}."""
    from api.escrow import flow
    from api.models import OnchainEscrow

    escrow = flow.escrow_of(order)
    missing = _missing_common(order, escrow)
    lines, warning = [], ""
    if decision not in ONCHAIN_DECISIONS:
        if decision == "completed":
            missing.append(
                _("not possible with an on-chain escrow: to pay the buyer use “Buyer wins”")
            )
        else:
            missing.append(
                _(
                    "not possible with an on-chain escrow: it can only go to the buyer, to the "
                    "seller, or back to each party"
                )
            )
    elif not missing:
        try:
            if decision == "return":
                _return_plan(order, escrow, lines, missing)
            else:
                _winner_plan(order, escrow, decision, lines, missing)
        except Exception as e:  # noqa: BLE001 (a broken escrow record must not break the page)
            missing.append(_("the release cannot be planned: %(error)s") % {"error": str(e)})
            lines = []
        if escrow is not None and escrow.status == OnchainEscrow.Status.RELEASING and not missing:
            warning = _(
                "A cooperative release had already been prepared (never broadcast): this "
                "decision replaces it."
            )
    return {"lines": lines, "missing": missing, "warning": warning, "possible": not missing}


def hybrid_b_note(order):
    """Hybrid B (the order went on over Lightning): what became of the maker's on-chain bond."""
    from api.escrow import bonds as bond_service
    from api.escrow import flow

    proxy = order.maker_bond
    if not flow.is_bond_proxy(proxy):
        return ""
    bond = flow.proxied_bond(proxy)
    if bond is None:
        return ""
    if flow.proxy_is_penalized(proxy):
        return _(
            "The maker's bond is on chain (%(bond)s sats, behind a Lightning stand-in). It was "
            "penalized on chain when the dispute opened: the coordinator got %(got)s sats from "
            "it and the rest went to the miners. What the maker gets back is credited as a "
            "Lightning reward."
        ) % {"bond": fmt_sats(bond.value), "got": fmt_sats(bond_service.penalty_sats(bond))}
    return _(
        "The maker's bond is on chain (%(bond)s sats, behind a Lightning stand-in). What the "
        "maker gets back is credited as a Lightning reward."
    ) % {"bond": fmt_sats(bond.value)}


def next_step(order):
    """After an on-chain decision: who has to sign, or where the release is. None otherwise."""
    from api.escrow import flow
    from api.models import OnchainEscrow
    from roblosats_admin import onchain

    if not applies(order):
        return None
    escrow = flow.escrow_of(order)
    if escrow is None or escrow.release_kind not in (
        OnchainEscrow.ReleaseKind.BUYER_WINS,
        OnchainEscrow.ReleaseKind.SELLER_WINS,
    ):
        return None
    role = "buyer" if escrow.release_kind == OnchainEscrow.ReleaseKind.BUYER_WINS else "seller"
    nick = _nick(_users(order)[role])
    if escrow.release_txid:
        status = onchain.status_text(escrow.release_txid)
        return {
            "done": True,
            "text": _("The %(role)s signed and the coordinator broadcast the release.")
            % {"role": role_name(role)},
            "link": onchain.tx_link(escrow.release_txid),
            "status": status,
        }
    if escrow.status != OnchainEscrow.Status.RELEASING:
        return None
    return {
        "done": False,
        "text": _(
            "Waiting for the %(role)s's signature (%(nick)s): their app signs the release when "
            "they open the order. The coordinator then adds its signature and broadcasts it; "
            "nothing else is needed from you."
        )
        % {"role": role_name(role), "nick": nick},
        "link": "",
        "status": "",
    }
