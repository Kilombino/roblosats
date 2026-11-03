from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils import timezone

from roblosats_admin import disputes
from roblosats_admin.i18n import _, _lazy
from roblosats_admin.messaging import MESSAGE_MAX, message_head

EXPIRY_REASONS = {
    0: _lazy("nobody took it"),
    1: _lazy("the maker's bond was not locked"),
    2: _lazy("the seller's escrow was not locked"),
    3: _lazy("the buyer's invoice was not submitted"),
    4: _lazy("neither the escrow nor the invoice arrived"),
}

ONCHAIN_STATUS = {
    0: _lazy("created, no address yet"),
    1: _lazy("address submitted"),
    2: _lazy("in the mempool"),
    3: _lazy("confirmed"),
    4: _lazy("cancelled"),
    5: _lazy("queued to be sent"),
}

TONE = {11: "alert", 14: "ok", 15: "alert", 16: "alert"}

LIVE = {0, 1, 2, 3, 6, 7, 8, 9, 10, 11, 13, 15, 16}
CAN_WRITE = {3, 6, 7, 8, 9, 10, 11, 13, 15, 16}


def page_url(order):
    name = "admin:api_order_dispute" if order.is_disputed else "admin:api_order_summary"
    return reverse(name, args=[order.pk])


def public_url(order):
    try:
        from api.notifications import Notifications

        return Notifications().order_url(order)
    except Exception:  # noqa: BLE001
        return ""


def chat_summary(order, label_by_user):
    """Chat messages that went through the coordinator. Their contents are
    PGP-encrypted between the parties, so only how many, who and when."""
    try:
        from chat.models import Message

        rows = []
        counts = {}
        for m in (
            Message.objects.filter(order=order)
            .order_by("created_at", "index")
            .only("index", "sender_id", "created_at")
        ):
            who = label_by_user.get(m.sender_id, _("(unknown)"))
            counts[who] = counts.get(who, 0) + 1
            rows.append(
                {"index": m.index, "who": who, "time": disputes.fmt_time(m.created_at)}
            )
    except Exception:  # noqa: BLE001
        return None
    return {
        "total": len(rows),
        "by_who": [{"who": w, "n": n} for w, n in counts.items()],
        "first": rows[0]["time"] if rows else "",
        "last": rows[-1]["time"] if rows else "",
        "rows": rows,
    }


def onchain_payout(tx):
    if tx is None:
        return {"exists": False}
    return {
        "exists": True,
        "sats": disputes.fmt_sats(tx.num_satoshis),
        "status": ONCHAIN_STATUS.get(tx.status, tx.get_status_display()),
        "url": disputes._url("admin:api_onchainpayment_change", tx.pk),
        "address": tx.address or "",
        "txid": tx.txid or "",
        "mining_fee": disputes.fmt_sats(tx.mining_fee_sats)
        if tx.mining_fee_sats
        else "",
    }


def routing_fee(payout):
    """Routing fee paid on the buyer's Lightning payout, once it was tried."""
    from api.models import LNPayment

    if payout is None or payout.fee is None:
        return ""
    if not payout.fee and payout.status != LNPayment.Status.SUCCED:
        return ""
    return f"{float(payout.fee):g}"


def winner_text(order, r):
    """Who won a resolved dispute: MLD = the maker lost, TLD = the taker lost."""
    from api.models import Order

    if order.status == Order.Status.MLD:
        winner_side = "taker"
    elif order.status == Order.Status.TLD:
        winner_side = "maker"
    else:
        return ""
    if r["buyer_side"] == winner_side:
        return _("the buyer won")
    return _("the seller won")


def _party(user, role, side):
    robot = user.robot if user else None
    return {
        "role": role,
        "role_label": disputes.role_name(role),
        "side_label": disputes.side_name(side),
        "nick": user.username if user else _("(nobody)"),
        "robot_url": disputes._url("admin:api_robot_change", robot.pk) if robot else "",
        "contracts": robot.total_contracts if robot else 0,
        "disputes": robot.num_disputes if robot else 0,
        "lost": robot.lost_disputes if robot else 0,
        "telegram": bool(robot and robot.telegram_enabled),
        "nostr": bool(robot and robot.nostr_pubkey),
        "nobody": user is None,
        "show_statement": False,
    }


