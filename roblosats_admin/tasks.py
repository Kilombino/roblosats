import logging

from celery import shared_task
from decouple import config

logger = logging.getLogger("roblosats_admin")

TELEGRAM_ATTEMPTS = 3


def _redact(text, token):
    text = str(text)
    return text.replace(token, "***") if token else text


def telegram_send_once(session, token, chat_id, text, timeout=60):
    if not token or not chat_id:
        return False, "missing token or chat id"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        response = session.post(
            url,
            data={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=timeout,
        )
    except Exception as e:  # noqa: BLE001
        return False, _redact(e, token)
    if response.ok:
        return True, ""
    try:
        detail = response.json().get("description") or response.reason
    except Exception:  # noqa: BLE001
        detail = response.reason
    return False, _redact(f"{response.status_code} {detail}", token)


def send_telegram(session, chat_id, text, token=None):
    if token is None:
        token = config("TELEGRAM_TOKEN", cast=str, default="")
    if not token or not chat_id:
        return False
    for _attempt in range(TELEGRAM_ATTEMPTS):
        ok, detail = telegram_send_once(session, token, chat_id, text)
        if ok:
            return True
        logger.warning(f"Telegram message failed: {detail}")
    return False


@shared_task(name="roblosats_coordinator_message", ignore_result=True, time_limit=300)
def relay_coordinator_message(robot_id=None, order_id=None, text=""):
    from api.models import Order, Robot
    from api.notifications import Notifications

    robot = Robot.objects.get(id=robot_id)
    order = Order.objects.get(id=order_id)
    notifications = Notifications()
    if robot.telegram_enabled:
        send_telegram(notifications.session, robot.telegram_chat_id, text)
    if robot.webhook_enabled:
        notifications.send_webhook_message(
            order, robot, text[:240], text[240:480], "coordinator_message"
        )


@shared_task(name="roblosats_telegram_alert", ignore_result=True, time_limit=300)
def telegram_alert(event=None, order_id=None, extra=None):
    from roblosats_admin import alerts

    alerts.deliver(event, order_id, extra or {})
