"""Failed Lightning payouts to the buyer (order status 15).

When the Lightning payment to the buyer does not arrive, the order stays in 15
until they submit an invoice that can be paid. The database only keeps the
CURRENT invoice (earlier ones are left without a link to the order), so the
history of invoices and attempts comes from the order's JSON log, which records
every invoice added and every failed attempt with its reason.

Each invoice's destination (final node and route-hint nodes) is decoded by the
coordinator's LND and cached (Django cache, i.e. Redis) so the page stays fast.
Full invoices and preimages are never shown; node public keys are, they are
public anyway.

Read-only: it never touches orders or payments.
"""

import json
import re
import time

from decouple import config
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils import timezone

from roblosats_admin.disputes import (
    ORDER_STATUS,
    PAYMENT_STATUS,
    _languages,
    _parse_time,
    _require_staff,
    amount_text,
    fmt_sats,
    fmt_time,
    roles,
)
from roblosats_admin.i18n import _, _lazy

FAILED = 15

_RE_INVOICE = re.compile(
    r"added the invoice LNPayment\(([0-9a-fA-F]{64}),[^)]*\) as payout method\."
    r"(?:.*?Amount to be sent is (\d+) Sats)?"
    r"(?:.*?routing budget is ([\d.]+) Sats \((\d+)ppm\))?"
)
_RE_FAILURE = re.compile(
    r"Payment LNPayment\(([0-9a-fA-F]{64}),([^)]*)\) \*\*failed\*\*\.\s*"
    r"Failure reason:\s*(.*)$",
    re.S,
)
INTO_FAILED = "to 15: *Failed lightning"

CACHE_DESTINATION = 30 * 86400  # an invoice never changes destination
CACHE_ALIAS = 86400
CACHE_ERROR = 300
LND_BUDGET = 8.0  # max seconds per page spent talking to LND
HISTORY_LIMIT = 200

LIST_VIEWS = {
    "now": _lazy("Failing now"),
    "history": _lazy("History"),
}


def payout_timeout():
    try:
        return int(config("PAYOUT_TIMEOUT_SECONDS", default=90))
    except (TypeError, ValueError):
        return 90


def reason(text_or_code):
    """Failure reason in plain words, from the LNPayment code or from the English
    text the log keeps."""
    by_code = {
        1: _("Ran out of time (%(s)s s) looking for a route, with routes still left to try")
        % {"s": payout_timeout()},
        2: _("No route reached their wallet (all failed, or there was none)"),
        3: _("Non-recoverable error while paying"),
        4: _("The invoice does not work for this payment (unknown hash, wrong amount or CLTV)"),
        5: _("The coordinator's node did not have enough balance in its channels"),
    }
    if isinstance(text_or_code, int):
        return by_code.get(text_or_code, "")
    t = (text_or_code or "").lower()
    if "timeout" in t:
        return by_code[1]
    if "permanently" in t or "no routes" in t or "no_route" in t:
        return by_code[2]
    if "non-recoverable" in t:
        return by_code[3]
    if "incorrect" in t or "unknown hash" in t:
        return by_code[4]
    if "balance" in t:
        return by_code[5]
    return (text_or_code or "").strip().rstrip(")") or _("unknown reason")


def read_history(order):
    """Invoices the buyer submitted and failed attempts, from the order log."""
    try:
        entries = json.loads(order.logs) if order.logs else []
    except (TypeError, ValueError):
        entries = []
    if not isinstance(entries, list):
        entries = []

    def blank(h, moment):
        return {
            "hash": h,
            "time": moment,
            "failures": 0,
            "last": None,
            "reason": "",
            "sats_log": None,
            "budget": "",
        }

    invoices = {}  # hash -> data, in arrival order
    attempts = 0
    times_failed = 0
    latest = None  # (moment, English text)
    for e in entries:
        if not isinstance(e, dict):
            continue
        event = str(e.get("event", ""))
        moment = _parse_time(e.get("timestamp"))
        if INTO_FAILED in event:
            times_failed += 1
            continue
        m = _RE_INVOICE.search(event)
        if m:
            h = m.group(1).lower()
            inv = invoices.setdefault(h, blank(h, moment))
            if m.group(2):
                inv["sats_log"] = int(m.group(2))
            if m.group(3):
                inv["budget"] = f"{float(m.group(3)):.0f} sats ({m.group(4)} ppm)"
            continue
        m = _RE_FAILURE.search(event)
        if m and ("Payment to buyer" in m.group(2) or m.group(1).lower() in invoices):
            h = m.group(1).lower()
            text = m.group(3).strip()
            if text.endswith(")"):
                text = text[:-1]
            attempts += 1
            inv = invoices.setdefault(h, blank(h, None))
            inv["failures"] += 1
            inv["last"] = moment
            inv["reason"] = text
            if moment is not None and (latest is None or moment >= latest[0]):
                latest = (moment, text)
    return {
        "invoices": list(invoices.values()),
        "attempts": attempts,
        "times_failed": times_failed,
        "last_time": latest[0] if latest else None,
        "last_text": latest[1] if latest else "",
    }


