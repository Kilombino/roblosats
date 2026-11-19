"""On-chain escrow in the coordinator panel (Roblosats 0.9.0).

Read-only views of the on-chain bonds and escrows: the escrow kind of each order
(Lightning, on-chain, hybrid A, hybrid B), a section on the order page, a summary
above the order list and attention flags. Nothing here changes an order; the only
actions on orders are still the dispute actions of api/admin.py.

Database only, except the chain tip (rescue timelock) and the mempool fee check, which
ask the coordinator's node and are skipped when it does not answer.
"""

from datetime import timedelta

from django.contrib import admin
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone
from django.utils.html import format_html, format_html_join

from roblosats_admin.disputes import currency_text, fmt_sats, fmt_time
from roblosats_admin.i18n import _, _lazy

EXPLORER = "https://mempool.kilombino.com/tx/"
URGENT = timedelta(minutes=30)
# Orders that are no longer moving (cancelled, expired, finished).
FINAL = {4, 5, 12, 14, 17, 18}
# Taken and not finished.
TRADING = {6, 7, 8, 9, 10, 11, 13, 15, 16}

HELD = (2, 3, 4)  # OnchainEscrow MEMPOOL, LOCKED, RELEASING: the escrow output is unspent

LIGHTNING, ONCHAIN, HYBRID_A, HYBRID_B = "ln", "onchain", "hybrid_a", "hybrid_b"
KIND_LABEL = {
    LIGHTNING: _lazy("Lightning"),
    ONCHAIN: _lazy("On-chain"),
    HYBRID_A: _lazy("Hybrid A (Lightning buyer)"),
    HYBRID_B: _lazy("Hybrid B (Lightning seller)"),
}
KIND_ICON = {LIGHTNING: "⚡", ONCHAIN: "⛓️", HYBRID_A: "⛓️⚡", HYBRID_B: "⚡⛓️"}


# ---------------------------------------------------------------- basics


def _txid(value):
    """A txid from what the models keep: a string, a coin [txid, vout, sats] or a record
    {"txid": ...} (bond leftovers)."""
    if isinstance(value, dict):
        value = value.get("txid")
    elif isinstance(value, (list, tuple)):
        value = value[0] if value else None
    value = str(value or "")
    return value if len(value) == 64 and all(c in "0123456789abcdef" for c in value.lower()) else ""


def tx_link(txid):
    txid = _txid(txid)
    if not txid:
        return ""
    return format_html(
        '<a href="{}{}" target="_blank" rel="noopener noreferrer"><code>{}…{}</code></a>',
        EXPLORER,
        txid,
        txid[:10],
        txid[-6:],
    )


def tx_links(txids):
    txids = [_txid(t) for t in txids or []]
    txids = [t for t in txids if t]
    if not txids:
        return ""
    return format_html_join(" ", "{}", ((tx_link(t),) for t in txids))


def _escrow(order):
    try:
        return order.onchain_escrow
    except Exception:  # noqa: BLE001 (RelatedObjectDoesNotExist)
        return None


def _has_bonds(order):
    flag = getattr(order, "_has_onchain_bonds", None)
    if flag is not None:
        return bool(flag)
    return order.onchain_bonds.exists()


def kind(order):
    from api.models import Order

    if order.escrow_type == Order.EscrowTypes.ONCHAIN:
        return HYBRID_A if order.taker_lightning and order.taker_id else ONCHAIN
    if _has_bonds(order) or _escrow(order) is not None:
        return HYBRID_B
    return LIGHTNING


def is_onchain_related(order):
    return kind(order) != LIGHTNING


def tip():
    """Chain tip of the coordinator's node, cached a minute (None if it does not answer)."""
    from django.core.cache import cache

    key = "roblosats-admin:onchain-tip"
    value = cache.get(key)
    if value is not None:
        return value or None
    try:
        from api.escrow import service

        value = int(service.rpc("getblockcount"))
    except Exception:  # noqa: BLE001
        value = 0
    cache.set(key, value, 60 if value else 30)
    return value or None


def rescue_height(escrow):
    if escrow is None or not escrow.confirmed_height:
        return None
    return escrow.confirmed_height + escrow.rescue_csv


