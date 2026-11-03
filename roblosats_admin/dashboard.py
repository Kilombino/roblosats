from decouple import config
from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from roblosats_admin.disputes import ORDER_STATUS, amount_text, fmt_duration
from roblosats_admin.i18n import _

ADMIN_ROOT = "/coordinator/"
IN_PROGRESS = [6, 7, 8, 9, 10]
IN_DISPUTE = [11, 16]
URGENT_SECONDS = 1800


def can_configure_alerts(request):
    from roblosats_admin.alerts import can_configure

    return can_configure(request)


def _time(moment):
    return timezone.localtime(moment).strftime("%d/%m %H:%M") if moment else ""


def _left(deadline, now):
    if deadline is None:
        return "", False
    seconds = int((deadline - now).total_seconds())
    if seconds <= 0:
        return _("deadline expired"), True
    return _("%(t)s left") % {"t": fmt_duration(seconds)}, seconds < URGENT_SECONDS


def _list(**filters):
    query = "&".join(f"{k}={v}" for k, v in filters.items())
    return f"{ADMIN_ROOT}api/order/?{query}"


def _order_url(order):
    name = "admin:api_order_dispute" if order.is_disputed else "admin:api_order_summary"
    return reverse(name, args=[order.pk])


def _channel_balance():
    try:
        from api.lightning.lnd import LNDNode

        balance = LNDNode.channel_balance()
        return (
            f"{balance['local_balance']:,}".replace(",", "."),
            f"{balance['remote_balance']:,}".replace(",", "."),
        )
    except Exception:  # noqa: BLE001
        return "?", "?"


def dashboard(request, context=None):
    from api.models import Order

    now = timezone.now()
    live = []
    for o in (
        Order.objects.filter(status__in=IN_PROGRESS)
        .select_related("currency")
        .order_by("-last_satoshis_time", "-created_at")[:10]
    ):
        left, urgent = _left(o.expires_at, now)
        live.append(
            {
                "id": o.id,
                "type": _("BUY") if o.type == Order.Types.BUY else _("SELL"),
                "amount": f"{amount_text(o)} {o.currency or ''}",
                "status": ORDER_STATUS.get(o.status, o.get_status_display()),
                "since": _time(o.created_at),
                "until": _time(o.expires_at),
                "left": left,
                "urgent": urgent,
                "link": _order_url(o),
            }
        )

    local, remote = _channel_balance()
    in_progress = Order.objects.filter(status__in=IN_PROGRESS).count()
    disputes = Order.objects.filter(status__in=IN_DISPUTE).count()
    public = Order.objects.filter(status=1).count()
    completed = Order.objects.filter(status=14).count()
    cancelled = Order.objects.filter(status__in=(4, 12)).count()
    expired = Order.objects.filter(status=5).count()
    disputes_url = f"{ADMIN_ROOT}api/order/disputes/?view=open"
    # With only one dispute waiting, the banner and the card open it directly.
    waiting = list(Order.objects.filter(status__in=IN_DISPUTE, is_disputed=True)[:2])
    if len(waiting) == 1:
        disputes_url = _order_url(waiting[0])
    version = getattr(settings, "VERSION", None)

    data = {
        "open_disputes": disputes,
        "links": {"disputes": disputes_url},
        "cards": [
            {
                "title": _("In progress"),
                "value": in_progress,
                "link": _list(status__in="6%2C7%2C8%2C9%2C10"),
                "foot": _("bonds, chat or fiat sent"),
            },
            {
                "title": _("In the book"),
                "value": public,
                "link": _list(status__exact=1),
                "foot": _("waiting for a taker"),
            },
            {
                "title": _("Disputes"),
                "value": disputes,
                "link": disputes_url,
                "foot": _("open and to be resolved"),
            },
            {
                "title": _("Completed"),
                "value": completed,
                "link": _list(status__exact=14),
                "foot": _("all time"),
            },
            {
                "title": _("Cancelled orders"),
                "value": cancelled,
                "link": _list(status__in="4%2C12"),
                "foot": _("by the maker or by both · expired: %(n)s") % {"n": expired},
            },
        ],
        "live_orders": live,
        "shortcuts": [
            {"title": _("All orders"), "link": f"{ADMIN_ROOT}api/order/"},
            {"title": _("Robots"), "link": f"{ADMIN_ROOT}api/robot/"},
            {"title": _("Lightning payments"), "link": f"{ADMIN_ROOT}api/lnpayment/"},
            {
                "title": _("On-chain payments"),
                "link": f"{ADMIN_ROOT}api/onchainpayment/",
            },
        ],
        "coordinator": {
            "alias": config("COORDINATOR_ALIAS", cast=str, default="Roblosats"),
            "version": f"{version['major']}.{version['minor']}.{version['patch']}"
            if isinstance(version, dict)
            else "",
            "lnd": config("LND_GRPC_HOST", cast=str, default=""),
            "local": local,
            "remote": remote,
        },
    }
    if can_configure_alerts(request):
        data["shortcuts"].append(
            {
                "title": _("Telegram alerts"),
                "link": reverse("admin:roblosats_admin_telegramalerts_changelist"),
            }
        )
    if context is None:
        return data
    context.update(data)
    return context