# ---------------------------------------------------------------- LND


class Budget:
    """Time cap per page: if LND is slow, whatever is left shows as "unknown"
    instead of hanging the panel."""

    def __init__(self, seconds=LND_BUDGET):
        self.end = time.monotonic() + seconds

    def left(self):
        return self.end - time.monotonic()


def _cache_get(key):
    try:
        from django.core.cache import cache

        return cache.get(key)
    except Exception:  # noqa: BLE001 - without a cache the page still works
        return None


def _cache_set(key, value, seconds):
    try:
        from django.core.cache import cache

        cache.set(key, value, seconds)
    except Exception:  # noqa: BLE001
        pass


def _stub():
    from api.lightning import lightning_pb2_grpc
    from api.lightning.lnd import LNDNode

    # the channel already carries the macaroon (metadata_call_credentials)
    return lightning_pb2_grpc.LightningStub(LNDNode.channel)


def _alias(pubkey, budget):
    if not pubkey:
        return ""
    key = f"rsa:alias:{pubkey}"
    alias = _cache_get(key)
    if alias is not None:
        return alias
    if budget.left() <= 0.5:
        return ""
    try:
        from api.lightning import lightning_pb2

        r = _stub().GetNodeInfo(
            lightning_pb2.NodeInfoRequest(pub_key=pubkey, include_channels=False),
            timeout=min(5.0, budget.left()),
        )
        alias = r.node.alias or ""
        _cache_set(key, alias, CACHE_ALIAS)
    except Exception:  # noqa: BLE001 - private node, CLN backend or LND down
        alias = ""
        _cache_set(key, alias, CACHE_ERROR)
    return alias


def _decode(lnp, budget):
    """{"destination": pubkey, "hints": [pubkeys]} or None if it could not be decoded."""
    if lnp is None or not getattr(lnp, "invoice", None):
        return None
    key = f"rsa:dest:{lnp.payment_hash}"
    d = _cache_get(key)
    if d is not None:
        return d or None
    if budget.left() <= 0.5:
        return None
    try:
        from api.lightning import lightning_pb2

        r = _stub().DecodePayReq(
            lightning_pb2.PayReqString(pay_req=lnp.invoice),
            timeout=min(5.0, budget.left()),
        )
        hints = []
        for route in r.route_hints:
            for hop in route.hop_hints:
                if hop.node_id and hop.node_id not in hints and hop.node_id != r.destination:
                    hints.append(hop.node_id)
        d = {"destination": r.destination, "hints": hints}
        _cache_set(key, d, CACHE_DESTINATION)
        return d
    except Exception:  # noqa: BLE001
        _cache_set(key, {}, CACHE_ERROR)
        return None


def _short(pubkey):
    return f"{pubkey[:8]}…{pubkey[-6:]}" if pubkey and len(pubkey) > 16 else (pubkey or "")


def _node(pubkey, budget):
    return {
        "pubkey": pubkey,
        "short": _short(pubkey),
        "alias": _alias(pubkey, budget) or _("unknown"),
    }


def destination(lnp, budget):
    """Where an invoice goes, ready to render (never the invoice itself)."""
    d = _decode(lnp, budget)
    if not d:
        return {"ok": False, "node": None, "hints": []}
    return {
        "ok": True,
        "node": _node(d.get("destination", ""), budget),
        "hints": [_node(p, budget) for p in d.get("hints", [])],
    }


# ---------------------------------------------------------------- summary