def price_text(order):
    """Fixed price / '丰 sat' order, '' for a market-price order in its currency."""
    parts = []
    currency = currency_text(order)
    if getattr(order, "sats_denominated", False):
        parts.append("丰 sat")
    if getattr(order, "fixed_price", None) is not None:
        parts.append(_("fixed %(p)s %(c)s/BTC") % {"p": f"{float(order.fixed_price):,.2f}", "c": currency})
    elif getattr(order, "contract_price", None) is not None and getattr(order, "sats_denominated", False):
        parts.append(_("%(p)s %(c)s/BTC at contract") % {"p": f"{float(order.contract_price):,.2f}", "c": currency})
    return " · ".join(parts)


# ---------------------------------------------------------------- flags


def flags(order, now=None, height=None, with_mempool=False, hint=None):
    """Things the coordinator should look at in a live on-chain order: [text]."""
    from api.models import OnchainBond, OnchainEscrow

    if order.status in FINAL or not is_onchain_related(order):
        return []
    now = now or timezone.now()
    out = []
    escrow = _escrow(order)
    bonds = list(order.onchain_bonds.all())
    waiting_payment = any(b.status == OnchainBond.Status.FUNDING for b in bonds) or (
        escrow is not None and escrow.status in (OnchainEscrow.Status.KEYS, OnchainEscrow.Status.FUNDING)
    )
    if waiting_payment and order.expires_at:
        left = order.expires_at - now
        if timedelta(0) < left < URGENT:
            out.append(_("deposit window ends in %(m)s min") % {"m": int(left.total_seconds() // 60) + 1})

    if escrow is not None:
        from api.escrow import service

        data = escrow.funding_signatures or {}
        if escrow.status == OnchainEscrow.Status.FUNDING:
            if service.rescue_plan(escrow) >= 2 and data.get("rescue"):
                missing = [r for r in service.ROLES if not data.get(f"{r}_rescue_sig")]
                if missing:
                    out.append(_("rescue signature missing: %(who)s") % {"who": ", ".join(missing)})
            missing = [r for r in service.ROLES if f"{r}_inputs" in (data.get("plan") or {}) and not data.get(f"{r}_sigs")]
            if missing:
                out.append(_("deposit signature missing: %(who)s") % {"who": ", ".join(missing)})
        if escrow.status == OnchainEscrow.Status.RELEASING and not escrow.release_txid:
            if escrow.release_kind == OnchainEscrow.ReleaseKind.COOPERATIVE:
                from api.escrow import flow as flow_

                if flow_.seller_leaf(escrow) and service.leaf_release_ok(escrow):
                    # 0.9.5: one round, only the seller's signature (the buyer signs nothing)
                    out.append(_("release waits for the seller's signature (one signature)"))
                else:
                    missing = [r for r in service.ROLES if r not in (escrow.release_partial_sigs or {})]
                    out.append(_("release signature missing: %(who)s") % {"who": ", ".join(missing) or "?"})
            else:
                out.append(_("release waits for the winner's signature"))
        due = rescue_height(escrow)
        if due and escrow.status in (
            OnchainEscrow.Status.MEMPOOL,
            OnchainEscrow.Status.LOCKED,
            OnchainEscrow.Status.RELEASING,
        ):
            height = height if height is not None else tip()
            if height and height >= due:
                out.append(_("rescue timelock reached (block %(h)s)") % {"h": due})

    if with_mempool and waiting_payment:
        try:
            from api.escrow import flow

            hint = hint if hint is not None else flow.fee_hint()
            for item in flow.mempool_seen(order, escrow):
                needed = flow.low_fee(item.get("fee_rate"), hint)
                if needed is not None:
                    out.append(
                        _("%(what)s of the %(role)s in the mempool at %(r)s sat/vB (1 hour needs %(n)s)")
                        % {"what": item["what"], "role": item["role"], "r": f"{item['fee_rate']:g}", "n": f"{needed:g}"}
                    )
        except Exception:  # noqa: BLE001 (node down: no mempool check)
            pass
    return out


# ---------------------------------------------------------------- order page


def release_path_text(escrow):
    """0.9.5: how the cooperative release was (or will be) signed."""
    from api.escrow import flow
    from api.models import OnchainEscrow

    if escrow.release_kind != OnchainEscrow.ReleaseKind.COOPERATIVE:
        return ""
    data = escrow.funding_signatures or {}
    if data.get("release_path") == "seller_leaf":
        return _("seller's leaf: the seller signed alone, with the coordinator")
    if escrow.release_txid:
        return _("key path: both traders signed together (MuSig2)")
    try:
        if flow.seller_leaf(escrow):
            return _("the seller's app can sign it alone (one signature)")
    except Exception:  # noqa: BLE001
        pass
    return _("both traders sign together (MuSig2)")



def _bond_row(bond):
    from api.escrow import bonds as bond_service
    from api.models import OnchainBond

    coins = [c for c in bond.coins or [] if c]
    penalty = bond.spend_txid if bond.status == OnchainBond.Status.PENALIZED else ""
    returned = bond.spend_txid if bond.status in (OnchainBond.Status.RETURNED, OnchainBond.Status.IN_DEPOSIT) else ""
    return {
        "id": bond.pk,
        "role": bond.role,
        "owner": bond.owner.username if bond.owner_id else "",
        "status": _(bond.get_status_display()),  # model labels, translated in locale/
        "required": fmt_sats(bond.required_sats),
        "value": fmt_sats(bond.value) if bond.value else "",
        "coins": len(coins) or (1 if bond.txid else 0),
        "funding": tx_links(coins or [bond.txid]),
        "penalty": tx_link(penalty),
        "penalty_sats": fmt_sats(bond_service.penalty_sats(bond)) if penalty else "",
        "returned": tx_link(returned) if bond.status == OnchainBond.Status.RETURNED else "",
        "into_deposit": bond.status == OnchainBond.Status.IN_DEPOSIT,
        "leftover": tx_links(bond.leftover_txids or []),
        "leftover_pending": bool(bond.leftover_coins) and not bond.leftover_txids,
        "address": bond.address,
        "auto_return": auto_return_text(bond),
        "returned_status": status_text(returned) if bond.status == OnchainBond.Status.RETURNED else "",
    }


def status_text(txid):
    """0.9.7: "confirmed" / "in the mempool" from the node (cached), "" when it does not answer."""
    from api.order_summary import tx_status

    st = tx_status(_txid(txid)) if _txid(txid) else None
    return {"confirmed": _("confirmed"), "mempool": _("in the mempool")}.get(st, "")


def _auto_return_txid(bond):
    try:
        from api.escrow import bonds as bond_service

        return bond_service.auto_return_message(bond)[0].txid()
    except Exception:  # noqa: BLE001
        return ""


def auto_return_text(bond):
    """0.9.7: what became of the bond's return signed in advance (stored, sent or by hand)."""
    from api.models import OnchainBond

    ar = getattr(bond, "auto_return", None) or {}
    S = OnchainBond.Status
    if not ar.get("address"):
        if bond.status in (S.LOCKED, S.RETURNABLE):
            return _("not set: the owner takes it back by hand")
        return ""
    addr = ar["address"]
    short = f"{addr[:10]}…{addr[-6:]}" if len(addr) > 20 else addr
    if bond.status == S.RETURNED and ar.get("sigs") and bond.spend_txid and bond.spend_txid == _auto_return_txid(bond):
        return _("sent by itself to %(a)s") % {"a": short}
    if not ar.get("sigs"):
        if bond.status in (S.RETURNED, S.IN_DEPOSIT, S.PENALIZED):
            return ""
        return _("to %(a)s, not signed yet: the owner takes it back by hand") % {"a": short}
    if bond.status == S.RETURNABLE:
        return _("signed to %(a)s but not sent (see the log): the owner takes it back by hand") % {"a": short}
    if bond.status in (S.FUNDING, S.SIGN, S.LOCKED):
        return _("signed: goes back by itself to %(a)s when the bond is free") % {"a": short}
    return ""


def order_section(order, now=None):
    """Context for the on-chain section of the order page; None for a Lightning order."""
    from api.escrow import flow, service
    from api.models import OnchainEscrow

    k = kind(order)
    if k == LIGHTNING:
        return None
    now = now or timezone.now()
    escrow = _escrow(order)
    bonds = [_bond_row(b) for b in order.onchain_bonds.select_related("owner").order_by("created_at")]
    section = {
        "kind": KIND_LABEL[k],
        "icon": KIND_ICON[k],
        "bonds": bonds,
        "escrow": None,
        "price": price_text(order),
        "swap_extra": fmt_sats(order.swap_surcharge_sats) if getattr(order, "swap_surcharge_sats", 0) else "",
        "swap_extra_back": fmt_sats(order.swap_surcharge_refund_sats)
        if getattr(order, "swap_surcharge_refund_sats", 0)
        else "",
    }
    if escrow is not None:
        data = escrow.funding_signatures or {}
        try:
            charged = flow.charged_fees(escrow, order)
        except Exception:  # noqa: BLE001
            charged = {"seller": 0, "buyer": 0}
        due = rescue_height(escrow)
        height = tip() if due else None
        rescue = data.get("rescue") or {}
        sweeps = [
            {"role": r, "tx": tx_link(data.get(f"sweep_{r}_txid")), "sats": fmt_sats(data.get(f"sweep_{r}_sats") or 0)}
            for r in service.ROLES
            if data.get(f"sweep_{r}_txid")
        ]
        section["escrow"] = {
            "status": _(escrow.get_status_display()),
            "sats": fmt_sats(escrow.escrow_sats),
            "address": escrow.address,
            "fee_plan": flow.fee_plan(escrow),
            "rescue_plan": service.rescue_plan(escrow),
            "extra_plan": service.extra_plan(escrow),
            "seller_fee": fmt_sats(charged.get("seller") or 0),
            "buyer_fee": fmt_sats(charged.get("buyer") or 0),
            "coordinator_received": fmt_sats(flow.proceeds(order)),
            "dev_paid": fmt_sats(flow.dev_paid(escrow)),
            "deposit": tx_link(escrow.funding_txid),
            "confirmed_height": escrow.confirmed_height or "",
            "release": tx_link(escrow.release_txid),
            # 0.9.5: a cooperative release may go through the seller's leaf, so the model's label
            # ("key path") would be wrong: "cooperative" plus how it was signed (release_path)
            "release_kind": (
                _("cooperative")
                if escrow.release_kind == OnchainEscrow.ReleaseKind.COOPERATIVE
                else _(escrow.get_release_kind_display())
            )
            if escrow.release_kind is not None
            else "",
            "release_status": status_text(escrow.release_txid),
            "release_path": release_path_text(escrow),
            "hybrid_plan": int(data.get("hybrid_plan") or 0),
            "cancelled": bool(data.get("cancel")),
            "rescue_csv": escrow.rescue_csv,
            "rescue_height": due or "",
            # only while the escrow is still on chain: after a release the rescue no longer exists
            "rescue_left": (due - height) if (due and height and escrow.status in HELD) else None,
            "rescue_to_seller": fmt_sats(rescue.get("to_seller") or 0) if rescue else "",
            "rescue_to_buyer": fmt_sats(rescue.get("to_buyer") or 0) if rescue else "",
            "sweeps": sweeps,
        }
    section["flags"] = flags(order, now=now, with_mempool=True)
    return section


# ---------------------------------------------------------------- order list


def annotate(queryset):
    from api.models import OnchainBond

    return queryset.select_related("onchain_escrow").annotate(
        _has_onchain_bonds=Exists(OnchainBond.objects.filter(order=OuterRef("pk")))
    )


def escrow_column(order):
    k = kind(order)
    if k == LIGHTNING:
        return format_html("{} {}", KIND_ICON[k], KIND_LABEL[k])
    escrow = _escrow(order)
    status = _(escrow.get_status_display()) if escrow is not None else _("bonds only")
    warn = flags(order, with_mempool=False)
    return format_html(
        '{} {}<br><small>{}</small>{}',
        KIND_ICON[k],
        KIND_LABEL[k],
        status,
        format_html('<br><small style="color:rgb(234 88 12);font-weight:600">⚠ {}</small>', "; ".join(warn))
        if warn
        else "",
    )


escrow_column.short_description = _lazy("Escrow")


def price_column(order):
    return price_text(order) or "—"


price_column.short_description = _lazy("Price")


class EscrowKindFilter(admin.SimpleListFilter):
    title = _lazy("escrow")
    parameter_name = "escrow_kind"

    def lookups(self, request, model_admin):
        return [(k, KIND_LABEL[k]) for k in (LIGHTNING, ONCHAIN, HYBRID_A, HYBRID_B)]

    def queryset(self, request, queryset):
        from api.models import OnchainBond, Order

        bonds = Exists(OnchainBond.objects.filter(order=OuterRef("pk")))
        value = self.value()
        if value == LIGHTNING:
            return queryset.filter(escrow_type=Order.EscrowTypes.LIGHTNING).exclude(bonds)
        if value == ONCHAIN:
            return queryset.filter(escrow_type=Order.EscrowTypes.ONCHAIN).filter(
                Q(taker_lightning=False) | Q(taker__isnull=True)
            )
        if value == HYBRID_A:
            return queryset.filter(
                escrow_type=Order.EscrowTypes.ONCHAIN, taker_lightning=True, taker__isnull=False
            )
        if value == HYBRID_B:
            return queryset.filter(escrow_type=Order.EscrowTypes.LIGHTNING).filter(bonds)
        return queryset


class OnchainStatusFilter(admin.SimpleListFilter):
    title = _lazy("on-chain escrow status")
    parameter_name = "onchain_status"

    def lookups(self, request, model_admin):
        from api.models import OnchainEscrow

        return [(str(v), label) for v, label in OnchainEscrow.Status.choices]

    def queryset(self, request, queryset):
        if self.value() is None:
            return queryset
        try:
            return queryset.filter(onchain_escrow__status=int(self.value()))
        except ValueError:
            return queryset


# ---------------------------------------------------------------- summary


def summary(now=None, limit=25):
    """Above the order list: on-chain trades in progress, sats held, what came in."""
    from django.db.models import Sum

    from api.escrow import bonds as bond_service
    from api.escrow import flow
    from api.models import OnchainBond, OnchainEscrow, Order

    now = now or timezone.now()
    held_escrow = OnchainEscrow.Status.MEMPOOL, OnchainEscrow.Status.LOCKED, OnchainEscrow.Status.RELEASING
    live = annotate(
        Order.objects.exclude(status__in=FINAL).filter(
            Q(escrow_type=Order.EscrowTypes.ONCHAIN) | Q(onchain_bonds__isnull=False) | Q(onchain_escrow__isnull=False)
        )
    ).distinct().select_related("currency").order_by("-created_at")
    height = tip()
    try:
        hint = flow.fee_hint()
    except Exception:  # noqa: BLE001
        hint = None
    rows = []
    for order in live[:limit]:
        rows.append(
            {
                "id": order.pk,
                "kind": f"{KIND_ICON[kind(order)]} {KIND_LABEL[kind(order)]}",
                "status": order.get_status_display(),
                "escrow": _escrow(order).get_status_display() if _escrow(order) is not None else "",
                "left": fmt_time(order.expires_at) if order.expires_at else "",
                "flags": flags(order, now=now, height=height, with_mempool=True, hint=hint),
            }
        )

    def window(days):
        since = now - timedelta(days=days)
        penalties = sum(
            bond_service.penalty_sats(b)
            for b in OnchainBond.objects.filter(status=OnchainBond.Status.PENALIZED, updated_at__gte=since)
        )
        fees = dev = 0
        for escrow in OnchainEscrow.objects.filter(updated_at__gte=since).exclude(release_txid="").select_related("order"):
            fees += flow.proceeds(escrow.order)
            dev += flow.dev_paid(escrow)
        return {"penalties": fmt_sats(penalties), "fees": fmt_sats(fees), "dev": fmt_sats(dev)}

    return {
        "trading": live.filter(status__in=TRADING).count(),
        "open": live.filter(status__in=[0, 1, 2, 3]).count(),
        "in_escrow": fmt_sats(
            OnchainEscrow.objects.filter(status__in=held_escrow).aggregate(s=Sum("escrow_sats"))["s"] or 0
        ),
        "in_bonds": fmt_sats(
            OnchainBond.objects.filter(status__in=[OnchainBond.Status.SIGN, OnchainBond.Status.LOCKED]).aggregate(
                s=Sum("value")
            )["s"]
            or 0
        ),
        "day": window(1),
        "month": window(30),
        "rows": rows,
        "attention": sum(1 for r in rows if r["flags"]),
        "height": height,
    }


# ---------------------------------------------------------------- model admins


class _ReadOnly:
    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class OnchainBondInline(_ReadOnly, admin.TabularInline):
    from api.models import OnchainBond as model  # noqa: N813

    fk_name = "order"
    extra = 0
    can_delete = False
    fields = ("role", "owner", "status", "required_sats", "value", "funding_links", "spend_link", "leftover_links")
    readonly_fields = fields
    verbose_name_plural = _lazy("On-chain bonds")

    @admin.display(description=_lazy("Funding"))
    def funding_links(self, bond):
        return tx_links([c for c in bond.coins or [] if c] or [bond.txid])

    @admin.display(description=_lazy("Penalty / return"))
    def spend_link(self, bond):
        return tx_link(bond.spend_txid)

    @admin.display(description=_lazy("Leftovers"))
    def leftover_links(self, bond):
        return tx_links(bond.leftover_txids)


class OnchainEscrowInline(_ReadOnly, admin.StackedInline):
    from api.models import OnchainEscrow as model  # noqa: N813

    extra = 0
    can_delete = False
    fields = (
        "status",
        "escrow_sats",
        "coordinator_fee_sats",
        "dev_fee_sats",
        "address",
        "deposit_link",
        "confirmed_height",
        "rescue_csv",
        "release_kind",
        "release_link",
        "release_fee_rate",
        "created_at",
        "updated_at",
    )
    readonly_fields = fields
    verbose_name_plural = _lazy("On-chain escrow")

    @admin.display(description=_lazy("Deposit"))
    def deposit_link(self, escrow):
        return tx_link(escrow.funding_txid)

    @admin.display(description=_lazy("Release"))
    def release_link(self, escrow):
        return tx_link(escrow.release_txid)


def register(site):
    from api.models import OnchainBond, OnchainEscrow

    if OnchainBond not in site._registry:

        @admin.register(OnchainBond, site=site)
        class OnchainBondAdmin(_ReadOnly, admin.ModelAdmin):
            list_display = ("id", "order_id", "role", "owner", "status", "required_sats", "value", "funding", "spend", "updated_at")
            list_filter = ("status", "role")
            search_fields = ["order__id", "txid", "spend_txid", "owner__username", "address"]
            ordering = ("-created_at",)
            list_per_page = 50
            exclude = ("penalty_tx", "return_tx", "leftover_tx", "penalty_owner_sig", "penalty_owner_sigs")

            @admin.display(description=_lazy("Funding"))
            def funding(self, bond):
                return tx_links([c for c in bond.coins or [] if c] or [bond.txid])

            @admin.display(description=_lazy("Penalty / return"))
            def spend(self, bond):
                return tx_link(bond.spend_txid)

    if OnchainEscrow not in site._registry:

        @admin.register(OnchainEscrow, site=site)
        class OnchainEscrowAdmin(_ReadOnly, admin.ModelAdmin):
            list_display = ("id", "order_id", "status", "escrow_sats", "deposit", "release", "release_kind", "confirmed_height", "updated_at")
            list_filter = ("status", "release_kind")
            search_fields = ["order__id", "funding_txid", "release_txid", "address", "order__maker__username", "order__taker__username"]
            ordering = ("-created_at",)
            list_per_page = 50
            exclude = ("funding_tx", "release_tx", "funding_signatures", "release_nonces", "release_partial_sigs")

            @admin.display(description=_lazy("Deposit"))
            def deposit(self, escrow):
                return tx_link(escrow.funding_txid)

            @admin.display(description=_lazy("Release"))
            def release(self, escrow):
                return tx_link(escrow.release_txid)
