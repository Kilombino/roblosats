import json
import logging
import uuid
from datetime import datetime, timezone
from secrets import token_urlsafe

from decouple import config
from api.models import (
    Order,
    Notification,
)
from api.utils import get_federation_short_alias, get_session
from api.tasks import nostr_send_notification_event

logger = logging.getLogger("api.notifications")


class Notifications:
    """Simple telegram messages using TG's API"""

    session = get_session()
    site = config("HOST_NAME")

    def get_context(user):
        """returns context needed to enable TG and webhook notifications"""
        context = {}
        if user.robot.telegram_enabled:
            context["tg_enabled"] = True
        else:
            context["tg_enabled"] = False

        if user.robot.telegram_token is None:
            user.robot.telegram_token = token_urlsafe()[:15]
            user.robot.save(update_fields=["telegram_token"])

        context["tg_token"] = user.robot.telegram_token
        context["tg_bot_name"] = config("TELEGRAM_BOT_NAME")

        context["webhook_enabled"] = user.robot.webhook_enabled
        context["webhook_url"] = user.robot.webhook_url or ""

        return context

    def order_url(self, order):
        """Builds the frontend order URL with the federation shortAlias route segment"""
        return f"http://{self.site}/order/{get_federation_short_alias()}/{order.id}"

    def send_message(
        self,
        order,
        robot,
        title,
        description="",
        event_type="notification",
        action=False,
    ):
        """Save a message for a user and sends it to Telegram, Nostr, and/or Webhook.
        action (0.9.4): the trader must do something now (Nostr tag, webhook field)."""
        self.save_message(order, robot, title, description)
        if robot.nostr_pubkey:
            nostr_send_notification_event.delay(
                robot_id=robot.id, order_id=order.id, text=title, action=bool(action)
            )
        if robot.telegram_enabled and robot.telegram_chat_id:
            self.send_telegram_message(robot.telegram_chat_id, title, description)
        if robot.webhook_enabled:
            self.send_webhook_message(
                order, robot, title, description, event_type, action=action
            )

    def save_message(self, order, robot, title, description=""):
        """Save a message for a user (the columns hold 240 characters)"""
        Notification.objects.create(
            title=title[:240],
            description=(description or "")[:240],
            robot=robot,
            order=order,
        )

    def send_telegram_message(self, chat_id, title, description=""):
        """sends a message to a user with telegram notifications enabled"""

        bot_token = config("TELEGRAM_TOKEN")
        text = f"{title} {description}".strip()
        message_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        # 0.9.4: the text goes URL-encoded (a "&" or "#" cut it before) and a dead Telegram
        # no longer holds a worker forever.
        for _ in range(5):
            try:
                self.session.get(
                    message_url,
                    params={"chat_id": chat_id, "text": text},
                    timeout=60,
                ).json()
                return
            except Exception:
                pass

    def send_webhook_message(
        self,
        order,
        robot,
        title,
        description="",
        event_type="notification",
        action=False,
    ):
        """Sends a webhook notification to the user's custom HTTP endpoint (Tor .onion only)"""
        from api.models import Robot

        webhook_url = robot.webhook_url
        if not Robot.is_valid_onion_url(webhook_url):
            logger.warning(
                f"Webhook URL rejected: not a .onion address for robot {robot.id}"
            )
            return
        payload = {
            "event_type": event_type,
            "event_id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "robot_hash_id": robot.hash_id,
            "order": {
                "id": order.id,
                "type": "BUY" if order.type == Order.Types.BUY else "SELL",
                "status": order.status,
            },
            "message": {
                "title": title,
                "description": description,
            },
            "action_needed": bool(action),
            "metadata": {
                "coordinator": config("COORDINATOR_ALIAS", cast=str, default="Unknown"),
                "platform_version": config("VERSION", cast=str, default="Unknown"),
            },
        }

        headers = {
            "Content-Type": "application/json",
        }

        if robot.webhook_api_key:
            headers["X-API-Key"] = robot.webhook_api_key

        try:
            response = self.session.post(
                webhook_url,
                data=json.dumps(payload),
                headers=headers,
                timeout=60,
            )
            response.raise_for_status()
            logger.info(f"Webhook sent successfully to robot {robot.id}")
        except Exception as e:
            logger.error(f"Webhook failed for robot {robot.id}: {e}")

    def send_webhook_test(self, robot):
        """Sends a test webhook notification when webhook is first configured"""
        from api.models import Robot

        webhook_url = robot.webhook_url
        if not Robot.is_valid_onion_url(webhook_url):
            logger.warning(
                f"Webhook test rejected: not a .onion address for robot {robot.id}"
            )
            return False

        payload = {
            "event_type": "webhook_test",
            "event_id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "robot_hash_id": robot.hash_id,
            "message": {
                "title": f"🔔 Hey {robot.user.username}, your webhook is configured!",
                "description": "You will receive notifications about your RoboSats orders.",
            },
            "metadata": {
                "coordinator": config("COORDINATOR_ALIAS", cast=str, default="Unknown"),
                "platform_version": config("VERSION", cast=str, default="Unknown"),
            },
        }

        headers = {
            "Content-Type": "application/json",
        }

        if robot.webhook_api_key:
            headers["X-API-Key"] = robot.webhook_api_key

        try:
            response = self.session.post(
                webhook_url,
                data=json.dumps(payload),
                headers=headers,
                timeout=60,
            )
            response.raise_for_status()
            logger.info(f"Webhook test sent successfully to robot {robot.id}")
            return True
        except Exception as e:
            logger.error(f"Webhook test failed for robot {robot.id}: {e}")
            return False

    def welcome(self, user):
        """User enabled Telegram Notifications"""
        lang = user.robot.telegram_lang_code

        if lang == "es":
            title = f"🔔 Hola {user.username}, te enviaré notificaciones sobre tus órdenes en RoboSats."
        else:
            title = f"🔔 Hey {user.username}, I will send you notifications about your RoboSats orders."
        self.send_telegram_message(user.robot.telegram_chat_id, title)
        user.robot.telegram_welcomed = True
        user.robot.save(update_fields=["telegram_welcomed"])
        return

    def onchain_seen_mempool(self, order, user_id, what, sats, short=None, low=None):
        """Roblosats onchain escrow: a payment of this robot (its bond or its funding) is in
        the mempool, waiting for 1 confirmation."""
        user = order.maker if order.maker_id == user_id else order.taker
        if user is None or user.id != user_id:
            return
        from api.step_alerts import lang_of

        lang = lang_of(user.robot)
        amount = f"{sats:,}".replace(",", ".")
        if lang == "es":
            concept = "tu fianza" if what == "bond" else "tu depósito"
            title = f"📥 Pago visto en la mempool: {concept} de {amount} sats (orden {order.id}), esperando 1 confirmación."
            if what == "bond" and short is not None:
                if short:
                    missing = f"{short:,}".replace(",", ".")
                    title = (f"📥 Pago de {amount} sats visto en la mempool para tu fianza (orden {order.id}). "
                             f"Con lo que ya enviaste NO llega a la fianza: faltan {missing} sats. Envía lo que falta a la misma dirección.")
                else:
                    title = (f"📥 Pago de {amount} sats visto en la mempool para tu fianza (orden {order.id}). "
                             f"Con lo que ya enviaste llega a la fianza; esperando 1 confirmación.")
        elif lang == "ca":
            concept = "la teva fiança" if what == "bond" else "el teu dipòsit"
            title = f"📥 Pagament vist a la mempool: {concept} de {amount} sats (ordre {order.id}), esperant 1 confirmació."
            if what == "bond" and short is not None:
                if short:
                    missing = f"{short:,}".replace(",", ".")
                    title = (f"📥 Pagament de {amount} sats vist a la mempool per a la teva fiança (ordre {order.id}). "
                             f"Amb el que ja vas enviar NO arriba a la fiança: falten {missing} sats. Envia el que falta a la mateixa adreça.")
                else:
                    title = (f"📥 Pagament de {amount} sats vist a la mempool per a la teva fiança (ordre {order.id}). "
                             f"Amb el que ja vas enviar arriba a la fiança; esperant 1 confirmació.")
        else:
            concept = "your bond" if what == "bond" else "your deposit"
            title = f"📥 Payment seen in the mempool: {concept} of {amount} Sats (order {order.id}), waiting for 1 confirmation."
            if what == "bond" and short is not None:
                if short:
                    title = (f"📥 Payment of {amount} Sats seen in the mempool for your bond (order {order.id}). "
                             f"Together with what you already sent it does NOT reach the bond: {short:,} Sats missing. Send the rest to the same address.")
                else:
                    title = (f"📥 Payment of {amount} Sats seen in the mempool for your bond (order {order.id}). "
                             f"Together with what you already sent it reaches the bond; waiting for 1 confirmation.")
        if low:
            # rl31-beta19: it pays less than the mempool needs for about an hour
            paid, needed = (f"{r:g}".replace(".", ",") if lang in ("es", "ca") else f"{r:g}" for r in low)
            if lang == "es":
                title += (f" ⚠️ Tu pago paga {paid} sat/vB y ahora la mempool pide {needed}: "
                          f"acelera la transacción (RBF o CPFP) o puede tardar mucho en confirmarse.")
            elif lang == "ca":
                title += (f" ⚠️ El teu pagament paga {paid} sat/vB i ara la mempool demana {needed}: "
                          f"accelera la transacció (RBF o CPFP) o pot trigar molt a confirmar-se.")
            else:
                title += (f" ⚠️ Your payment pays {paid} sat/vB and the mempool currently needs {needed}: "
                          f"speed it up (RBF or CPFP) or it may take a long time to confirm.")
        self.send_message(order, user.robot, title, self.order_url(order), action=bool(low))

    SIGN_WHAT = {
        "bond_signature": ("your bond", "tu fianza", "la teva fiança"),
        "rescue_signature": ("the rescue", "el rescate", "el rescat"),
        "deposit_signature": ("the deposit", "el depósito", "el dipòsit"),
        "release_signature": ("the release", "la liberación", "l'alliberament"),
    }

    def onchain_sign_needed(self, order, user_id, what):
        """0.9.3: the coordinator waits for this robot's signature; its app signs by itself, but
        only while the order is open in the foreground."""
        user = order.maker if order.maker_id == user_id else order.taker
        if user is None or user.id != user_id:
            return
        en, es, ca = self.SIGN_WHAT.get(what, ("the next step", "el siguiente paso", "el pas següent"))
        from api.step_alerts import lang_of

        lang = lang_of(user.robot)
        if lang == "es":
            title = (f"✍️ Abre la orden {order.id}: tu app tiene que firmar {es}. "
                     "Déjala abierta en primer plano en este dispositivo hasta que termine.")
        elif lang == "ca":
            title = (f"✍️ Obre l'ordre {order.id}: la teva app ha de signar {ca}. "
                     "Deixa-la oberta en primer pla en aquest dispositiu fins que acabi.")
        else:
            title = (f"✍️ Open order {order.id}: your app needs to sign {en}. "
                     "Keep it open in the foreground on this device until it is done.")
        self.send_message(order, user.robot, title, self.order_url(order), action=True)

    def take_lost(self, order, user_id):
        """0.9.8: another robot's bond locked first. The onchain bond of this robot is free (it
        was never at stake): don't send it now; if it was sent, get it back from the order."""
        from django.contrib.auth.models import User

        from api.step_alerts import lang_of

        user = User.objects.filter(id=user_id).first()
        if user is None or order.taker_id == user_id or order.maker_id == user_id:
            return
        lang = lang_of(user.robot)
        if lang == "es":
            title = (f"⚡ Otro robot tomó la orden {order.id} antes que tú: su fianza se bloqueó primero. "
                     "Tu fianza on-chain no está en juego y no pierdes nada. Si aún no la enviaste, no la envíes. "
                     "Si ya la enviaste, abre la orden para recuperarla (cuando tenga 1 confirmación).")
        elif lang == "ca":
            title = (f"⚡ Un altre robot ha pres l'ordre {order.id} abans que tu: la seva fiança s'ha bloquejat primer. "
                     "La teva fiança on-chain no està en joc i no perds res. Si encara no l'has enviada, no l'enviïs. "
                     "Si ja l'has enviada, obre l'ordre per recuperar-la (quan tingui 1 confirmació).")
        else:
            title = (f"⚡ Another robot took order {order.id} before you: its bond locked first. "
                     "Your onchain bond is not at stake and you lose nothing. If you have not sent it yet, don't. "
                     "If you already sent it, open the order to get it back (once it has 1 confirmation).")
        self.send_message(order, user.robot, title, self.order_url(order), action=True)

    def order_taken_confirmed(self, order):
        lang = order.maker.robot.telegram_lang_code
        if lang == "es":
            title = f"✅ Hey {order.maker.username} ¡Tu orden con ID {order.id} ha sido tomada por {order.taker.username}!🥳"
            description = f"Visita {self.order_url(order)} para continuar."
        else:
            title = f"✅ Hey {order.maker.username}, your order was taken by {order.taker.username}!🥳"
            description = f"Visit {self.order_url(order)} to proceed with the trade."
        self.send_message(order, order.maker.robot, title, description)

        lang = order.taker.robot.telegram_lang_code
        if lang == "es":
            title = f"✅ Hey {order.taker.username}, acabas de tomar la orden con ID {order.id}."
        else:
            title = f"✅ Hey {order.taker.username}, you just took the order with ID {order.id}."
        self.send_message(order, order.taker.robot, title)

        return

    def fiat_exchange_starts(self, order):
        for user in [order.maker, order.taker]:
            lang = user.robot.telegram_lang_code
            if lang == "es":
                title = f"✅ Hey {user.username}, el depósito de garantía y el recibo del comprador han sido recibidos. Es hora de enviar el dinero fiat."
                description = (
                    f"Visita {self.order_url(order)} para hablar con tu contraparte."
                )
            else:
                title = f"✅ Hey {user.username}, the escrow and invoice have been submitted. The fiat exchange starts now via the platform chat."
                description = (
                    f"Visit {self.order_url(order)} to talk with your counterpart."
                )
            self.send_message(order, user.robot, title, description)
        return

    def order_expired_untaken(self, order):
        lang = order.maker.robot.telegram_lang_code
        if lang == "es":
            title = f"😪 Hey {order.maker.username}, tu orden con ID {order.id} ha expirado sin ser tomada por ningún robot."
            description = f"Visita {self.order_url(order)} para renovarla."
        else:
            title = f"😪 Hey {order.maker.username}, your order with ID {order.id} has expired without a taker."
            description = f"Visit {self.order_url(order)} to renew it."
        self.send_message(order, order.maker.robot, title, description)
        return

    def trade_successful(self, order):
        for user in [order.maker, order.taker]:
            lang = user.robot.telegram_lang_code
            if lang == "es":
                title = f"🥳 ¡Tu orden con ID {order.id} ha finalizado exitosamente!"
                description = (
                    "⚡ Únete a nosotros en @robosats_es y ayúdanos a mejorar."
                )
            else:
                title = f"🥳 Your order with ID {order.id} has finished successfully!"
                description = "⚡ Join us @robosats and help us improve."
            self.send_message(order, user.robot, title, description)
        return

    def public_order_cancelled(self, order):
        lang = order.maker.robot.telegram_lang_code
        if lang == "es":
            title = f"❌ Hey {order.maker.username}, has cancelado tu orden pública con ID {order.id}."
        else:
            title = f"❌ Hey {order.maker.username}, you have cancelled your public order with ID {order.id}."
        self.send_message(order, order.maker.robot, title)
        return

    def collaborative_cancelled(self, order):
        for user in [order.maker, order.taker]:
            lang = user.robot.telegram_lang_code
            if lang == "es":
                title = f"❌ Hey {user.username}, tu orden con ID {str(order.id)} fue cancelada colaborativamente."
            else:
                title = f"❌ Hey {user.username}, your order with ID {str(order.id)} has been collaboratively cancelled."
            self.send_message(order, user.robot, title)
        return

    def dispute_opened(self, order):
        for user in [order.maker, order.taker]:
            lang = user.robot.telegram_lang_code
            if lang == "es":
                title = f"⚖️ Hey {user.username}, la orden con ID {str(order.id)} ha entrado en disputa."
            else:
                title = f"⚖️ Hey {user.username}, a dispute has been opened on your order with ID {str(order.id)}."
            self.send_message(order, user.robot, title)

        from django.apps import apps

        if apps.is_installed("roblosats_admin"):
            return

        admin_chat_id = config("TELEGRAM_COORDINATOR_CHAT_ID")

        if len(admin_chat_id) == 0:
            return

        coordinator_text = (
            f"There is a new dispute opened for the order with ID {str(order.id)}."
        )
        coordinator_description = f"Visit http://{self.site}/coordinator/api/order/{str(order.id)}/change to proceed."
        self.send_telegram_message(
            admin_chat_id, coordinator_text, coordinator_description
        )

        return

    def order_published(self, order):
        lang = order.maker.robot.telegram_lang_code
        # In weird cases the order cannot be found (e.g. it is cancelled)
        queryset = Order.objects.filter(maker=order.maker)
        if len(queryset) == 0:
            return
        order = queryset.last()
        if lang == "es":
            title = f"✅ Hey {order.maker.username}, tu orden con ID {str(order.id)} es pública en el libro de ordenes."
        else:
            title = f"✅ Hey {order.maker.username}, your order with ID {str(order.id)} is public in the order book."
        self.send_message(order, order.maker.robot, title)
        return

    def new_chat_message(self, order, chat_message):
        """
        Sends a TG notification for a new in-app chat message if
        the last chat was at least CHAT_NOTIFICATION_TIMEGAP minutes ago.
        """
        from datetime import timedelta

        from django.utils import timezone

        from chat.models import Message

        TIMEGAP = config("CHAT_NOTIFICATION_TIMEGAP", cast=int, default=5)
        if chat_message.index > 1:
            previous_message = Message.objects.get(
                chatroom=chat_message.chatroom, index=(chat_message.index - 1)
            )
            notification_reason = f"(You receive this notification only because more than {TIMEGAP} minutes have passed since the last in-chat message)"
            if previous_message.created_at > timezone.now() - timedelta(
                minutes=TIMEGAP
            ):
                return
        else:
            notification_reason = f"(You receive this notification because this was the first in-chat message. You will only be notified again if there is a gap bigger than {TIMEGAP} minutes between messages)"

        # 0.9.4: in the robot's language, with the order number (step_alerts)
        from api import step_alerts

        step_alerts.on_chat_message(order, chat_message)
        return

    def coordinator_cancelled(self, order):
        title = f"🛠️ Your order with ID {order.id} has been cancelled by the coordinator {config('COORDINATOR_ALIAS', cast=str, default='NoAlias')} for the upcoming maintenance stop."
        self.send_message(order, order.maker.robot, title)
        return

    def dispute_closed(self, order):
        lang = order.maker.robot.telegram_lang_code
        if order.status == Order.Status.MLD:
            # Maker lost dispute
            looser = order.maker
            winner = order.taker
        elif order.status == Order.Status.TLD:
            # Taker lost dispute
            looser = order.taker
            winner = order.maker

        lang = looser.robot.telegram_lang_code
        if lang == "es":
            title = f"⚖️ Hey {looser.username}, has perdido la disputa en la orden con ID {str(order.id)}."
        else:
            title = f"⚖️ Hey {looser.username}, you lost the dispute on your order with ID {str(order.id)}."
        self.send_message(order, looser.robot, title)

        lang = winner.robot.telegram_lang_code
        if lang == "es":
            title = f"⚖️ Hey {winner.username}, has ganado la disputa en la orden con ID {str(order.id)}."
        else:
            title = f"⚖️ Hey {winner.username}, you won the dispute on your order with ID {str(order.id)}."
        self.send_message(order, winner.robot, title)

        return

    def lightning_failed(self, order):
        lang = order.maker.robot.telegram_lang_code
        if order.type == Order.Types.BUY:
            buyer = order.maker
        else:
            buyer = order.taker

        if lang == "es":
            title = f"⚡❌ Hey {buyer.username}, el pago lightning en la order con ID {str(order.id)} ha fallado."
            description = "Intentalo de nuevo con una nueva factura o con otra wallet."
        else:
            title = f"⚡❌ Hey {buyer.username}, the lightning payment on your order with ID {str(order.id)} failed."
            description = "Try again with a new invoice or from another wallet."

        self.send_message(order, buyer.robot, title, description)
        return
