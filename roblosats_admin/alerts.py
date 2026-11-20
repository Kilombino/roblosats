import logging
import re
from datetime import timedelta

from decouple import config
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import NoReverseMatch, reverse
from django.utils import translation

from roblosats_admin.i18n import _, _lazy
from roblosats_admin.middleware import PANEL_LANGUAGES, panel_language

# 0.9.7: the alerts also speak Catalan (only the alert texts are in locale/ca; the panel
# itself is not translated to Catalan).
ALERT_LANGUAGES = PANEL_LANGUAGES + [("ca", "Català")]


def alert_language(code):
    code = (code or "").lower()
    if code.split("-")[0].split("_")[0] == "ca":
        return "ca"
    return panel_language(code)
from roblosats_admin.models import EVENT_FIELDS

logger = logging.getLogger("roblosats_admin")

EVENTS = {
    "published": ("on_published", _lazy("Order published in the order book"), "📢"),
    "taken": ("on_taken", _lazy("Order taken (the taker's bond is locked)"), "🤝"),
    "escrow_locked": (
        "on_escrow_locked",
        _lazy("Escrow and buyer's invoice in: the fiat exchange starts"),
        "🔒",
    ),
    "finished": ("on_finished", _lazy("Order finished successfully"), "✅"),
    "cancelled": ("on_cancelled", _lazy("Order cancelled or expired"), "❌"),
    "dispute": ("on_dispute", _lazy("Dispute opened"), "⚖️"),
    "dispute_chat": (
        "on_dispute_chat",
        _lazy("Chat message from a party in a disputed order"),
        "💬",
    ),
    "coordinator_message": (
        "on_coordinator_message",
        _lazy("Message sent from the coordinator"),
        "✉️",
    ),
    # 0.9.7: a free on-chain bond whose return signed in advance could not be sent
    "bond_manual": (
        "on_bond_manual",
        _lazy("On-chain bond not returned by itself: its owner takes it back by hand"),
        "🔓",
    ),
    # 0.9.7 (watch.py): one toggle per group
    "onchain": (
        "on_onchain_events",
        _lazy(
            "On-chain orders: bonds and deposits in the mempool and confirmed, deposit signed, release, automatic bond returns, swap payouts and rescues"
        ),
        "⛓️",
    ),
    "wallet": (
        "on_wallet_events",
        _lazy(
            "Your node's on-chain wallet: payments in and out, channels opened and closed (force closes too) and high mining fees"
        ),
        "👛",
    ),
}

TEST_EVENT = "test"
TEST_TIMEOUT = 20
TEXT_PREVIEW = 300

_RE_TOKEN = re.compile(r"^\d{3,15}:[A-Za-z0-9_-]{30,64}$")
_RE_CHAT = re.compile(r"^(-?\d{1,20}|@[A-Za-z][A-Za-z0-9_]{3,31})$")
_RE_URL = re.compile(r"^https?://[^\s/?#]+(:\d+)?/?$")


def env_token():
    return config("TELEGRAM_TOKEN", cast=str, default="").strip()


def env_chat():
    return config("TELEGRAM_COORDINATOR_CHAT_ID", cast=str, default="").strip()


def current():
    from roblosats_admin.models import TelegramAlerts

    row = TelegramAlerts.objects.filter(pk=1).first()
    if row is None:
        row = TelegramAlerts(pk=1, on_dispute=bool(env_token() and env_chat()))
    return row


def token_of(cfg):
    return cfg.bot_token or env_token()


def chat_of(cfg):
    return cfg.chat_id or env_chat()


def wants(cfg, event):
    if event == TEST_EVENT:
        return bool(token_of(cfg) and chat_of(cfg))
    field = EVENTS.get(event, (None,))[0]
    return bool(field and getattr(cfg, field) and token_of(cfg) and chat_of(cfg))


def mask(token):
    if not token:
        return ""
    return "…" + token[-4:] if len(token) > 12 else "…"


def event_for(old_status, new_status):
    from api.models import Order

    S = Order.Status
    if new_status == S.PUB and old_status != S.PAU:
        return "published"
    if new_status == S.WF2:
        return "taken"
    if new_status == S.CHA and old_status in (S.WF2, S.WFE, S.WFI):
        return "escrow_locked"
    if new_status == S.SUC:
        return "finished"
    if new_status in (S.UCA, S.EXP, S.CCA) and old_status != S.WFB:
        return "cancelled"
    if new_status == S.DIS:
        return "dispute"
    return None


