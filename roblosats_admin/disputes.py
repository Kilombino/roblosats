import json
import re
from datetime import datetime
from datetime import timezone as dt_timezone
from types import SimpleNamespace

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.template.response import TemplateResponse
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.utils.html import escape
from django.utils.safestring import mark_safe
from roblosats_admin.i18n import _, _lazy

from roblosats_admin.messaging import (
    MESSAGE_MAX,
    message_head,
    message_title_prefix,
    send_coordinator_message,
)
from roblosats_admin.middleware import PANEL_LANGUAGES

ORDER_STATUS = {
    0: _lazy("Waiting for the maker's bond"),
    1: _lazy("Public in the order book"),
    2: _lazy("Paused"),
    3: _lazy("Waiting for the taker's bond"),
    4: _lazy("Cancelled"),
    5: _lazy("Expired"),
    6: _lazy("Waiting for the seller's escrow and the buyer's invoice"),
    7: _lazy("Waiting for the seller's escrow"),
    8: _lazy("Waiting for the buyer's invoice"),
    9: _lazy("In the chat: the buyer has to pay"),
    10: _lazy("In the chat: the buyer says they have paid"),
    11: _lazy("In dispute: collecting statements"),
    12: _lazy("Collaboratively cancelled"),
    13: _lazy("Paying the sats to the buyer"),
    14: _lazy("Completed successfully"),
    15: _lazy("Lightning payment to the buyer failed"),
    16: _lazy("In dispute: both statements are in, waiting for your decision"),
    17: _lazy("Dispute resolved: the maker lost"),
    18: _lazy("Dispute resolved: the taker lost"),
}

PAYMENT_STATUS = {
    0: _lazy("invoice generated, unpaid"),
    1: _lazy("locked (held)"),
    2: _lazy("settled by the coordinator"),
    3: _lazy("returned"),
    4: _lazy("cancelled"),
    5: _lazy("expired"),
    6: _lazy("valid invoice"),
    7: _lazy("payment in flight"),
    8: _lazy("paid"),
    9: _lazy("payment failed"),
}

DECISIONS = {
    "buyer": _lazy("Buyer wins"),
    "seller": _lazy("Seller wins"),
    "return": _lazy("Return everything"),
    "completed": _lazy("Trade completed (pay the buyer)"),
}

RECIPIENTS = ("buyer", "seller", "both")

_RE_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_RE_CONTACTS = [
    re.compile(r"(?:https?://)?t\.me/[A-Za-z0-9_+/-]+"),
    re.compile(r"(?<![\w.])@[A-Za-z][A-Za-z0-9_]{3,31}"),
    re.compile(r"npub1[02-9ac-hj-np-z]{58}"),
    re.compile(r"https?://(?:www\.)?simplex\.chat/\S+|smp://\S+"),
    re.compile(r"(?:session|signal)[:\s]+\S+", re.I),
    re.compile(r"\+\d[\d ]{7,16}\d"),
]
_RE_LOG_OBJ = re.compile(
    r"(Robot|LNPayment|Order|OnchainPayment)\(([0-9a-fA-F\-]+),\s*([^)]+)\)"
)


def role_name(role):
    return {"buyer": _("buyer"), "seller": _("seller")}.get(role, role)


def side_name(side):
    return {"maker": _("maker"), "taker": _("taker")}.get(side, side)


def fmt_time(moment):
    if moment is None:
        return ""
    if timezone.is_naive(moment):
        moment = timezone.make_aware(moment, dt_timezone.utc)
    return timezone.localtime(moment).strftime("%Y-%m-%d %H:%M")


