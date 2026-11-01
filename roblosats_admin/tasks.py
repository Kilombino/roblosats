import logging

from celery import shared_task
from decouple import config

logger = logging.getLogger("roblosats_admin")

TELEGRAM_ATTEMPTS = 3


def send_telegram(session, chat_id, text):
    token = config("TELEGRAM_TOKEN", cast=str, default="")
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for _attempt in range(TELEGRAM_ATTEMPTS):
        try:
            response = session.post(
                url, data={"chat_id": chat_id, "text": text}, timeout=60
            )
            if response.ok:
                return True
        except Exception as e:
            logger.warning(f"Telegram coordinator message failed: {e}")
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
