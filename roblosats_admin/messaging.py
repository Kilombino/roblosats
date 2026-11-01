from decouple import config
from django.contrib import admin, messages
from django.template.response import TemplateResponse
from roblosats_admin.i18n import _, _lazy

MESSAGE_MAX = 400
NOTIFICATION_FIELD = 240


def coordinator_alias():
    return config("COORDINATOR_ALIAS", cast=str, default="").strip() or "Roblosats"


def message_head(order_id):
    return f"{coordinator_alias()} coordinator (order {order_id}): "


def message_title_prefix(order_id):
    return f"{coordinator_alias()} coordinator (order {order_id})"


def send_coordinator_message(order, robot, text):
    from api.models import Notification
    from api.tasks import nostr_send_notification_event

    from roblosats_admin.tasks import relay_coordinator_message

    full = message_head(order.id) + text[:MESSAGE_MAX]
    Notification.objects.create(
        title=full[:NOTIFICATION_FIELD],
        description=full[NOTIFICATION_FIELD : 2 * NOTIFICATION_FIELD],
        robot=robot,
        order=order,
    )
    if robot.nostr_pubkey:
        nostr_send_notification_event.delay(
            robot_id=robot.id, order_id=order.id, text=full
        )
    if robot.telegram_enabled or robot.webhook_enabled:
        relay_coordinator_message.delay(robot_id=robot.id, order_id=order.id, text=full)
    return full


def robots_for(order, who):
    robots = []
    if who in ("both", "maker") and order.maker_id:
        robots.append(order.maker.robot)
    if who in ("both", "taker") and order.taker_id:
        robots.append(order.taker.robot)
    return robots


@admin.action(description=_lazy("Send a message to maker / taker"))
def message_parties(modeladmin, request, queryset):
    if "send_message" in request.POST:
        text = request.POST.get("text", "").strip()
        who = request.POST.get("who", "both")
        if who not in ("both", "maker", "taker"):
            who = "both"
        if not text:
            modeladmin.message_user(
                request, _("The message is empty: nothing was sent."), messages.ERROR
            )
            return None
        if len(text) > MESSAGE_MAX:
            modeladmin.message_user(
                request,
                _(
                    "The message has %(n)s characters; the limit is %(max)s. Nothing was sent."
                )
                % {"n": len(text), "max": MESSAGE_MAX},
                messages.ERROR,
            )
            return None
        for order in queryset.select_related("maker__robot", "taker__robot"):
            robots = robots_for(order, who)
            for robot in robots:
                send_coordinator_message(order, robot, text)
            names = ", ".join(r.user.username for r in robots) or _("nobody")
            modeladmin.message_user(
                request,
                _("Order %(id)s: message sent to %(names)s.")
                % {"id": order.id, "names": names},
                messages.SUCCESS,
            )
        return None
    return TemplateResponse(
        request,
        "roblosats_admin/message_form.html",
        {
            **modeladmin.admin_site.each_context(request),
            "title": _("Send a message to maker / taker"),
            "opts": modeladmin.model._meta,
            "orders": queryset.select_related("maker", "taker"),
            "action": "message_parties",
            "max_len": MESSAGE_MAX,
            "head_example": message_head("N"),
            "selected": request.POST.getlist(admin.helpers.ACTION_CHECKBOX_NAME),
        },
    )
