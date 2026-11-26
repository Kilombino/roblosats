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

0.9.10: "Penalize the seller/buyer (no trade)" (flow.penalize_split: the seller's leaf, the
guilty bond less the reward share to the coordinator) and "Trade completed" (flow.completed_split:
the buyer's leaf, the cooperative release) exist on chain too.

Nothing here moves money: the buttons still call the actions of api/admin.py.
"""

from roblosats_admin.disputes import fmt_sats, is_open_dispute, role_name, side_name
from roblosats_admin.i18n import _

ONCHAIN_DECISIONS = ("buyer", "seller", "return", "penalize_seller", "penalize_buyer", "completed")


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
    """The Lightning bond of 'buyer'/'seller' in a hybrid A/D order, or None."""
    from api.escrow import flow

    if flow.role_is_ln(order, role):
        return flow.ln_bond(order)
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
    if what == "penalize":
        text = _(
            "It goes out on chain through the seller's leaf with the coordinator: the seller's "
            "app (%(nick)s) must sign the release, then the coordinator adds its signature and "
            "broadcasts it. The buyer does not sign anything."
        )
        return text % {"nick": nick}
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
    out = ""
    for label, sats in items:
        if not sats:
            continue
        if sats < 0:
            out += f" − {label} {fmt_sats(-sats)}"
        else:
            out += (" + " if out else "") + f"{label} {fmt_sats(sats)}"
    return out.strip()


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
                    (_("trade amount (on-chain escrow)") if winner == "buyer" else _("their on-chain escrow back"), info["trade"]),
                    (_("their on-chain bond"), info["bonds"][winner]),
                    (_("reward from the other on-chain bond"), info["reward"]),
                    (_("leftover of their deposit"), info["extras"][winner]),
                ]
            )
            lines.append(
                _(
                    "The %(role)s %(nick)s receives %(sats)s sats on chain at %(address)s (%(parts)s), "
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
                    "%(sats)s sats on chain at %(address)s."
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
                    "The coordinator receives %(sats)s sats on chain at its fee address: %(penalty)s "
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
                "the %(winner)s as a Lightning reward, withdrawn from the app."
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
                "is credited back to them as a Lightning reward, withdrawn from the app."
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
                    (_("trade amount (on-chain escrow)"), trade),
                    (_("their on-chain bond"), bonds["seller"]),
                    (_("their fee"), held["seller"]),
                    (_("leftover of their deposit"), ex["seller"]),
                ]
            )
            lines.append(
                _(
                    "The seller %(nick)s gets back %(sats)s sats on chain at %(address)s (%(parts)s), "
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
                    (_("their on-chain bond"), bonds["buyer"]),
                    (_("their fee"), held["buyer"]),
                    (_("leftover of their deposit"), ex["buyer"]),
                ]
            )
            lines.append(
                _("The buyer %(nick)s gets back %(sats)s sats on chain at %(address)s (%(parts)s).")
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
                "The %(side)s's Lightning bond (%(sats)s sats) goes back to them: credited as a "
                "Lightning reward if it was already settled when the dispute opened, unlocked otherwise."
            )
            % {"sats": fmt_sats(ln.num_satoshis), "side": side_name(flow.ln_side(order) or "taker")}
        )
    lines.append(_("The order ends as collaboratively cancelled."))


def _penalize_plan(order, escrow, guilty, lines, missing):
    """0.9.10: penalize with no trade (flow.penalize_split), through the seller's leaf."""
    from api.escrow import flow
    from api.escrow.service import EscrowError

    _need_address(escrow, "seller", missing)
    if missing:
        return
    try:
        outs, info = flow.penalize_split(order, escrow, guilty)
    except EscrowError:
        _need_address(escrow, "buyer", missing)
        return
    innocent = info["innocent"]
    users = _users(order)
    lines.append(_signer_line(order, "seller", "penalize"))
    for who, sats in outs:
        if who == "seller":
            items = [(_("their on-chain escrow back"), info["trade"]), (_("their fee"), info["held"]["seller"]),
                     (_("leftover of their deposit"), info["extras"]["seller"])]
            if innocent == "seller":
                items[1:1] = [(_("their on-chain bond"), info["bonds"]["seller"]), (_("reward from the other on-chain bond"), info["reward"])]
            text = _("The seller %(nick)s gets back %(sats)s sats on chain at %(address)s (%(parts)s).")
        elif who == "buyer":
            items = [(_("their fee"), info["held"]["buyer"]), (_("leftover of their deposit"), info["extras"]["buyer"])]
            if innocent == "buyer":
                items[0:0] = [(_("their on-chain bond"), info["bonds"]["buyer"]), (_("reward from the other on-chain bond"), info["reward"])]
            text = _("The buyer %(nick)s gets back %(sats)s sats on chain at %(address)s (%(parts)s).")
        else:
            lines.append(
                _("The coordinator receives %(sats)s sats on chain at its fee address: the penalized bond less the reward share.")
                % {"sats": fmt_sats(sats)}
            )
            continue
        lines.append(text % {"nick": _nick(users[who]), "sats": fmt_sats(sats), "address": _address(escrow, who),
                             "parts": _parts(items)})
    payer = outs[info["fee_payer"]][0]
    lines.append(
        _("The release's mining fee comes out of the coordinator's output.")
        if payer == "coordinator"
        else _("The release's mining fee comes out of the seller's output.")
    )
    if info["bonds"][guilty]:
        lines.append(
            _("The %(role)s %(nick)s loses their on-chain bond (%(bond)s sats).")
            % {"role": role_name(guilty), "nick": _nick(users[guilty]), "bond": fmt_sats(info["bonds"][guilty])}
        )
    ln = _ln_bond(order, guilty)
    if ln is not None:
        import decouple

        split = decouple.config("SLASHED_BOND_REWARD_SPLIT", cast=float, default=0.5)
        lines.append(
            _(
                "The %(role)s's bond is a Lightning bond (%(sats)s sats), settled when the "
                "dispute opened: the coordinator keeps it and credits %(reward)s sats of it to "
                "the %(winner)s as a Lightning reward, withdrawn from the app."
            )
            % {"role": role_name(guilty), "sats": fmt_sats(ln.num_satoshis),
               "reward": fmt_sats(int(ln.num_satoshis * split)), "winner": role_name(innocent)}
        )
    ln = _ln_bond(order, innocent)
    if ln is not None:
        lines.append(
            _(
                "The %(role)s's Lightning bond (%(sats)s sats), settled when the dispute opened, "
                "is credited back to them as a Lightning reward, withdrawn from the app."
            )
            % {"role": role_name(innocent), "sats": fmt_sats(ln.num_satoshis)}
        )
    lines.append(_("There is no trade: the trade fees are given back and nobody receives sats they did not pay for."))


def _completed_plan(order, escrow, lines, missing):
    """0.9.10: a completed trade (flow.completed_split), through the buyer's leaf."""
    from api.escrow import flow

    _need_address(escrow, "buyer", missing)
    bonds, _trade = flow._bond_values(escrow)
    if bonds["seller"] and not _address(escrow, "seller"):
        _need_address(escrow, "seller", missing)
    if missing:
        return
    outs, info = flow.completed_split(order, escrow)
    users = _users(order)
    lines.append(_signer_line(order, "buyer", "completed"))
    for who, sats in outs:
        if who == "buyer":
            lines.append(
                _(
                    "The buyer %(nick)s receives %(sats)s sats on chain at %(address)s (%(parts)s), minus the "
                    "release's mining fee, which comes out of this output."
                )
                % {"nick": _nick(users["buyer"]), "sats": fmt_sats(sats), "address": _address(escrow, "buyer"),
                   "parts": _parts([(_("trade amount (on-chain escrow)"), info["trade"]), (_("their on-chain bond"), info["bonds"]["buyer"]),
                                    (_("leftover of their deposit"), info["extras"]["buyer"]),
                                    (_("their fee"), -info["fee_later"])])}
            )
        elif who == "seller":
            lines.append(
                _("The seller %(nick)s gets back %(sats)s sats on chain at %(address)s (%(parts)s).")
                % {"nick": _nick(users["seller"]), "sats": fmt_sats(sats), "address": _address(escrow, "seller"),
                   "parts": _parts([(_("their on-chain bond"), info["bonds"]["seller"]),
                                    (_("leftover of their deposit"), info["extras"]["seller"])])}
            )
        elif who == "coordinator":
            lines.append(
                _("The coordinator receives %(sats)s sats on chain at its fee address: the trade fees.")
                % {"sats": fmt_sats(sats)}
            )
        elif who == "dev":
            lines.append(_("The development fund receives %(sats)s sats.") % {"sats": fmt_sats(sats)})
    ln = _ln_bond(order, "buyer") or _ln_bond(order, "seller")
    if ln is not None:
        lines.append(
            _(
                "The %(side)s's Lightning bond (%(sats)s sats) goes back to them: credited as a "
                "Lightning reward if it was already settled when the dispute opened, unlocked otherwise."
            )
            % {"sats": fmt_sats(ln.num_satoshis), "side": side_name(flow.ln_side(order) or "taker")}
        )
    lines.append(_("The order ends as a successful trade once the release is broadcast."))


def plan(order, decision):
    """Same shape as disputes.plan: {lines, missing, warning, possible}."""
    from api.escrow import flow
    from api.models import OnchainEscrow

    escrow = flow.escrow_of(order)
    missing = _missing_common(order, escrow)
    lines, warning = [], ""
    if decision not in ONCHAIN_DECISIONS:
        missing.append(_("unknown decision"))
    elif not missing:
        try:
            if decision == "return":
                _return_plan(order, escrow, lines, missing)
            elif decision.startswith("penalize_"):
                _penalize_plan(order, escrow, decision.split("_", 1)[1], lines, missing)
            elif decision == "completed":
                _completed_plan(order, escrow, lines, missing)
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
    from api.models import OnchainBond

    proxy = order.maker_bond
    if not flow.is_bond_proxy(proxy):
        return ""
    bond = flow.proxied_bond(proxy)
    if bond is None:
        return ""
    if flow.proxy_is_penalized(proxy):
        return _(
            "The maker's bond is on chain (%(bond)s sats, behind a Lightning stand-in). It was "
            "already penalized on chain before the decision: the coordinator got %(got)s sats from "
            "it and the rest went to the miners. If the maker keeps their bond, its value is "
            "credited as a Lightning reward."
        ) % {"bond": fmt_sats(bond.value), "got": fmt_sats(bond_service.penalty_sats(bond))}
    if bond.status == OnchainBond.Status.LOCKED:
        # 0.9.12: not penalized when the dispute opened
        values = {"bond": fmt_sats(bond.value), "blocks": "?"}
        try:
            left = bond_service.blocks_to_refund(bond)
            values["blocks"] = str(max(0, left - bond_service.safety_blocks(bond))) if left is not None else "0"
        except Exception:  # noqa: BLE001
            pass
        return _(
            "The maker's bond is on chain (%(bond)s sats) and still locked: it was not penalized "
            "when the dispute opened. If the maker keeps it, it goes straight back to them on "
            "chain; if they lose it, it is penalized on chain when you decide. If no decision is "
            "made within %(blocks)s blocks, it is penalized anyway before the maker could take it "
            "back alone."
        ) % values
    return _(
        "The maker's bond is on chain (%(bond)s sats, behind a Lightning stand-in). What the "
        "maker gets back is credited as a Lightning reward."
    ) % {"bond": fmt_sats(bond.value)}


def hybrid_b_lines(order, decision, lines):
    """0.9.12: a hybrid B order whose on-chain maker (buyer) bond is still locked: the lines of
    the Lightning plan that credit that bond say instead that it goes back on chain, or that it
    is penalized now. Edits `lines` in place; nothing when the bond is not locked any more."""
    from api.escrow import flow
    from roblosats_admin.disputes import fmt_sats, payout_sats, roles

    if not flow.proxy_still_locked(order) or not lines:
        return
    r = roles(order)
    if not r["maker_buys"]:
        return  # hybrid B: the maker is always the buyer
    v = {
        "buyer": r["buyer"].username if r["buyer"] else "?",
        "seller": r["seller"].username if r["seller"] else "?",
        "buyer_bond": fmt_sats(order.maker_bond.num_satoshis),
        "seller_bond": fmt_sats(r["seller_bond"].num_satoshis if r["seller_bond"] else 0),
        "escrow": fmt_sats(order.trade_escrow.num_satoshis if order.trade_escrow else 0),
    }
    payout = payout_sats(order)
    if decision == "buyer" and payout is not None:
        v["trade"] = fmt_sats(payout)
        lines[0] = _(
            "The buyer %(buyer)s is credited %(trade)s sats as a reward (the trade amount). Their "
            "on-chain bond (%(buyer_bond)s) is not credited: it goes straight back to them on chain."
        ) % v
    elif decision == "seller" and len(lines) > 1:
        lines[1] = _("The buyer %(buyer)s gets nothing: their on-chain bond (%(buyer_bond)s) is penalized on chain now.") % v
    elif decision == "return":
        lines[0] = _(
            "Each party gets back what they put in: the buyer %(buyer)s their on-chain bond "
            "(%(buyer_bond)s), straight back on chain; the seller %(seller)s their bond "
            "(%(seller_bond)s) and their escrow (%(escrow)s), credited as a reward to withdraw "
            "from the app."
        ) % v
    elif decision == "penalize_seller" and len(lines) > 1:
        lines[1] = _(
            "The buyer %(buyer)s gets their on-chain bond (%(buyer_bond)s) straight back on chain. "
            "They do not receive the trade sats."
        ) % v
    elif decision == "penalize_buyer":
        lines[0] = _(
            "The buyer %(buyer)s LOSES their on-chain bond (%(buyer_bond)s): it is penalized on "
            "chain now and the coordinator keeps it."
        ) % v
    elif decision == "completed" and len(lines) > 1:
        lines[1] = _(
            "Both bonds go back: the buyer's on-chain bond (%(buyer_bond)s) straight back on "
            "chain, and %(seller_bond)s credited to the seller %(seller)s as a reward."
        ) % v


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