def payout_summary(order, budget=None, with_invoices=False):
    """What there is to know about the payment to the buyer of one order."""
    from api.models import LNPayment

    budget = budget or Budget()
    hist = read_history(order)
    current = order.payout
    hashes = [i["hash"] for i in hist["invoices"]]
    if current is not None and current.payment_hash not in hashes:
        hashes.append(current.payment_hash)
    payments = {p.payment_hash: p for p in LNPayment.objects.filter(payment_hash__in=hashes)}

    last_time = hist["last_time"]
    if current is not None and current.last_routing_time and (
        last_time is None or current.last_routing_time > last_time
    ):
        last_time = current.last_routing_time
    if hist["last_text"]:
        why = reason(hist["last_text"])
    elif current is not None and current.failure_reason:
        why = reason(int(current.failure_reason))
    else:
        why = ""

    destinations = set()
    rows = []
    if with_invoices:
        for inv in hist["invoices"]:
            p = payments.get(inv["hash"])
            d = destination(p, budget) if p is not None else None
            if d and d["ok"]:
                destinations.add(d["node"]["pubkey"])
            rows.append(
                {
                    "time": fmt_time(inv["time"] or (p.created_at if p else None)),
                    "sats": fmt_sats(p.num_satoshis if p else inv["sats_log"]),
                    "budget": inv["budget"],
                    "failures": inv["failures"],
                    "last": fmt_time(inv["last"]),
                    "reason": reason(inv["reason"]) if inv["reason"] else "",
                    "status": PAYMENT_STATUS.get(p.status, p.get_status_display())
                    if p
                    else _("not in the database"),
                    "current": current is not None and inv["hash"] == current.payment_hash,
                    "destination": d,
                    "url": reverse("admin:api_lnpayment_change", args=[p.pk]) if p else "",
                }
            )

    return {
        "invoices": len(hist["invoices"]) or (1 if current is not None else 0),
        "attempts": hist["attempts"] or (current.routing_attempts if current is not None else 0),
        "times_failed": hist["times_failed"],
        "last": fmt_time(last_time),
        "reason": why,
        "destination": destination(current, budget),
        "invoice_status": PAYMENT_STATUS.get(current.status, current.get_status_display())
        if current is not None
        else "",
        "sats": fmt_sats(current.num_satoshis) if current is not None else "?",
        "budget": hist["invoices"][-1]["budget"] if hist["invoices"] else "",
        "rows": rows,
        "same_destination": len(destinations) == 1 and len(rows) > 1,
        "distinct_destinations": len(destinations),
        "timeout": payout_timeout(),
    }


def order_section(order):
    """The order page's "Payment to the buyer" block: only for Lightning payouts
    that are failing now (15) or failed before succeeding. None otherwise."""
    if order.is_swap or (order.status != FAILED and INTO_FAILED not in (order.logs or "")):
        return None
    try:
        r = payout_summary(order, with_invoices=True)
    except Exception:  # noqa: BLE001 - extra information, never break the page
        return None
    r["live"] = order.status == FAILED
    return r


def count_failed():
    from api.models import Order

    return Order.objects.filter(status=FAILED).count()


def failed_payouts_view(order_admin, request):
    from api.models import Order

    _require_staff(request)
    view = request.GET.get("view", "now")
    if view not in LIST_VIEWS:
        view = "now"
    budget = Budget()
    qs = Order.objects.select_related("maker", "taker", "currency", "payout")

    def row(o):
        r = roles(o)
        name = "admin:api_order_dispute" if o.is_disputed else "admin:api_order_summary"
        return {
            "id": o.pk,
            "url": reverse(name, args=[o.pk]),
            "amount": amount_text(o),
            "currency": str(o.currency) if o.currency else "",
            "buyer": r["buyer"].username if r["buyer"] else "?",
            "status": ORDER_STATUS.get(o.status, o.get_status_display()),
            "completed": o.status == Order.Status.SUC,
            **payout_summary(o, budget),
        }

    if view == "history":
        orders = (
            qs.filter(logs__contains=INTO_FAILED)
            .exclude(status=FAILED)
            .order_by("-created_at")[:HISTORY_LIMIT]
        )
    else:
        orders = qs.filter(status=FAILED).order_by("-last_satoshis_time", "-created_at")
    list_url = reverse("admin:api_order_failed_payouts")
    n = count_failed()
    tabs = []
    for key, label in LIST_VIEWS.items():
        text = str(label)
        if key == "now" and n:
            text += f" ({n})"
        tabs.append({"label": text, "url": f"{list_url}?view={key}", "current": key == view})
    return TemplateResponse(
        request,
        "roblosats_admin/failed_payouts.html",
        {
            **order_admin.admin_site.each_context(request),
            "title": _("Failed payouts: history")
            if view == "history"
            else _("Failed payouts to the buyer"),
            "opts": order_admin.model._meta,
            "view": view,
            "tabs": tabs,
            "rows": [row(o) for o in orders],
            "history_limit": HISTORY_LIMIT,
            "timeout": payout_timeout(),
            "tz": timezone.get_current_timezone_name(),
            "languages": _languages(request),
        },
    )