def _context(order_admin, request, order):
    from api.models import Order

    r = disputes.roles(order)
    buyer, seller = r["buyer"], r["seller"]
    role_by_nick = {}
    label_by_robot = {}
    label_by_user = {}
    for role, user in (("buyer", buyer), ("seller", seller)):
        if user:
            label = f"{disputes.role_name(role)} ({user.username})"
            role_by_nick[user.username] = role
            label_by_robot[user.robot.id] = label
            label_by_user[user.pk] = label

    deadline = None
    if order.expires_at and order.status in LIVE:
        sec = (order.expires_at - timezone.now()).total_seconds()
        deadline = {
            "time": disputes.fmt_time(order.expires_at),
            "expired": sec <= 0,
            "text": (
                _("%(t)s left") % {"t": disputes.fmt_duration(sec)}
                if sec > 0
                else _("expired %(t)s ago") % {"t": disputes.fmt_duration(sec)}
            ),
        }

    status = str(disputes.ORDER_STATUS.get(order.status, order.get_status_display()))
    if order.status == Order.Status.EXP and order.expiry_reason is not None:
        reason = EXPIRY_REASONS.get(order.expiry_reason)
        if reason:
            status += f" ({reason})"
    winner = winner_text(order, r)
    if winner:
        status += f" ({winner})"

    sats = order.last_satoshis or order.satoshis or order.t0_satoshis
    page = reverse("admin:api_order_summary", args=[order.pk])
    can_change = order_admin.has_change_permission(request, order)
    chat = chat_summary(order, label_by_user)
    return {
        **order_admin.admin_site.each_context(request),
        "title": _("Order %(id)s") % {"id": order.pk},
        "opts": order_admin.model._meta,
        "order": order,
        "page_url": page,
        "change_url": disputes._url("admin:api_order_change", order.pk) + "?raw=1",
        "list_url": reverse("admin:api_order_changelist"),
        "dispute_url": reverse("admin:api_order_dispute", args=[order.pk])
        if order.is_disputed
        else "",
        "dispute_open": disputes.is_open_dispute(order),
        "type_label": _("BUY") if order.type == Order.Types.BUY else _("SELL"),
        "type_explained": (
            _("the maker buys sats")
            if order.type == Order.Types.BUY
            else _("the maker sells sats")
        ),
        "amount": disputes.amount_text(order),
        "currency": str(order.currency) if order.currency else "",
        "method": order.payment_method,
        "sats": disputes.fmt_sats(sats) if sats else "?",
        "spamcoin_sats": disputes.fmt_sats(order.spamcoin_sats)
        if order.spamcoin_sats is not None
        else "",
        "premium": f"{float(order.premium):g}" if order.premium is not None else "0",
        "bond_size": f"{float(order.bond_size):g}"
        if order.bond_size is not None
        else "?",
        "status": status,
        "tone": TONE.get(order.status, ""),
        "created": disputes.fmt_time(order.created_at),
        "contract_time": disputes.fmt_time(order.contract_finalization_time)
        if order.taker_id
        else "",
        "deadline": deadline,
        "tz": timezone.get_current_timezone_name(),
        "buyer": _party(buyer, "buyer", r["buyer_side"]),
        "seller": _party(seller, "seller", r["seller_side"]),
        "buyer_bond": disputes._payment(r["buyer_bond"]),
        "seller_bond": disputes._payment(r["seller_bond"]),
        "escrow": disputes._payment(order.trade_escrow),
        "payout": disputes._payment(order.payout) if not order.is_swap else None,
        "payout_tx": onchain_payout(order.payout_tx) if order.is_swap else None,
        "routing_fee": routing_fee(order.payout) if not order.is_swap else "",
        "earnings": disputes.fmt_sats(order.proceeds or 0),
        "fiat_sent": order.is_fiat_sent,
        "is_swap": order.is_swap,
        "chat": chat,
        "chat_count": chat["total"] if chat is not None else None,
        "sent": disputes.sent_messages(order, label_by_robot),
        "log": disputes.render_log(order.logs, role_by_nick),
        "public_url": public_url(order),
        "can_change": can_change,
        "can_write": can_change and order.status in CAN_WRITE and bool(buyer or seller),
        "max_message": MESSAGE_MAX,
        "message_head": message_head(order.pk),
        "languages": disputes._languages(request),
    }


def order_view(order_admin, request, order_id):
    from api.models import Order

    if not (request.user.is_active and request.user.is_staff):
        raise PermissionDenied
    order = get_object_or_404(
        Order.objects.select_related(
            "maker__robot",
            "taker__robot",
            "currency",
            "maker_bond",
            "taker_bond",
            "trade_escrow",
            "payout",
            "payout_tx",
        ),
        pk=order_id,
    )
    page = reverse("admin:api_order_summary", args=[order.pk])
    if request.method == "POST":
        if not order_admin.has_change_permission(request, order):
            raise PermissionDenied
        if request.POST.get("action") == "message":
            return disputes.message_post(request, order, page)
        messages.error(request, _("Unknown action."))
        return HttpResponseRedirect(page)
    return TemplateResponse(
        request, "roblosats_admin/order.html", _context(order_admin, request, order)
    )