def queue(event, order_id, extra=None):
    try:
        if not wants(current(), event):
            return False
        from roblosats_admin.tasks import telegram_alert

        def send():
            try:
                telegram_alert.delay(event=event, order_id=order_id, extra=extra or {})
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Telegram alert {event} not queued: {e}")

        transaction.on_commit(send)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Telegram alert {event} skipped: {e}")
        return False


def on_status_changed(sender, order, old_status, new_status, **kwargs):
    event = event_for(old_status, new_status)
    if event:
        queue(event, order.id, {"old": int(old_status), "new": int(new_status)})


def on_chat_message(sender, instance, created=False, raw=False, **kwargs):
    if not created or raw:
        return
    try:
        from api.models import Order

        order = instance.order
        if order is None or order.status not in (Order.Status.DIS, Order.Status.WFR):
            return
        if not wants(current(), "dispute_chat"):
            return
        gap = config("CHAT_NOTIFICATION_TIMEGAP", cast=int, default=5)
        recent = (
            type(instance)
            .objects.filter(
                order=order,
                created_at__gt=instance.created_at - timedelta(minutes=gap),
                created_at__lte=instance.created_at,
            )
            .exclude(pk=instance.pk)
            .exists()
        )
        if recent:
            return
        queue(
            "dispute_chat",
            order.id,
            {"sender": instance.sender.username if instance.sender else ""},
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Telegram chat alert skipped: {e}")


def coordinator_message_sent(order, names, text):
    queue(
        "coordinator_message",
        order.id,
        {"names": list(names), "text": str(text)[:TEXT_PREVIEW]},
    )


def base_url(cfg):
    if cfg.panel_url:
        return cfg.panel_url.rstrip("/")
    host = config("HOST_NAME", cast=str, default="").strip()
    return f"http://{host}" if host else ""


def order_path(order):
    name = "admin:api_order_dispute" if order.is_disputed else "admin:api_order_summary"
    try:
        return reverse(name, args=[order.pk])
    except NoReverseMatch:
        return ""


def headline(event, order, extra):
    from roblosats_admin import disputes

    values = {"id": order.pk if order else ""}
    if event == "published":
        if extra.get("old", 0) == 0:
            return _("Order %(id)s is public in the order book") % values
        return _("Order %(id)s is public in the order book again") % values
    if event == "taken":
        return _("Order %(id)s was taken: the taker's bond is locked") % values
    if event == "escrow_locked":
        return (
            _(
                "Order %(id)s: escrow and buyer's invoice are in, the fiat exchange starts"
            )
            % values
        )
    if event == "finished":
        return _("Order %(id)s finished successfully") % values
    if event == "cancelled":
        values["status"] = disputes.ORDER_STATUS.get(order.status, "")
        return _("Order %(id)s: %(status)s") % values
    if event == "dispute":
        return _("Dispute opened on order %(id)s") % values
    if event == "dispute_chat":
        r = disputes.roles(order)
        sender = extra.get("sender", "")
        role = ""
        for key in ("buyer", "seller"):
            if r[key] and r[key].username == sender:
                role = disputes.role_name(key)
        values["who"] = f"{role} ({sender})" if role else sender or "?"
        return _("New chat message from %(who)s on disputed order %(id)s") % values
    if event == "coordinator_message":
        values["names"] = _(" and ").join(extra.get("names") or []) or "?"
        return _("You wrote to %(names)s on order %(id)s") % values
    if event == "bond_manual":
        values["role"] = disputes.side_name(extra.get("role", "")) or extra.get("role", "")
        return (
            _(
                "Order %(id)s: the %(role)s's on-chain bond is free but did not go back by itself; their app shows the button to take it back"
            )
            % values
        )
    if event == "onchain":
        return onchain_headline(order, extra)
    if event == "wallet":
        return wallet_headline(extra)
    return _("Test message: the coordinator's alerts reach this chat.")


def _who(role):
    from roblosats_admin import disputes

    if role in ("buyer", "seller"):
        return disputes.role_name(role)
    return disputes.side_name(role)


def onchain_headline(order, extra):
    from roblosats_admin.disputes import fmt_sats

    v = {
        "id": order.pk if order else extra.get("order", "?"),
        "role": _who(extra.get("role", "")),
        "sats": fmt_sats(extra.get("sats") or 0),
        "h": extra.get("height", ""),
        "how": extra.get("how", ""),
    }
    k = extra.get("kind")
    texts = {
        "bond_mempool": _("Order %(id)s: the %(role)s's on-chain bond is in the mempool (%(sats)s sats)"),
        "bond_confirmed": _("Order %(id)s: the %(role)s's on-chain bond is confirmed (%(sats)s sats)"),
        "deposit_mempool": _("Order %(id)s: the %(role)s's deposit payment is in the mempool (%(sats)s sats)"),
        "deposit_signed": _("Order %(id)s: the escrow deposit is fully signed and broadcast (%(sats)s sats)"),
        "deposit_confirmed": _("Order %(id)s: the escrow deposit is confirmed in block %(h)s"),
        "release_sent": _("Order %(id)s: the escrow release was broadcast"),
        "release_confirmed": _("Order %(id)s: the escrow release is confirmed"),
        "return_sent": _("Order %(id)s: the %(role)s's bond went back by itself (%(sats)s sats)"),
        "return_confirmed": _("Order %(id)s: the %(role)s's automatic bond return is confirmed"),
        "payout_sent": _("Order %(id)s: the buyer's on-chain payout was sent (%(sats)s sats)"),
        "payout_confirmed": _("Order %(id)s: the buyer's on-chain payout is confirmed (%(sats)s sats)"),
        "rescue_sent": _("Order %(id)s: the escrow was rescued without the coordinator"),
    }
    text = texts.get(k, _("Order %(id)s: on-chain event")) % v
    if k == "release_sent":
        how = _("seller's leaf, one signature") if extra.get("leaf") else v["how"]
        if how:
            text += f" ({how})"
    return text


CLOSE_TYPES = {
    "cooperative": _lazy("cooperative close"),
    "local_force": _lazy("force close by your node"),
    "remote_force": _lazy("force close by the peer"),
    "breach": _lazy("breach: the peer published an old state"),
    "funding_canceled": _lazy("funding cancelled"),
    "abandoned": _lazy("abandoned"),
}


def wallet_headline(extra):
    from roblosats_admin.disputes import fmt_sats

    k = extra.get("kind")
    peer = extra.get("peer") or ""
    v = {
        "sats": fmt_sats(abs(int(extra.get("amount") or 0))),
        "fee": fmt_sats(int(extra.get("fee") or 0)),
        "c": extra.get("confs", 0),
        "peer": f"{peer[:10]}…{peer[-6:]}" if len(peer) > 20 else peer or "?",
        "cap": fmt_sats(int(extra.get("capacity") or 0)),
        "settled": fmt_sats(int(extra.get("settled") or 0)),
        "type": str(CLOSE_TYPES.get(extra.get("type"), extra.get("type") or "")),
    }
    texts = {
        "tx_in_mempool": _("Your wallet: %(sats)s sats coming in, in the mempool"),
        "tx_in_confirmed": _("Your wallet: %(sats)s sats received (%(c)s confirmations)"),
        "tx_out_mempool": _("Your wallet: %(sats)s sats sent, in the mempool (mining fee %(fee)s sats)"),
        "tx_out_confirmed": _("Your wallet: %(sats)s sats sent, confirmed (mining fee %(fee)s sats)"),
        "fee_notable": _("Your wallet paid a high mining fee: %(fee)s sats"),
        "chan_opening": _("Channel opening with %(peer)s: %(cap)s sats"),
        "chan_open": _("Channel open with %(peer)s: %(cap)s sats"),
        "chan_closing": _("Channel force-closing with %(peer)s (%(cap)s sats)")
        if extra.get("force")
        else _("Channel closing with %(peer)s (%(cap)s sats)"),
        "chan_closed": _("Channel closed with %(peer)s: %(type)s, %(settled)s sats back to your wallet"),
    }
    return texts.get(k, _("Your wallet: on-chain event")) % v


def build_text(cfg, event, order, extra):
    from api.models import Order
    from roblosats_admin import disputes
    from roblosats_admin.messaging import coordinator_alias

    icon = EVENTS[event][2] if event in EVENTS else "🔔"
    lines = [f"{icon} {coordinator_alias()}: {headline(event, order, extra)}"]
    if order is not None:
        kind = _("BUY") if order.type == Order.Types.BUY else _("SELL")
        sats = order.last_satoshis or order.satoshis or order.t0_satoshis
        if getattr(order, "sats_denominated", False) is True:
            # 0.9.9: "SELL 1,000,000 sats for 2,100 Spamsats" (it said "1000000 Spamcoin")
            detail = f"{kind} {disputes.trade_text(order)}"
        else:
            detail = (
                f"{kind} {disputes.amount_text(order)} "
                f"{disputes.currency_text(order)} · {disputes.fmt_sats(sats)} sats"
            )
        if order.payment_method:
            detail += f" · {order.payment_method}"
        lines.append(detail)
        if event == "coordinator_message" and extra.get("text"):
            lines.append(f"«{extra['text']}»")
        if event == "bond_manual" and extra.get("reason"):
            lines.append(f"({extra['reason']})")
    if event in ("onchain", "wallet"):
        from roblosats_admin.onchain import EXPLORER

        if extra.get("label"):
            lines.append(f"«{extra['label']}»")
        txid = extra.get("txid") or ""
        if len(txid) == 64:
            lines.append(EXPLORER + txid)
    if order is not None:
        path = order_path(order)
        if path:
            lines.append(base_url(cfg) + path)
    return "\n".join(lines)


def deliver(event, order_id, extra):
    from api.models import Order
    from api.utils import get_session

    cfg = current()
    if not wants(cfg, event):
        return False
    order = None
    if order_id is not None:
        order = (
            Order.objects.select_related("maker", "taker", "currency")
            .filter(pk=order_id)
            .first()
        )
        if order is None:
            return False
    lang = alert_language(cfg.language) or "en"
    with translation.override(lang):
        text = build_text(cfg, event, order, extra)
    from roblosats_admin.tasks import send_telegram

    return send_telegram(get_session(), chat_of(cfg), text, token=token_of(cfg))


def send_test(cfg):
    from api.utils import get_session
    from roblosats_admin.tasks import telegram_send_once

    lang = alert_language(cfg.language) or "en"
    with translation.override(lang):
        text = build_text(cfg, TEST_EVENT, None, {})
        if base_url(cfg):
            text += "\n" + base_url(cfg) + reverse("admin:index")
    return telegram_send_once(
        get_session(), token_of(cfg), chat_of(cfg), text, timeout=TEST_TIMEOUT
    )


def can_configure(request):
    user = getattr(request, "user", None)
    return bool(
        user
        and user.is_active
        and user.is_staff
        and user.has_perm("roblosats_admin.change_telegramalerts")
    )


def _save(request, cfg):
    post = request.POST
    errors = []
    token = post.get("bot_token", "").strip()
    if post.get("clear_token") == "1":
        cfg.bot_token = ""
    elif token:
        if _RE_TOKEN.match(token):
            cfg.bot_token = token
        else:
            errors.append(_("The bot token does not look like a Telegram token."))
    chat = post.get("chat_id", "").strip()
    if chat and not _RE_CHAT.match(chat):
        errors.append(
            _("The chat id must be a number (groups start with -) or a @channel name.")
        )
    else:
        cfg.chat_id = chat
    url = post.get("panel_url", "").strip()
    if url and not _RE_URL.match(url):
        errors.append(
            _(
                "The panel address must look like https://host or http://host:port, with nothing after it."
            )
        )
    else:
        cfg.panel_url = url.rstrip("/")
    cfg.language = alert_language(post.get("language")) or "en"
    for field in EVENT_FIELDS:
        setattr(cfg, field, post.get(field) == "1")
    if errors:
        for e in errors:
            messages.error(request, e)
        return False
    cfg.save()
    messages.success(request, _("Telegram alerts saved."))
    return True


def settings_view(model_admin, request):
    from roblosats_admin.disputes import _languages

    if not can_configure(request):
        raise PermissionDenied
    page = request.path
    cfg = current()
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "save":
            _save(request, cfg)
        elif action == "test":
            if not wants(cfg, TEST_EVENT):
                messages.error(
                    request,
                    _(
                        "Save a bot token and a chat id first: there is nowhere to send it."
                    ),
                )
            else:
                ok, detail = send_test(cfg)
                if ok:
                    messages.success(
                        request, _("Test message sent. Check the Telegram chat.")
                    )
                else:
                    messages.error(
                        request,
                        _("Telegram did not accept the message: %(why)s")
                        % {"why": detail},
                    )
        else:
            messages.error(request, _("Unknown action."))
        return HttpResponseRedirect(page)

    events = [
        {"field": field, "label": label, "icon": icon, "on": getattr(cfg, field)}
        for field, label, icon in EVENTS.values()
    ]
    return TemplateResponse(
        request,
        "roblosats_admin/alerts.html",
        {
            **model_admin.admin_site.each_context(request),
            "title": _("Telegram alerts"),
            "opts": model_admin.model._meta,
            "cfg": cfg,
            "page_url": page,
            "token_mask": mask(cfg.bot_token),
            "env_token_mask": mask(env_token()) if env_token() else "",
            "env_chat": env_chat(),
            "ready": wants(cfg, TEST_EVENT),
            "default_url": base_url(type(cfg)()),
            "events": events,
            "alert_languages": [
                {"code": code, "name": name, "current": code == cfg.language}
                for code, name in ALERT_LANGUAGES
            ],
            "languages": _languages(request),
        },
    )