def fmt_duration(seconds):
    seconds = abs(int(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return _("%(d)s d %(h)s h") % {"d": days, "h": hours}
    if hours:
        return _("%(h)s h %(m)s min") % {"h": hours, "m": minutes}
    return _("%(m)s min") % {"m": minutes}


def fmt_sats(n):
    return f"{int(n):,}".replace(",", " ") if n is not None else "?"


def _truthy(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes")


def _url(name, *args):
    try:
        return reverse(name, args=args)
    except NoReverseMatch:
        return ""


def parse_statement(raw):
    if raw in (None, ""):
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        data = None
    if isinstance(data, dict):
        msgs = data.get("messages") or []
        return {
            "text": str(data.get("statement") or ""),
            "messages": [m for m in msgs if isinstance(m, dict)]
            if isinstance(msgs, list)
            else [],
        }
    return {"text": str(raw), "messages": []}


def find_contacts(text):
    found = []
    for email in _RE_EMAIL.findall(text or ""):
        if email not in found:
            found.append(email)
    rest = _RE_EMAIL.sub(" ", text or "")
    for pattern in _RE_CONTACTS:
        for piece in pattern.findall(rest):
            piece = piece.strip().rstrip(".,;)")
            if piece and piece not in found:
                found.append(piece)
    return found


def _parse_time(value):
    try:
        moment = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if timezone.is_naive(moment):
        moment = moment.replace(tzinfo=dt_timezone.utc)
    return moment


def merge_chat(statements, role_by_nick):
    by_index = {}
    for source, statement in statements:
        if not statement:
            continue
        for n, m in enumerate(statement["messages"]):
            key = str(m.get("index", "")).strip() or f"{source}-{n}"
            text = str(m.get("plainTextMessage") or "")
            signed = _truthy(m.get("validSignature", False))
            nick = str(m.get("userNick") or "")
            if key in by_index:
                entry = by_index[key]
                entry["sources"].add(source)
                entry["signature_ok"] = entry["signature_ok"] and signed
                if entry["text"] != text or entry["nick"] != nick:
                    entry["mismatch"] = True
                    entry["other_text"] = text
                continue
            moment = _parse_time(m.get("time"))
            by_index[key] = {
                "index": key,
                "text": text,
                "nick": nick,
                "role": role_by_nick.get(nick, ""),
                "role_label": role_name(role_by_nick.get(nick, "")),
                "moment": moment,
                "time": fmt_time(moment),
                "signature_ok": signed,
                "sources": {source},
                "mismatch": False,
                "other_text": "",
            }

    def sort_key(e):
        try:
            num = int(e["index"])
        except ValueError:
            num = 10**9
        return (e["moment"] is None, e["moment"] or timezone.now(), num)

    chat = sorted(by_index.values(), key=sort_key)
    for e in chat:
        e["sources"] = _(" and ").join(role_name(s) for s in sorted(e["sources"]))
    return chat


def render_log(raw, role_by_nick):
    try:
        entries = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        entries = []
    if not isinstance(entries, list):
        entries = []
    rows = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        event = escape(str(entry.get("event", "")))
        event = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", event, flags=re.DOTALL)
        event = re.sub(r"\*(.+?)\*", r"<i>\1</i>", event, flags=re.DOTALL)

        def link(m):
            kind, ident, name = m.group(1), m.group(2), m.group(3)
            url = _url(f"admin:api_{kind.lower()}_change", ident)
            role = role_by_nick.get(name.strip(), "") if kind == "Robot" else ""
            extra = (
                f' <span class="rsd-tag">{escape(role_name(role))}</span>'
                if role
                else ""
            )
            if url:
                return f'<a href="{escape(url)}">{name}</a>{extra}'
            return f"{name}{extra}"

        event = _RE_LOG_OBJ.sub(link, event)
        rows.append(
            {
                "time": fmt_time(_parse_time(entry.get("timestamp"))),
                "level": str(entry.get("level", "")),
                "event": mark_safe(event),
            }
        )
    return rows


def who_opened(raw_log, role_by_nick):
    try:
        entries = json.loads(raw_log) if raw_log else []
    except (TypeError, ValueError):
        return None
    for entry in entries if isinstance(entries, list) else []:
        event = str(entry.get("event", "")) if isinstance(entry, dict) else ""
        moment = (
            _parse_time(entry.get("timestamp")) if isinstance(entry, dict) else None
        )
        if "dispute was opened automatically" in event:
            return {
                "who": _("opened automatically (the chat deadline expired)"),
                "time": fmt_time(moment),
            }
        if event.startswith("Dispute was opened"):
            m = _RE_LOG_OBJ.search(event)
            if m:
                nick = m.group(3).strip()
                role = role_by_nick.get(nick)
                who = f"{role_name(role)} ({nick})" if role else nick
                return {"who": who, "time": fmt_time(moment)}
            return {"who": _("unknown"), "time": fmt_time(moment)}
    return None


def _payment(p):
    if p is None:
        return {"exists": False}
    return {
        "exists": True,
        "sats": fmt_sats(p.num_satoshis),
        "status": PAYMENT_STATUS.get(p.status, p.get_status_display()),
        "url": _url("admin:api_lnpayment_change", p.pk),
        "expires": fmt_time(getattr(p, "expires_at", None)),
    }


def roles(order):
    from api.models import Order

    maker_buys = order.type == Order.Types.BUY
    return {
        "maker_buys": maker_buys,
        "buyer": order.maker if maker_buys else order.taker,
        "seller": order.taker if maker_buys else order.maker,
        "buyer_side": "maker" if maker_buys else "taker",
        "seller_side": "taker" if maker_buys else "maker",
        "buyer_bond": order.maker_bond if maker_buys else order.taker_bond,
        "seller_bond": order.taker_bond if maker_buys else order.maker_bond,
    }


def payout_sats(order):
    if order.is_swap:
        return order.payout_tx.num_satoshis if order.payout_tx else None
    return order.payout.num_satoshis if order.payout else None


def action_for(order, decision):
    maker_buys = roles(order)["maker_buys"]
    if decision == "buyer":
        return "maker_wins" if maker_buys else "taker_wins"
    if decision == "seller":
        return "taker_wins" if maker_buys else "maker_wins"
    if decision == "return":
        return "return_everything"
    if decision == "completed":
        return "successful_trade"
    return None


def is_open_dispute(order):
    from api.models import Order

    return order.is_disputed and order.status in (Order.Status.DIS, Order.Status.WFR)


def plan(order, decision):
    from api.models import LNPayment

    r = roles(order)
    buyer_nick = r["buyer"].username if r["buyer"] else "?"
    seller_nick = r["seller"].username if r["seller"] else "?"
    buyer_bond = r["buyer_bond"].num_satoshis if r["buyer_bond"] else 0
    seller_bond = r["seller_bond"].num_satoshis if r["seller_bond"] else 0
    escrow = order.trade_escrow
    escrow_sats = escrow.num_satoshis if escrow else 0
    payout = payout_sats(order)

    missing = []
    if not is_open_dispute(order):
        missing.append(_("the order is no longer in dispute"))
    if not escrow or escrow.status != LNPayment.Status.SETLED:
        missing.append(_("the seller's escrow has not been settled by the coordinator"))
    if not r["buyer_bond"] or not r["seller_bond"]:
        missing.append(_("a bond is missing"))

    values = {
        "buyer": buyer_nick,
        "seller": seller_nick,
        "buyer_bond": fmt_sats(buyer_bond),
        "seller_bond": fmt_sats(seller_bond),
        "escrow": fmt_sats(escrow_sats),
    }
    lines, warning = [], ""
    if decision == "buyer":
        if payout is None:
            missing.append(_("the buyer never submitted a payout invoice or address"))
        else:
            values["trade"] = fmt_sats(payout)
            values["total"] = fmt_sats(buyer_bond + payout)
            lines.append(
                _(
                    "The buyer %(buyer)s is credited %(total)s sats as a reward "
                    "(their bond of %(buyer_bond)s + %(trade)s from the trade). "
                    "They withdraw it from the app."
                )
                % values
            )
            lines.append(
                _(
                    "The seller %(seller)s gets nothing back: they lose their bond "
                    "(%(seller_bond)s) and their escrow (%(escrow)s)."
                )
                % values
            )
    elif decision == "seller":
        values["total"] = fmt_sats(seller_bond + escrow_sats)
        lines.append(
            _(
                "The seller %(seller)s is credited %(total)s sats as a reward "
                "(their bond of %(seller_bond)s + their escrow of %(escrow)s). "
                "They withdraw it from the app."
            )
            % values
        )
        lines.append(
            _("The buyer %(buyer)s gets nothing and loses their bond (%(buyer_bond)s).")
            % values
        )
    elif decision == "return":
        lines.append(
            _(
                "Each party gets back what they put in, credited as a reward to withdraw "
                "from the app: the buyer %(buyer)s their bond (%(buyer_bond)s); the "
                "seller %(seller)s their bond (%(seller_bond)s) and their escrow "
                "(%(escrow)s)."
            )
            % values
        )
        lines.append(_("The order ends as collaboratively cancelled."))
    elif decision == "completed":
        if payout is None:
            missing.append(_("the buyer never submitted a payout invoice or address"))
        else:
            values["trade"] = fmt_sats(payout)
            if order.is_swap:
                lines.append(
                    _(
                        "The buyer %(buyer)s is paid %(trade)s sats on-chain, to the "
                        "address they gave."
                    )
                    % values
                )
            else:
                lines.append(
                    _(
                        "The buyer %(buyer)s is paid %(trade)s sats right now over "
                        "Lightning, to the invoice they gave."
                    )
                    % values
                )
            lines.append(
                _(
                    "Both bonds are credited back as rewards: %(buyer_bond)s to the buyer "
                    "and %(seller_bond)s to the seller %(seller)s."
                )
                % values
            )
            if (
                not order.is_swap
                and order.payout
                and order.payout.expires_at
                and order.payout.expires_at < timezone.now()
            ):
                warning = _(
                    "The buyer's invoice has already expired: the payment will probably "
                    "fail and you will have to ask them for a new one."
                )
    return {
        "lines": lines,
        "missing": missing,
        "warning": warning,
        "possible": not missing,
    }


class _MessageCollector:
    def __init__(self):
        self.items = []

    def add(self, level, message, extra_tags=""):
        self.items.append((level, str(message)))


def resolve(order_admin, order, decision, user):
    from api.models import Order

    name = action_for(order, decision)
    if name is None:
        return False, [_("Unknown decision")]
    collector = _MessageCollector()
    fake = SimpleNamespace(
        _messages=collector, META={}, user=user, POST={}, method="POST"
    )
    getattr(order_admin, name)(fake, Order.objects.filter(pk=order.pk))
    ok = any(level == messages.SUCCESS for level, _text in collector.items)
    return ok, [text for _level, text in collector.items]


def write_to(order, recipient, text):
    r = roles(order)
    users = []
    if recipient in ("buyer", "both") and r["buyer"]:
        users.append(r["buyer"])
    if recipient in ("seller", "both") and r["seller"]:
        users.append(r["seller"])
    for u in users:
        send_coordinator_message(order, u.robot, text)
    return [u.username for u in users]


def sent_messages(order, label_by_robot):
    from api.models import Notification

    prefix = message_title_prefix(order.pk)
    head_re = re.compile(r"^" + re.escape(prefix) + r":\s*")
    rows = []
    for n in Notification.objects.filter(
        order=order, title__startswith=prefix
    ).order_by("created_at"):
        text = head_re.sub("", (n.title or "") + (n.description or ""))
        to = label_by_robot.get(n.robot_id, f"robot {n.robot_id}")
        last = rows[-1] if rows else None
        if (
            last
            and last["text"] == text
            and abs((n.created_at - last["moment"]).total_seconds()) < 120
        ):
            if to not in last["to"]:
                last["to"].append(to)
            continue
        rows.append(
            {
                "text": text,
                "moment": n.created_at,
                "time": fmt_time(n.created_at),
                "to": [to],
            }
        )
    for row in rows:
        row["to"] = _(" and ").join(row["to"])
    return rows


def _languages(request):
    current = getattr(request, "LANGUAGE_CODE", "en")
    return [{"code": c, "name": n, "current": c == current} for c, n in PANEL_LANGUAGES]


def _context(order_admin, request, order, confirm=None):
    from api.models import Order

    r = roles(order)
    buyer, seller = r["buyer"], r["seller"]
    role_by_nick = {}
    label_by_robot = {}
    for role, user in (("buyer", buyer), ("seller", seller)):
        if user:
            role_by_nick[user.username] = role
            label_by_robot[user.robot.id] = f"{role_name(role)} ({user.username})"

    maker_st = parse_statement(order.maker_statement)
    taker_st = parse_statement(order.taker_statement)
    buyer_st = maker_st if r["maker_buys"] else taker_st
    seller_st = taker_st if r["maker_buys"] else maker_st

    def party(user, role, side, statement, bond):
        robot = user.robot if user else None
        return {
            "role": role,
            "role_label": role_name(role),
            "side_label": side_name(side),
            "nick": user.username if user else _("(nobody)"),
            "robot_url": _url("admin:api_robot_change", robot.pk) if robot else "",
            "contracts": robot.total_contracts if robot else 0,
            "disputes": robot.num_disputes if robot else 0,
            "lost": robot.lost_disputes if robot else 0,
            "telegram": bool(robot and robot.telegram_enabled),
            "nostr": bool(robot and robot.nostr_pubkey),
            "statement": statement,
            "contacts": find_contacts(statement["text"]) if statement else [],
            "attached_chat": bool(statement and statement["messages"]),
            "bond": _payment(bond),
        }

    p_buyer = party(buyer, "buyer", r["buyer_side"], buyer_st, r["buyer_bond"])
    p_seller = party(seller, "seller", r["seller_side"], seller_st, r["seller_bond"])

    deadline = None
    if order.expires_at:
        sec = (order.expires_at - timezone.now()).total_seconds()
        deadline = {
            "time": fmt_time(order.expires_at),
            "expired": sec <= 0,
            "text": (
                _("%(t)s left") % {"t": fmt_duration(sec)}
                if sec > 0
                else _("expired %(t)s ago") % {"t": fmt_duration(sec)}
            ),
        }

    if order.has_range and order.amount is None:
        amount = f"{order.min_amount:g}–{order.max_amount:g}"
    elif order.amount is not None:
        amount = f"{order.amount:f}".rstrip("0").rstrip(".")
    else:
        amount = "?"

    status = str(ORDER_STATUS.get(order.status, order.get_status_display()))
    winner = None
    if order.status == Order.Status.MLD:
        winner = "buyer" if r["buyer_side"] == "taker" else "seller"
    elif order.status == Order.Status.TLD:
        winner = "buyer" if r["buyer_side"] == "maker" else "seller"
    if winner:
        status += " " + "(" + _("winner: %(role)s") % {"role": role_name(winner)} + ")"

    plans = {
        key: {"key": key, "label": label, **plan(order, key)}
        for key, label in DECISIONS.items()
    }
    page = reverse("admin:api_order_dispute", args=[order.pk])
    return {
        **order_admin.admin_site.each_context(request),
        "title": _("Dispute of order %(id)s") % {"id": order.pk},
        "opts": order_admin.model._meta,
        "order": order,
        "page_url": page,
        "change_url": _url("admin:api_order_change", order.pk),
        "list_url": reverse("admin:api_order_disputes"),
        "type_label": _("BUY") if order.type == Order.Types.BUY else _("SELL"),
        "type_explained": (
            _("the maker buys sats")
            if order.type == Order.Types.BUY
            else _("the maker sells sats")
        ),
        "amount": amount,
        "currency": str(order.currency) if order.currency else "",
        "method": order.payment_method,
        "sats": fmt_sats(order.last_satoshis or order.satoshis or order.t0_satoshis),
        "premium": f"{float(order.premium):g}" if order.premium is not None else "0",
        "status": status,
        "in_dispute": is_open_dispute(order),
        "opened": who_opened(order.logs, role_by_nick),
        "deadline": deadline,
        "tz": timezone.get_current_timezone_name(),
        "buyer": p_buyer,
        "seller": p_seller,
        "chat": merge_chat((("buyer", buyer_st), ("seller", seller_st)), role_by_nick),
        "nobody_attached": not (p_buyer["attached_chat"] or p_seller["attached_chat"]),
        "sent": sent_messages(order, label_by_robot),
        "max_message": MESSAGE_MAX,
        "message_head": message_head(order.pk),
        "plans": [plans[k] for k in DECISIONS],
        "confirm": plans.get(confirm),
        "log": render_log(order.logs, role_by_nick),
        "escrow": _payment(order.trade_escrow),
        "payout": _payment(order.payout) if not order.is_swap else None,
        "payout_tx": order.payout_tx if order.is_swap else None,
        "fiat_sent": order.is_fiat_sent,
        "can_change": order_admin.has_change_permission(request, order),
        "languages": _languages(request),
    }


def _require_staff(request):
    if not (request.user.is_active and request.user.is_staff):
        raise PermissionDenied


def dispute_view(order_admin, request, order_id):
    from api.models import Order

    _require_staff(request)
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
    page = reverse("admin:api_order_dispute", args=[order.pk])

    if request.method == "POST":
        if not order_admin.has_change_permission(request, order):
            raise PermissionDenied
        action = request.POST.get("action")
        if action == "message":
            text = request.POST.get("text", "").strip()
            recipient = request.POST.get("recipient", "")
            if recipient not in RECIPIENTS:
                messages.error(request, _("Choose who the message is for."))
            elif not text:
                messages.error(request, _("The message is empty: nothing was sent."))
            elif len(text) > MESSAGE_MAX:
                messages.error(
                    request,
                    _(
                        "The message has %(n)s characters; the limit is %(max)s. Nothing was sent."
                    )
                    % {"n": len(text), "max": MESSAGE_MAX},
                )
            else:
                nicks = write_to(order, recipient, text)
                if nicks:
                    messages.success(
                        request,
                        _(
                            "Message sent to %(names)s (app, plus Nostr and Telegram if they use them)."
                        )
                        % {"names": _(" and ").join(nicks)},
                    )
                else:
                    messages.error(request, _("There is nobody to send it to."))
            return HttpResponseRedirect(page + "#write")
        if action == "resolve":
            decision = request.POST.get("decision", "")
            if decision not in DECISIONS or request.POST.get("confirmed") != "yes":
                messages.error(
                    request, _("The decision was not confirmed. Nothing was done.")
                )
                return HttpResponseRedirect(page + "#resolve")
            if request.POST.get("status") != str(order.status):
                messages.error(
                    request,
                    _(
                        "The order changed status while you were confirming. Nothing was done: review it again."
                    ),
                )
                return HttpResponseRedirect(page + "#resolve")
            current = plan(order, decision)
            if not current["possible"]:
                messages.error(
                    request,
                    _(
                        "It cannot be resolved this way right now: %(why)s. Nothing was done."
                    )
                    % {"why": "; ".join(current["missing"])},
                )
                return HttpResponseRedirect(page + "#resolve")
            ok, notes = resolve(order_admin, order, decision, request.user)
            detail = " | ".join(notes) or _("no response")
            if ok:
                messages.success(
                    request,
                    _("Done: “%(decision)s” on order %(id)s. (%(detail)s)")
                    % {
                        "decision": DECISIONS[decision],
                        "id": order.pk,
                        "detail": detail,
                    },
                )
            else:
                messages.error(
                    request,
                    _("Order %(id)s could not be resolved. Response: %(detail)s")
                    % {"id": order.pk, "detail": detail},
                )
            return HttpResponseRedirect(page)
        messages.error(request, _("Unknown action."))
        return HttpResponseRedirect(page)

    confirm = request.GET.get("confirm")
    if confirm not in DECISIONS:
        confirm = None
    return TemplateResponse(
        request,
        "roblosats_admin/dispute.html",
        _context(order_admin, request, order, confirm),
    )


def disputes_view(order_admin, request):
    from api.models import Order

    _require_staff(request)
    now = timezone.now()

    def row(o):
        r = roles(o)
        sec = (o.expires_at - now).total_seconds() if o.expires_at else None
        return {
            "id": o.pk,
            "url": reverse("admin:api_order_dispute", args=[o.pk]),
            "type_label": _("BUY") if o.type == Order.Types.BUY else _("SELL"),
            "amount": f"{o.amount:f}".rstrip("0").rstrip(".")
            if o.amount is not None
            else "?",
            "currency": str(o.currency) if o.currency else "",
            "method": o.payment_method,
            "status": ORDER_STATUS.get(o.status, o.get_status_display()),
            "buyer": r["buyer"].username if r["buyer"] else "?",
            "seller": r["seller"].username if r["seller"] else "?",
            "buyer_statement": bool(
                o.maker_statement if r["maker_buys"] else o.taker_statement
            ),
            "seller_statement": bool(
                o.taker_statement if r["maker_buys"] else o.maker_statement
            ),
            "deadline": ""
            if sec is None
            else (
                _("%(t)s left") % {"t": fmt_duration(sec)}
                if sec > 0
                else _("deadline expired")
            ),
            "created": fmt_time(o.created_at),
        }

    open_statuses = [Order.Status.DIS, Order.Status.WFR]
    qs = Order.objects.select_related("maker", "taker", "currency")
    open_rows = [
        row(o)
        for o in qs.filter(is_disputed=True, status__in=open_statuses).order_by(
            "expires_at"
        )
    ]
    closed_rows = [
        row(o)
        for o in qs.filter(is_disputed=True)
        .exclude(status__in=open_statuses)
        .order_by("-created_at")[:30]
    ]
    return TemplateResponse(
        request,
        "roblosats_admin/disputes.html",
        {
            **order_admin.admin_site.each_context(request),
            "title": _("Disputes"),
            "opts": order_admin.model._meta,
            "open_disputes": open_rows,
            "closed_disputes": closed_rows,
            "tz": timezone.get_current_timezone_name(),
            "languages": _languages(request),
        },
    )
