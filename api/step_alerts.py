"""Roblosats 0.9.4: one short notice for every step of an order where a trader must act or where
something important happened, for both roles (maker/taker, buyer/seller) and for Lightning,
onchain and hybrid orders.

Every notice goes through Notifications.send_message, so it reaches every channel the robot has
(Telegram if enabled, the Nostr DM the apps show as a notification, the webhook if enabled) and is
saved in the robot's notification list. Texts in English, Spanish and Catalan, always with the
order number and what to do next. Notices that need the trader to do something carry an "action"
flag (Nostr tag, webhook field): the Android app shows them in its "Order needs you" channel.

No spam: one notice per event (a cache key per order, robot and event), nothing for a step the
robot just did itself, and the Telegram/webhook toggles of the robot are respected.
"""

import logging
from datetime import timedelta

from decouple import config
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger("api.step_alerts")

DEDUP_TTL = 40 * 24 * 3600  # an order never lives that long
WATCH_EVERY = 20  # seconds between two looks at an onchain order (the loop runs every 5 s)
DEADLINE_MARKS = (30, 10)  # minutes before a deadline the robot must meet
# a reminder is only worth it when the step lasts clearly longer than the reminder
DEADLINE_MIN_WINDOW = {30: 45 * 60, 10: 20 * 60}

LANGS = ("en", "es", "ca")


def lang_of(robot):
    code = (getattr(robot, "telegram_lang_code", None) or "").lower()
    for lang in ("es", "ca"):
        if code == lang or code.startswith(lang + "-") or code.startswith(lang + "_"):
            return lang
    return "en"


def fmt_sats(sats, lang):
    try:
        text = f"{int(sats):,}"
    except (TypeError, ValueError):
        return "?"
    return text.replace(",", ".") if lang in ("es", "ca") else text


def fmt_left(seconds):
    seconds = max(int(seconds), 0)
    minutes = (seconds + 59) // 60
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if minutes else f"{hours} h"


def left_until(order, now=None):
    if order.expires_at is None:
        return None
    now = now or timezone.now()
    return fmt_left((order.expires_at - now).total_seconds())


# What a trader has to do, and what happens otherwise (deadline reminders and step notices).
WHAT = {
    "bond_maker": ("lock your bond to publish the order", "bloquear tu fianza para publicar la orden", "bloquejar la teva fiança per publicar l'ordre"),
    "bond_taker": ("lock your bond to take the order", "bloquear tu fianza para tomar la orden", "bloquejar la teva fiança per prendre l'ordre"),
    "escrow_ln": ("lock the trade escrow", "bloquear el depósito de garantía", "bloquejar el dipòsit de garantia"),
    "escrow_onchain": ("fund the onchain escrow", "financiar el depósito on-chain", "finançar el dipòsit on-chain"),
    "fee_onchain": ("send your fee to your funding address", "enviar tu comisión a tu dirección de pago", "enviar la teva comissió a la teva adreça de pagament"),
    "payout_ln": ("send your payout invoice", "enviar tu factura de cobro", "enviar la teva factura de cobrament"),
    "address": ("give your address", "dar tu dirección", "donar la teva adreça"),
    "fiat_sent": ("send the payment and press \"Confirm sent\"", "enviar el pago y pulsar \"Confirmar enviado\"", "enviar el pagament i prémer \"Confirmar enviat\""),
    "fiat_received": ("press \"Confirm received\" once the money is in your account (or open a dispute if it did not arrive)", "pulsar \"Confirmar recibido\" cuando el dinero esté en tu cuenta (o abrir una disputa si no llegó)", "prémer \"Confirmar rebut\" quan els diners siguin al teu compte (o obrir una disputa si no han arribat)"),
    "statement": ("send your dispute statement", "enviar tu declaración de la disputa", "enviar la teva declaració de la disputa"),
    "sign": ("let your app sign (keep the order open)", "dejar que tu app firme (mantén la orden abierta)", "deixar que la teva app signi (mantén l'ordre oberta)"),
}
CONSEQUENCE = {
    "nothing": ("the order is cancelled (nothing is charged)", "la orden se cancela (no se cobra nada)", "l'ordre es cancel·la (no es cobra res)"),
    "take": ("the take is dropped (nothing is charged)", "la toma se descarta (no se cobra nada)", "la presa es descarta (no es cobra res)"),
    "bond": ("you lose your bond", "pierdes tu fianza", "perds la teva fiança"),
    "dispute": ("a dispute opens automatically", "se abre una disputa automáticamente", "s'obre una disputa automàticament"),
    "statement": ("the coordinator decides without it", "el coordinador decide sin ella", "el coordinador decideix sense ella"),
}
PEER = {
    "buyer": ("the buyer", "el comprador", "el comprador"),
    "seller": ("the seller", "el vendedor", "el venedor"),
    "taker": ("the taker", "el tomador", "el prenedor"),
}

TEXTS = {
    # --- the order in the book --------------------------------------------------------------
    "published": (
        "📢 Bond locked: order {n} is public in the book. I will tell you when someone takes it.",
        "📢 Fianza bloqueada: la orden {n} ya es pública en el libro. Te aviso cuando alguien la tome.",
        "📢 Fiança bloquejada: l'ordre {n} ja és pública al llibre. T'aviso quan algú la prengui.",
    ),
    "republished": (
        "📢 Order {n} is public again: the taker cancelled or did not finish its step in time. Your bond keeps backing the order.",
        "📢 La orden {n} vuelve a ser pública: el tomador canceló o no completó su paso a tiempo. Tu fianza sigue respaldando la orden.",
        "📢 L'ordre {n} torna a ser pública: el prenedor va cancel·lar o no va completar el seu pas a temps. La teva fiança continua avalant l'ordre.",
    ),
    "taker_dropped": (
        "⌛ Order {n}: you left the trade before the escrow (you cancelled or the time ran out). Your bond was kept as a penalty.",
        "⌛ Orden {n}: dejaste el intercambio antes del depósito (cancelaste o se acabó el tiempo). Tu fianza se ha retenido como penalización.",
        "⌛ Ordre {n}: vas deixar l'intercanvi abans del dipòsit (vas cancel·lar o es va acabar el temps). La teva fiança s'ha retingut com a penalització.",
    ),
    "take_started": (
        "🪙 You are taking order {n}: lock your onchain bond within {left} (send it to the address in the order).",
        "🪙 Estás tomando la orden {n}: bloquea tu fianza on-chain antes de {left} (envíala a la dirección de la orden).",
        "🪙 Estàs prenent l'ordre {n}: bloqueja la teva fiança on-chain abans de {left} (envia-la a l'adreça de l'ordre).",
    ),
    "being_taken": (
        "👀 Someone is taking your order {n}: waiting for their onchain bond (up to {left}). Nothing to do yet.",
        "👀 Alguien está tomando tu orden {n}: esperando su fianza on-chain (hasta {left}). Aún no tienes que hacer nada.",
        "👀 Algú està prenent la teva ordre {n}: esperant la seva fiança on-chain (fins a {left}). Encara no has de fer res.",
    ),
    "taken_ln_waiting": (
        "👀 Order {n} is being taken: waiting for the taker's bond. Nothing to do yet.",
        "👀 Están tomando la orden {n}: esperando la fianza del tomador. Aún no tienes que hacer nada.",
        "👀 Estan prenent l'ordre {n}: esperant la fiança del prenedor. Encara no has de fer res.",
    ),
    # --- the contract ---------------------------------------------------------------------
    "contract_maker": (
        "🤝 Order {n} was taken and the taker's bond is locked. Next: {next}",
        "🤝 La orden {n} ha sido tomada y la fianza del tomador está bloqueada. Ahora: {next}",
        "🤝 L'ordre {n} ha estat presa i la fiança del prenedor està bloquejada. Ara: {next}",
    ),
    "contract_taker": (
        "🤝 Your bond is locked: order {n} is yours. Next: {next}",
        "🤝 Tu fianza está bloqueada: la orden {n} es tuya. Ahora: {next}",
        "🤝 La teva fiança està bloquejada: l'ordre {n} és teva. Ara: {next}",
    ),
    "next_escrow_ln": (
        "lock the trade escrow ({sats} sats, a Lightning hold invoice) within {left}.",
        "bloquea el depósito de garantía ({sats} sats, una factura retenida de Lightning) antes de {left}.",
        "bloqueja el dipòsit de garantia ({sats} sats, una factura retinguda de Lightning) abans de {left}.",
    ),
    "next_payout_ln": (
        "send your payout invoice ({sats} sats) within {left}.",
        "envía tu factura de cobro ({sats} sats) antes de {left}.",
        "envia la teva factura de cobrament ({sats} sats) abans de {left}.",
    ),
    "next_address_buyer": (
        "give the address that will receive the sats, within {left}. Then the seller funds the escrow.",
        "da la dirección que recibirá los sats, antes de {left}. Luego el vendedor financia el depósito.",
        "dona l'adreça que rebrà els sats, abans de {left}. Després el venedor finança el dipòsit.",
    ),
    "next_address_seller": (
        "give your refund address within {left}; then you fund the escrow.",
        "da tu dirección de devolución antes de {left}; después financias el depósito.",
        "dona la teva adreça de devolució abans de {left}; després finances el dipòsit.",
    ),
    "seller_lock_now": (
        "🔒 Order {n}: the buyer is ready. Lock the trade escrow now: {sats} sats, {left} left.",
        "🔒 Orden {n}: el comprador está listo. Bloquea ya el depósito de garantía: {sats} sats, quedan {left}.",
        "🔒 Ordre {n}: el comprador està a punt. Bloqueja ja el dipòsit de garantia: {sats} sats, queden {left}.",
    ),
    "buyer_payout_now": (
        "🧾 Order {n}: the seller locked the escrow. Send your payout invoice now: {sats} sats, {left} left.",
        "🧾 Orden {n}: el vendedor ha bloqueado el depósito. Envía ya tu factura de cobro: {sats} sats, quedan {left}.",
        "🧾 Ordre {n}: el venedor ha bloquejat el dipòsit. Envia ja la teva factura de cobrament: {sats} sats, queden {left}.",
    ),
    "fund_now": (
        "⛓️ Order {n}: fund the escrow now. Send {sats} sats to the address shown in the order; {left} left.",
        "⛓️ Orden {n}: financia ya el depósito. Envía {sats} sats a la dirección que muestra la orden; quedan {left}.",
        "⛓️ Ordre {n}: finança ja el dipòsit. Envia {sats} sats a l'adreça que mostra l'ordre; queden {left}.",
    ),
    "fee_now": (
        "⛓️ Order {n}: send your fee ({sats} sats) to the address shown in the order; {left} left.",
        "⛓️ Orden {n}: envía tu comisión ({sats} sats) a la dirección que muestra la orden; quedan {left}.",
        "⛓️ Ordre {n}: envia la teva comissió ({sats} sats) a l'adreça que mostra l'ordre; queden {left}.",
    ),
    "funding_confirmed": (
        "✅ Order {n}: your payment is confirmed. The deposit is being prepared: keep the order open so your app can sign it.",
        "✅ Orden {n}: tu pago está confirmado. Se está preparando el depósito: deja la orden abierta para que tu app lo firme.",
        "✅ Ordre {n}: el teu pagament està confirmat. S'està preparant el dipòsit: deixa l'ordre oberta perquè la teva app el signi.",
    ),
    "deposit_mempool": (
        "📦 Order {n}: the deposit is signed and in the mempool. The chat opens when it gets 1 confirmation.",
        "📦 Orden {n}: el depósito está firmado y en la mempool. El chat se abre cuando tenga 1 confirmación.",
        "📦 Ordre {n}: el dipòsit està signat i a la mempool. El xat s'obre quan tingui 1 confirmació.",
    ),
    # --- the chat ---------------------------------------------------------------------------
    "chat_buyer": (
        "💬 Order {n}: {how}the chat is open. Send the payment to the seller as agreed and press \"Confirm sent\".",
        "💬 Orden {n}: {how}el chat está abierto. Envía el pago al vendedor como acordasteis y pulsa \"Confirmar enviado\".",
        "💬 Ordre {n}: {how}el xat és obert. Envia el pagament al venedor tal com vau acordar i prem \"Confirmar enviat\".",
    ),
    "chat_seller": (
        "💬 Order {n}: {how}the chat is open. The buyer now sends you the payment; confirm it only when you see the money in your account.",
        "💬 Orden {n}: {how}el chat está abierto. Ahora el comprador te envía el pago; confírmalo solo cuando veas el dinero en tu cuenta.",
        "💬 Ordre {n}: {how}el xat és obert. Ara el comprador t'envia el pagament; confirma'l només quan vegis els diners al teu compte.",
    ),
    "how_ln": ("the escrow is locked and ", "el depósito está bloqueado y ", "el dipòsit està bloquejat i "),
    "how_onchain": (
        "the deposit is confirmed, the escrow is locked and ",
        "el depósito está confirmado, la garantía bloqueada y ",
        "el dipòsit està confirmat, la garantia bloquejada i ",
    ),
    "fiat_sent": (
        "💸 Order {n}: the buyer says the payment is SENT. Check your account and press \"Confirm received\" only when you see the money.",
        "💸 Orden {n}: el comprador dice que el pago está ENVIADO. Revisa tu cuenta y pulsa \"Confirmar recibido\" solo cuando veas el dinero.",
        "💸 Ordre {n}: el comprador diu que el pagament està ENVIAT. Revisa el teu compte i prem \"Confirmar rebut\" només quan vegis els diners.",
    ),
    "fiat_sent_undone": (
        "↩️ Order {n}: the buyer undid \"payment sent\". Wait until the payment really arrives.",
        "↩️ Orden {n}: el comprador ha deshecho \"pago enviado\". Espera a que el pago llegue de verdad.",
        "↩️ Ordre {n}: el comprador ha desfet \"pagament enviat\". Espera que el pagament arribi de debò.",
    ),
    "chat_message": (
        "💬 Order {n}: new chat message from {who}.",
        "💬 Orden {n}: nuevo mensaje de {who} en el chat.",
        "💬 Ordre {n}: missatge nou de {who} al xat.",
    ),
    "collab_asked": (
        "🤝 Order {n}: {peer} asks to cancel the order by agreement. Open the order to accept it (Collaborative cancel) or keep trading.",
        "🤝 Orden {n}: {peer} pide cancelar la orden de mutuo acuerdo. Abre la orden para aceptarlo (Cancelación colaborativa) o sigue con el intercambio.",
        "🤝 Ordre {n}: {peer} demana cancel·lar l'ordre de mutu acord. Obre l'ordre per acceptar-ho (Cancel·lació col·laborativa) o continua amb l'intercanvi.",
    ),
    # --- the release ------------------------------------------------------------------------
    "paying_ln": (
        "⚡ Order {n}: the seller confirmed the payment. Paying your invoice of {sats} sats now.",
        "⚡ Orden {n}: el vendedor ha confirmado el pago. Pagando ahora tu factura de {sats} sats.",
        "⚡ Ordre {n}: el venedor ha confirmat el pagament. Pagant ara la teva factura de {sats} sats.",
    ),
    "releasing_onchain": (
        "⛓️ Order {n}: the seller confirmed the payment. The release to your address is being signed: keep the order open so your app can sign.",
        "⛓️ Orden {n}: el vendedor ha confirmado el pago. Se está firmando la liberación a tu dirección: deja la orden abierta para que tu app firme.",
        "⛓️ Ordre {n}: el venedor ha confirmat el pagament. S'està signant l'alliberament a la teva adreça: deixa l'ordre oberta perquè la teva app signi.",
    ),
    "release_mempool_buyer": (
        "📤 Order {n}: the release is in the mempool: {sats} sats to your address. Waiting for 1 confirmation.",
        "📤 Orden {n}: la liberación está en la mempool: {sats} sats a tu dirección. Esperando 1 confirmación.",
        "📤 Ordre {n}: l'alliberament és a la mempool: {sats} sats a la teva adreça. Esperant 1 confirmació.",
    ),
    "release_mempool_seller": (
        "📤 Order {n}: the release is in the mempool ({trade} sats to the buyer, {sats} sats back to you). Waiting for 1 confirmation.",
        "📤 Orden {n}: la liberación está en la mempool ({trade} sats al comprador, {sats} sats de vuelta para ti). Esperando 1 confirmación.",
        "📤 Ordre {n}: l'alliberament és a la mempool ({trade} sats al comprador, {sats} sats de tornada per a tu). Esperant 1 confirmació.",
    ),
    "success_buyer": (
        "🥳 Order {n} finished: you received {sats} sats.",
        "🥳 La orden {n} ha terminado: has recibido {sats} sats.",
        "🥳 L'ordre {n} ha acabat: has rebut {sats} sats.",
    ),
    "success_seller": (
        "🥳 Order {n} finished: the buyer received the sats and your bond is back.",
        "🥳 La orden {n} ha terminado: el comprador ha recibido los sats y tu fianza ha vuelto.",
        "🥳 L'ordre {n} ha acabat: el comprador ha rebut els sats i la teva fiança ha tornat.",
    ),
    "success_seller_onchain": (
        "🥳 Order {n} finished: the release is confirmed and {sats} sats came back to you (your bond and any extra).",
        "🥳 La orden {n} ha terminado: la liberación está confirmada y te han vuelto {sats} sats (tu fianza y lo que sobraba).",
        "🥳 L'ordre {n} ha acabat: l'alliberament està confirmat i t'han tornat {sats} sats (la teva fiança i el que sobrava).",
    ),
    "payout_failed": (
        "⚡❌ Order {n}: paying your invoice failed. Send a new invoice (or use another wallet) in the order page.",
        "⚡❌ Orden {n}: el pago de tu factura ha fallado. Envía una factura nueva (o usa otra cartera) en la página de la orden.",
        "⚡❌ Ordre {n}: el pagament de la teva factura ha fallat. Envia una factura nova (o fes servir una altra cartera) a la pàgina de l'ordre.",
    ),
    # --- cancellations, disputes and expiry ---------------------------------------------------
    "collab_done": (
        "❌ Order {n} was cancelled by agreement. {detail}",
        "❌ La orden {n} se ha cancelado de mutuo acuerdo. {detail}",
        "❌ L'ordre {n} s'ha cancel·lat de mutu acord. {detail}",
    ),
    "collab_detail_ln": (
        "The escrow and the bonds are returned.",
        "El depósito y las fianzas se devuelven.",
        "El dipòsit i les fiances es retornen.",
    ),
    "collab_detail_onchain": (
        "The escrow went back to the seller and the bonds to their owners.",
        "El depósito ha vuelto al vendedor y las fianzas a sus dueños.",
        "El dipòsit ha tornat al venedor i les fiances als seus propietaris.",
    ),
    "maker_cancelled": (
        "❌ Order {n} was cancelled by the maker before the escrow. Your bond is free.",
        "❌ El creador ha cancelado la orden {n} antes del depósito. Tu fianza queda libre.",
        "❌ El creador ha cancel·lat l'ordre {n} abans del dipòsit. La teva fiança queda lliure.",
    ),
    "dispute_opened": (
        "⚖️ Order {n} is in dispute. Send your statement and evidence (chat, receipts) in the order page; {left} left.",
        "⚖️ La orden {n} está en disputa. Envía tu declaración y pruebas (chat, recibos) en la página de la orden; quedan {left}.",
        "⚖️ L'ordre {n} està en disputa. Envia la teva declaració i proves (xat, rebuts) a la pàgina de l'ordre; queden {left}.",
    ),
    "statement_peer": (
        "⚖️ Order {n}: {peer} sent its dispute statement. Send yours too; {left} left.",
        "⚖️ Orden {n}: {peer} ha enviado su declaración de la disputa. Envía la tuya también; quedan {left}.",
        "⚖️ Ordre {n}: {peer} ha enviat la seva declaració de la disputa. Envia la teva també; queden {left}.",
    ),
    "dispute_wait": (
        "⚖️ Order {n}: both statements are in. The coordinator will resolve the dispute and I will tell you the outcome.",
        "⚖️ Orden {n}: ya están las dos declaraciones. El coordinador resolverá la disputa y te diré el resultado.",
        "⚖️ Ordre {n}: ja hi ha les dues declaracions. El coordinador resoldrà la disputa i et diré el resultat.",
    ),
    "dispute_won_buyer": (
        "⚖️ Order {n}: the dispute was resolved in your favour. The sats are sent to you; check the order page.",
        "⚖️ Orden {n}: la disputa se ha resuelto a tu favor. Se te envían los sats; revisa la página de la orden.",
        "⚖️ Ordre {n}: la disputa s'ha resolt a favor teu. Se t'envien els sats; revisa la pàgina de l'ordre.",
    ),
    "dispute_won_seller": (
        "⚖️ Order {n}: the dispute was resolved in your favour. The escrow is returned to you; check the order page.",
        "⚖️ Orden {n}: la disputa se ha resuelto a tu favor. El depósito vuelve a ti; revisa la página de la orden.",
        "⚖️ Ordre {n}: la disputa s'ha resolt a favor teu. El dipòsit torna a tu; revisa la pàgina de l'ordre.",
    ),
    # 0.9.10: onchain dispute decisions with no trade, and a dispute solved as a completed trade
    "dispute_penalized_seller": (
        "⚖️ Order {n}: the dispute was resolved with no trade and you were penalized: your bond goes to the coordinator. Your escrow amount comes back to you: keep the order open so your app signs the release.",
        "⚖️ Orden {n}: la disputa se ha resuelto sin intercambio y se te ha penalizado: tu fianza va al coordinador. El importe de tu depósito vuelve a ti: deja la orden abierta para que tu app firme la liberación.",
        "⚖️ Ordre {n}: la disputa s'ha resolt sense intercanvi i se t'ha penalitzat: la teva fiança va al coordinador. L'import del teu dipòsit torna a tu: deixa l'ordre oberta perquè la teva app signi l'alliberament.",
    ),
    "dispute_penalized_buyer": (
        "⚖️ Order {n}: the dispute was resolved with no trade and you were penalized: your bond goes to the coordinator. You do not receive the trade sats; check the order page.",
        "⚖️ Orden {n}: la disputa se ha resuelto sin intercambio y se te ha penalizado: tu fianza va al coordinador. No recibes los sats del intercambio; revisa la página de la orden.",
        "⚖️ Ordre {n}: la disputa s'ha resolt sense intercanvi i se t'ha penalitzat: la teva fiança va al coordinador. No reps els sats de l'intercanvi; revisa la pàgina de l'ordre.",
    ),
    "dispute_other_penalized_buyer": (
        "⚖️ Order {n}: the dispute was resolved with no trade: the seller was penalized. Your bond comes back to you with a share of theirs, once the seller's app signs the release.",
        "⚖️ Orden {n}: la disputa se ha resuelto sin intercambio: se ha penalizado al vendedor. Tu fianza vuelve a ti con una parte de la suya, cuando la app del vendedor firme la liberación.",
        "⚖️ Ordre {n}: la disputa s'ha resolt sense intercanvi: s'ha penalitzat el venedor. La teva fiança torna a tu amb una part de la seva, quan l'app del venedor signi l'alliberament.",
    ),
    "dispute_other_penalized_seller": (
        "⚖️ Order {n}: the dispute was resolved with no trade: the buyer was penalized. Your escrow and your bond come back to you with a share of theirs: keep the order open so your app signs the release.",
        "⚖️ Orden {n}: la disputa se ha resuelto sin intercambio: se ha penalizado al comprador. Tu depósito y tu fianza vuelven a ti con una parte de la suya: deja la orden abierta para que tu app firme la liberación.",
        "⚖️ Ordre {n}: la disputa s'ha resolt sense intercanvi: s'ha penalitzat el comprador. El teu dipòsit i la teva fiança tornen a tu amb una part de la seva: deixa l'ordre oberta perquè la teva app signi l'alliberament.",
    ),
    "dispute_completed_buyer": (
        "⚖️ Order {n}: the dispute was resolved as a completed trade. The sats and your bond are released to your address: keep the order open so your app signs the release.",
        "⚖️ Orden {n}: la disputa se ha resuelto como intercambio completado. Los sats y tu fianza se liberan a tu dirección: deja la orden abierta para que tu app firme la liberación.",
        "⚖️ Ordre {n}: la disputa s'ha resolt com a intercanvi completat. Els sats i la teva fiança s'alliberen a la teva adreça: deixa l'ordre oberta perquè la teva app signi l'alliberament.",
    ),
    "dispute_completed_seller": (
        "⚖️ Order {n}: the dispute was resolved as a completed trade. The buyer receives the sats; your bond comes back to you in the same release, which the buyer's app signs.",
        "⚖️ Orden {n}: la disputa se ha resuelto como intercambio completado. El comprador recibe los sats; tu fianza vuelve a ti en la misma liberación, que firma la app del comprador.",
        "⚖️ Ordre {n}: la disputa s'ha resolt com a intercanvi completat. El comprador rep els sats; la teva fiança torna a tu en el mateix alliberament, que signa l'app del comprador.",
    ),
    "dispute_lost": (
        "⚖️ Order {n}: the dispute was resolved against you. Your bond was lost; see the order page for the details.",
        "⚖️ Orden {n}: la disputa se ha resuelto en tu contra. Has perdido tu fianza; mira los detalles en la página de la orden.",
        "⚖️ Ordre {n}: la disputa s'ha resolt en contra teva. Has perdut la teva fiança; mira els detalls a la pàgina de l'ordre.",
    ),
    "exp_nobond": (
        "⌛ Order {n} expired: the bond was not locked in time. Nothing was charged.",
        "⌛ La orden {n} ha caducado: la fianza no se bloqueó a tiempo. No se ha cobrado nada.",
        "⌛ L'ordre {n} ha caducat: la fiança no es va bloquejar a temps. No s'ha cobrat res.",
    ),
    "exp_untaken": (
        "⌛ Order {n} expired without a taker. Your bond is free; you can renew the order from its page.",
        "⌛ La orden {n} ha caducado sin que nadie la tomara. Tu fianza queda libre; puedes renovarla desde su página.",
        "⌛ L'ordre {n} ha caducat sense que ningú la prengués. La teva fiança queda lliure; pots renovar-la des de la seva pàgina.",
    ),
    "exp_both": (
        "⌛ Order {n} expired: neither the escrow nor the payout info arrived in time. Both bonds were kept.",
        "⌛ La orden {n} ha caducado: ni el depósito ni los datos de cobro llegaron a tiempo. Se han retenido las dos fianzas.",
        "⌛ L'ordre {n} ha caducat: ni el dipòsit ni les dades de cobrament van arribar a temps. S'han retingut les dues fiances.",
    ),
    "exp_fault": (
        "⌛ Order {n} expired: you did not {what} in time. Your bond was kept as a penalty.",
        "⌛ La orden {n} ha caducado: no llegaste a {what} a tiempo. Tu fianza se ha retenido como penalización.",
        "⌛ L'ordre {n} ha caducat: no vas {what} a temps. La teva fiança s'ha retingut com a penalització.",
    ),
    "exp_victim": (
        "⌛ Order {n} expired: {peer} did not finish its step in time. Your bond is free; if you already sent sats, the order page shows how to get them back.",
        "⌛ La orden {n} ha caducado: {peer} no completó su paso a tiempo. Tu fianza queda libre; si ya enviaste sats, la página de la orden te dice cómo recuperarlos.",
        "⌛ L'ordre {n} ha caducat: {peer} no va completar el seu pas a temps. La teva fiança queda lliure; si ja vas enviar sats, la pàgina de l'ordre et diu com recuperar-los.",
    ),
    # --- after the order ----------------------------------------------------------------------
    "reward": (
        "🎁 Order {n}: you got {sats} sats of rewards (part of the other trader's bond). {when}",
        "🎁 Orden {n}: has ganado {sats} sats de recompensa (parte de la fianza del otro). {when}",
        "🎁 Ordre {n}: has guanyat {sats} sats de recompensa (part de la fiança de l'altre). {when}",
    ),
    "reward_now": (
        "Withdraw them from your robot page with a Lightning invoice.",
        "Retíralos desde la página de tu robot con una factura Lightning.",
        "Retira'ls des de la pàgina del teu robot amb una factura Lightning.",
    ),
    "reward_later": (
        "You can withdraw them from your robot page in {wait}.",
        "Podrás retirarlos desde la página de tu robot dentro de {wait}.",
        "Podràs retirar-los des de la pàgina del teu robot d'aquí a {wait}.",
    ),
    "reward_ready": (
        "🎁 Your {sats} sats of rewards from order {n} can be withdrawn now from your robot page.",
        "🎁 Tus {sats} sats de recompensa de la orden {n} ya se pueden retirar desde la página de tu robot.",
        "🎁 Els teus {sats} sats de recompensa de l'ordre {n} ja es poden retirar des de la pàgina del teu robot.",
    ),
    "bond_back": (
        "🔓 Order {n}: your onchain bond ({sats} sats) is free. Open the order and press \"Get your bond back\" to send it to your wallet.",
        "🔓 Orden {n}: tu fianza on-chain ({sats} sats) queda libre. Abre la orden y pulsa \"Recuperar tu fianza\" para enviarla a tu cartera.",
        "🔓 Ordre {n}: la teva fiança on-chain ({sats} sats) queda lliure. Obre l'ordre i prem \"Recupera la teva fiança\" per enviar-la a la teva cartera.",
    ),
    "leftover": (
        "🪙 Order {n}: {sats} sats are left at your funding address. Open the order to send them back to your wallet.",
        "🪙 Orden {n}: quedan {sats} sats en tu dirección de pago. Abre la orden para devolverlos a tu cartera.",
        "🪙 Ordre {n}: queden {sats} sats a la teva adreça de pagament. Obre l'ordre per tornar-los a la teva cartera.",
    ),
    "deadline": (
        "⏰ Order {n}: {left} left to {what}, or {cons}.",
        "⏰ Orden {n}: quedan {left} para {what}; si no, {cons}.",
        "⏰ Ordre {n}: queden {left} per {what}; si no, {cons}.",
    ),
}


def pick(table, key, lang):
    row = table[key]
    return row[LANGS.index(lang)] if lang in LANGS else row[0]


def text(key, lang, **values):
    """The notice in the robot's language. Values that are keys of WHAT/CONSEQUENCE/PEER/TEXTS
    (passed as ('table', key)) are translated too."""
    out = {}
    for k, v in values.items():
        if isinstance(v, tuple) and len(v) == 2 and v[0] in ("WHAT", "CONSEQUENCE", "PEER", "TEXTS"):
            out[k] = pick(globals()[v[0]], v[1], lang)
        else:
            out[k] = v
    for k in ("next", "detail", "when", "how"):
        if isinstance(out.get(k), tuple):  # (key, values) of a nested text
            nested_key, nested_values = out[k]
            out[k] = text(nested_key, lang, **nested_values)
    return pick(TEXTS, key, lang).format(**out)


# --- sending -------------------------------------------------------------------------------


def _claim(order_id, user_id, dedup):
    """True the first time this event is seen for this robot (one notice per event)."""
    try:
        return cache.add(f"step-alert:{order_id}:{user_id}:{dedup}", 1, timeout=DEDUP_TTL)
    except Exception as e:  # without the cache, better a repeated notice than none
        logger.warning(f"step alert dedup unavailable: {e}")
        return True


def tell(order, user, key, action=False, dedup=None, **values):
    """Sends one notice to a trader (from a celery task: Telegram and webhooks block)."""
    if user is None:
        return False
    if not _claim(order.id, user.id, dedup or key):
        return False
    from api.notifications import Notifications

    lang = lang_of(user.robot)
    if "sats" in values and not isinstance(values["sats"], str):
        values["sats"] = fmt_sats(values["sats"], lang)
    if "trade" in values and not isinstance(values["trade"], str):
        values["trade"] = fmt_sats(values["trade"], lang)
    try:
        title = text(key, lang, n=order.id, **values)
    except Exception as e:
        logger.error(f"Order {order.id}: step alert {key} not built: {e}")
        return False
    notifications = Notifications()
    notifications.send_message(
        order, user.robot, title, notifications.order_url(order), event_type="order_step", action=action
    )
    return True


def queue(order, user, key, action=False, dedup=None, countdown=0, **values):
    """From code that must not block (requests, the follow_invoices loop): the notice is sent
    by a celery worker. The event is claimed here so nothing is queued twice."""
    if user is None:
        return False
    dedup = dedup or key
    if not _claim(order.id, user.id, f"q:{dedup}"):
        return False
    from api.tasks import send_step_alert

    try:
        send_step_alert.apply_async(
            kwargs=dict(order_id=order.id, user_id=user.id, key=key, action=action, dedup=dedup, values=values),
            countdown=countdown,
        )
    except Exception as e:
        logger.warning(f"Order {order.id}: step alert {key} not queued: {e}")
        return False
    return True


# --- who is who ----------------------------------------------------------------------------


def _user(user_id):
    if not user_id:
        return None
    from django.contrib.auth.models import User

    return User.objects.filter(id=user_id).select_related("robot").first()


def roles(order, taker=None):
    """{'maker', 'taker', 'buyer', 'seller'} -> User (taker may be gone: pass it)."""
    from api.models import Order

    maker = order.maker
    taker = taker if taker is not None else order.taker
    seller, buyer = (maker, taker) if order.type == Order.Types.SELL else (taker, maker)
    return {"maker": maker, "taker": taker, "buyer": buyer, "seller": seller}


def role_name(order, user):
    from api.models import Order

    is_maker = user is not None and order.maker_id == user.id
    maker_sells = order.type == Order.Types.SELL
    return "seller" if is_maker == maker_sells else "buyer"


def _is_onchain(order):
    from api.models import Order

    return order.escrow_type == Order.EscrowTypes.ONCHAIN


def _ln_role(order, role=None):
    """True when the escrow and the payout of this order run over Lightning. An onchain order
    keeps an onchain escrow and payout even when the taker locked its bond with Lightning
    (hybrid A); when a Lightning seller takes it (hybrid B) it becomes a Lightning order."""
    return not _is_onchain(order)


def _escrow_sats(order):
    try:
        return int(order.trade_escrow.num_satoshis) if order.trade_escrow else int(order.last_satoshis or 0)
    except Exception:
        return int(order.last_satoshis or 0)


def _payout_sats(order):
    try:
        if order.payout is not None:
            return int(order.payout.num_satoshis)
        if order.payout_tx is not None and order.payout_tx.sent_satoshis:
            return int(order.payout_tx.sent_satoshis)
    except Exception:
        pass
    try:
        from api.logics import Logics

        return int(Logics.payout_amount(order, order.maker if role_name(order, order.maker) == "buyer" else order.taker)[1]["invoice_amount"])
    except Exception:
        return int(order.last_satoshis or 0)


# --- order status steps --------------------------------------------------------------------


def _dispute_outcome(order):
    try:
        from api.escrow import flow

        return flow.dispute_outcome(flow.escrow_of(order))
    except Exception:
        return ""


def on_status(order, old, new, taker_id=None):
    """The notices of one order status change (from the order_status_changed signal)."""
    from api.models import Order

    S = Order.Status
    taker = order.taker or _user(taker_id)
    r = roles(order, taker)
    onchain = _is_onchain(order)
    step = f"st:{old}>{new}:{int(order.expires_at.timestamp()) if order.expires_at else 0}"
    left = left_until(order) or "?"

    def tell_(who, key, action=False, **values):
        return tell(order, r[who], key, action=action, dedup=f"{step}:{key}", **values)

    if new == S.PUB:
        if old == S.WFB:
            tell_("maker", "published")
        elif old in (S.TAK, S.WF2, S.WFE, S.WFI):
            tell_("maker", "republished")
            if taker is not None and taker.id != order.maker_id:
                tell(order, taker, "taker_dropped", dedup=f"{step}:taker_dropped")
        return

    if new == S.TAK:
        tell_("maker", "taken_ln_waiting")
        return

    if new == S.WF2:
        nexts = {}
        for role in ("buyer", "seller"):
            if _ln_role(order, role):
                if role == "seller":
                    nexts[role] = ("next_escrow_ln", {"sats": fmt_sats(_escrow_sats(order), lang_of(r[role].robot)), "left": left})
                else:
                    nexts[role] = ("next_payout_ln", {"sats": fmt_sats(_payout_sats(order), lang_of(r[role].robot)), "left": left})
            else:
                nexts[role] = (f"next_address_{role}", {"left": left})
        for who in ("maker", "taker"):
            role = role_name(order, r[who])
            tell_(who, f"contract_{who}", action=True, next=nexts[role])
        return

    if new == S.WFE and old == S.WF2:
        # onchain: the funding notice (with the amount owed) comes from watch_onchain
        if _ln_role(order, "seller"):
            tell_("seller", "seller_lock_now", action=True, sats=_escrow_sats(order), left=left)
        return

    if new == S.WFI:
        if _ln_role(order, "buyer"):
            tell_("buyer", "buyer_payout_now", action=True, sats=_payout_sats(order), left=left)
        return

    if new == S.CHA:
        if old == S.FSE:
            tell_("seller", "fiat_sent_undone")
            return
        if old in (S.WF2, S.WFE, S.WFI):
            how = "how_onchain" if onchain else "how_ln"
            tell_("buyer", "chat_buyer", action=True, how=(how, {}))
            tell_("seller", "chat_seller", how=(how, {}))
        return

    if new == S.FSE:
        tell_("seller", "fiat_sent", action=True)
        return

    if new == S.DIS:
        for who in ("maker", "taker"):
            tell_(who, "dispute_opened", action=True, left=left)
        return

    if new == S.WFR:
        for who in ("maker", "taker"):
            tell_(who, "dispute_wait")
        return

    if new in (S.MLD, S.TLD):
        loser, winner = ("maker", "taker") if new == S.MLD else ("taker", "maker")
        outcome = _dispute_outcome(order) if onchain else ""
        if outcome.startswith("penalize_"):  # 0.9.10: no trade, the guilty side penalized
            guilty = role_name(order, r[loser])
            innocent = role_name(order, r[winner])
            tell_(loser, f"dispute_penalized_{guilty}", action=guilty == "seller")
            tell_(winner, f"dispute_other_penalized_{innocent}", action=innocent == "seller")
            return
        tell_(loser, "dispute_lost")
        tell_(winner, f"dispute_won_{role_name(order, r[winner])}")
        return

    if new == S.PAY:
        if onchain and old in (S.DIS, S.WFR):  # 0.9.10: a dispute solved as a completed trade
            tell_("buyer", "dispute_completed_buyer", action=True)
            tell_("seller", "dispute_completed_seller")
            return
        if order.payout is not None or not onchain:
            tell_("buyer", "paying_ln", sats=_payout_sats(order))
        else:
            tell_("buyer", "releasing_onchain", action=True)
        return

    if new == S.SUC:
        summary = _onchain_summary(order, r["buyer"]) if onchain else None
        if summary:
            tell_("buyer", "success_buyer", sats=summary["received_sats"])
        else:
            tell_("buyer", "success_buyer", sats=_payout_sats(order))
        seller_summary = _onchain_summary(order, r["seller"]) if onchain else None
        if seller_summary and seller_summary.get("received_sats"):
            tell_("seller", "success_seller_onchain", sats=seller_summary["received_sats"])
        else:
            tell_("seller", "success_seller")
        return

    if new == S.FAI:
        tell_("buyer", "payout_failed", action=True)
        return

    if new == S.CCA:
        detail = ("collab_detail_onchain", {}) if onchain else ("collab_detail_ln", {})
        for who in ("maker", "taker"):
            tell_(who, "collab_done", detail=detail)
        return

    if new == S.UCA:
        if old in (S.WF2, S.WFE, S.WFI) and taker is not None:
            tell_("taker", "maker_cancelled")
        return

    if new == S.EXP:
        _on_expired(order, old, r, tell_)
        return


def _fault_what(order, role):
    if role == "seller":
        return "escrow_ln" if _ln_role(order, "seller") else "escrow_onchain"
    return "payout_ln" if _ln_role(order, "buyer") else "address"


def _on_expired(order, old, r, tell_):
    from api.models import Order

    S = Order.Status
    if old == S.WFB:
        tell_("maker", "exp_nobond")
        return
    if old in (S.PUB, S.PAU):
        tell_("maker", "exp_untaken")
        return
    if old not in (S.WF2, S.WFE, S.WFI):
        return
    reason = order.expiry_reason
    R = Order.ExpiryReasons
    if reason == R.NESINV or (reason is None and old == S.WF2 and not _is_onchain(order)):
        for who in ("maker", "taker"):
            tell_(who, "exp_both")
        return
    if reason == R.NESCRO or (reason is None and old == S.WFE):
        fault = "seller"
    elif reason == R.NINVOI or (reason is None and old == S.WFI):
        fault = "buyer"
    else:
        return
    victim = "buyer" if fault == "seller" else "seller"
    tell_(fault, "exp_fault", what=("WHAT", _fault_what(order, fault)))
    tell_(victim, "exp_victim", peer=("PEER", fault))


def _onchain_summary(order, user):
    if user is None:
        return None
    try:
        from api.escrow import flow

        return flow.trade_summary(order, user)
    except Exception:
        return None


# --- other events --------------------------------------------------------------------------


def on_take_started(order, take_order):
    """An onchain take: the taker has hours to lock its bond, the maker waits for it."""
    if take_order.lightning or not _is_onchain(order):
        return
    left = fmt_left((take_order.expires_at - timezone.now()).total_seconds())
    dedup = f"take:{take_order.id}"
    queue(order, take_order.taker, "take_started", action=True, dedup=dedup, left=left)
    queue(order, order.maker, "being_taken", dedup=dedup, left=left)


def on_collab_asked(order, asker):
    peer = order.taker if order.maker_id == asker.id else order.maker
    queue(order, peer, "collab_asked", action=True, dedup=f"collab:{order.status}",
          peer=("PEER", role_name(order, asker)))


def on_statement(order, user):
    """A dispute statement arrived: the other trader is told if it has not sent its own."""
    is_maker = order.maker_id == user.id
    peer = order.taker if is_maker else order.maker
    peer_statement = order.taker_statement if is_maker else order.maker_statement
    if peer is None or peer_statement not in (None, ""):
        return
    queue(order, peer, "statement_peer", action=True, dedup="statement_peer",
          peer=("PEER", role_name(order, user)), left=left_until(order) or "?")


def on_chat_message(order, chat_message):
    receiver = chat_message.receiver
    who = ("PEER", role_name(order, chat_message.sender))
    tell(order, receiver, "chat_message", dedup=f"chat:{chat_message.id}", who=who)


def on_reward(robot, amount, order_id, delay):
    from api.models import Order

    order = Order.objects.filter(id=order_id).first()
    if order is None:
        return
    when = ("reward_now", {}) if delay <= 0 else ("reward_later", {"wait": fmt_left(delay)})
    queue(order, robot.user, "reward", dedup=f"reward:{amount}", sats=int(amount), when=when)


# --- onchain sub-steps (from the follow_invoices loop) ---------------------------------------


def watch_onchain(order, escrow):
    """Funding owed, funding confirmed, deposit in the mempool, release in the mempool.
    Looked at every WATCH_EVERY seconds at most; each notice once."""
    from api.models import OnchainEscrow, Order

    if escrow is None:
        return
    try:
        if not cache.add(f"step-alert-watch:{order.id}", 1, timeout=WATCH_EVERY):
            return
    except Exception:
        pass
    from api.escrow import flow

    S = OnchainEscrow.Status
    r = roles(order)
    if escrow.status == S.KEYS and order.status == Order.Status.WFE:
        for role in ("seller", "buyer"):
            if r[role] is None:
                continue
            who, what = flow.waiting_for(order, escrow, role)
            if who == "you" and what == "funding":
                owed, _ = flow.recommended_funding(order, escrow, role)
                key = "fund_now" if role == "seller" else "fee_now"
                queue(order, r[role], key, action=True, dedup=f"fund:{escrow.id}", sats=int(owed),
                      left=left_until(order) or "?")
        for role in ("seller", "buyer"):
            if r[role] is None or cache.get(f"step-alert:{order.id}:{r[role].id}:q:funded:{escrow.id}"):
                continue
            owed, _ = flow.recommended_funding(order, escrow, role)
            if role == "buyer" and (not owed or not cache.get(f"step-alert:{order.id}:{r[role].id}:q:fund:{escrow.id}")):
                continue  # the buyer only funds its fee in some plans: tell it only if it had to
            confirmed = sum(u.value for u in flow.service.funding_utxos(escrow, role))
            if owed and confirmed >= owed:
                queue(order, r[role], "funding_confirmed", action=True, dedup=f"funded:{escrow.id}")
    if escrow.status == S.MEMPOOL and escrow.funding_txid:
        for role in ("seller", "buyer"):
            queue(order, r[role], "deposit_mempool", dedup=f"deposit:{escrow.funding_txid}")
    if order.status == Order.Status.PAY and escrow.release_txid and escrow.status in (S.RELEASING, S.LOCKED, S.RELEASED):
        if escrow.status == S.RELEASED:
            return  # confirmed already: the "finished" notice says it all
        for role in ("buyer", "seller"):
            summary = _onchain_summary(order, r[role])
            if not summary:
                continue
            if role == "buyer":
                queue(order, r[role], "release_mempool_buyer", dedup=f"release:{escrow.release_txid}",
                      sats=int(summary["received_sats"]))
            else:
                queue(order, r[role], "release_mempool_seller", dedup=f"release:{escrow.release_txid}",
                      sats=int(summary["received_sats"]), trade=int(summary.get("trade_sats") or 0))


def onchain_end_check(order):
    """After an onchain (or hybrid) order ended: bonds free to take back, funding leftovers."""
    from api.models import OnchainBond

    for bond in OnchainBond.objects.filter(order=order, status=OnchainBond.Status.RETURNABLE).select_related("owner__robot"):
        tell(order, bond.owner, "bond_back", action=True, dedup=f"bond_back:{bond.id}", sats=int(bond.value or 0))
    try:
        from api.escrow import flow

        escrow = flow.escrow_of(order)
        if escrow is None:
            return
        r = roles(order)
        for role in ("seller", "buyer"):
            if r[role] is None:
                continue
            utxos = flow.funding_leftover(order, escrow, role)
            sats = sum(u.value for u in utxos)
            if sats > 0 and not escrow.funding_signatures.get(f"sweep_{role}_txid"):
                tell(order, r[role], "leftover", action=True, dedup=f"leftover:{escrow.id}:{sats}", sats=sats)
    except Exception as e:
        logger.warning(f"Order {order.id}: leftover check failed: {e}")


# --- deadlines (celery beat, every minute) --------------------------------------------------


def _step_window(order):
    """How long the current step lasts in total (seconds), to skip reminders on short steps."""
    from api.models import Order

    if _is_onchain(order) and order.status in (Order.Status.WF2, Order.Status.WFE):
        return config("ESCROW_FUNDING_SECONDS", cast=int, default=3 * 3600)
    try:
        return order.t_to_expire(order.status)
    except Exception:
        return 0


def deadline_targets(order):
    """[(user, what, consequence)] for the robots that must act before order.expires_at."""
    from api.models import Order

    S = Order.Status
    r = roles(order)
    st = order.status
    if st == S.WFB:
        return [(r["maker"], "bond_maker", "nothing")]
    if st in (S.WF2, S.WFE, S.WFI) and _is_onchain(order):
        try:
            from api.escrow import flow

            escrow = flow.escrow_of(order)
            out = []
            for role in ("seller", "buyer"):
                if r[role] is None:
                    continue
                if escrow is None:
                    if role == "seller":
                        out.append((r[role], "address", "bond"))
                    continue
                who, what = flow.waiting_for(order, escrow, role)
                if who != "you":
                    continue
                if what in ("keys", "address"):
                    out.append((r[role], "address", "bond"))
                elif what == "funding":
                    out.append((r[role], "escrow_onchain" if role == "seller" else "fee_onchain", "bond"))
                elif what and what.endswith("_signature"):
                    out.append((r[role], "sign", "bond"))
            return out
        except Exception as e:
            logger.warning(f"Order {order.id}: onchain deadline check failed: {e}")
            return []
    if st == S.WF2:
        return [(r["seller"], "escrow_ln", "bond"), (r["buyer"], "payout_ln", "bond")]
    if st == S.WFE:
        return [(r["seller"], "escrow_ln", "bond")]
    if st == S.WFI:
        return [(r["buyer"], "payout_ln", "bond")]
    if st == S.CHA:
        return [(r["buyer"], "fiat_sent", "dispute")]
    if st == S.FSE:
        return [(r["seller"], "fiat_received", "dispute")]
    if st == S.DIS:
        out = []
        if order.maker_statement in (None, ""):
            out.append((r["maker"], "statement", "statement"))
        if order.taker_statement in (None, ""):
            out.append((r["taker"], "statement", "statement"))
        return out
    return []


def deadline_mark(remaining, window):
    """Which reminder is due (30 or 10 minutes) for a step with [remaining] seconds left that
    lasts [window] seconds in total; None when none."""
    if remaining <= 0:
        return None
    for mark in sorted(DEADLINE_MARKS):  # the closest first: after a gap, only the 10-minute one
        if remaining <= mark * 60:
            return mark if window >= DEADLINE_MIN_WINDOW[mark] else None
    return None


def sweep(now=None):
    """Deadline reminders, onchain takes about to time out, rewards that became withdrawable and
    bonds that became free to take back."""
    from api.models import OnchainBond, Order, TakeOrder

    now = now or timezone.now()
    horizon = now + timedelta(minutes=max(DEADLINE_MARKS) + 1)
    S = Order.Status
    orders = Order.objects.filter(
        status__in=[S.WFB, S.WF2, S.WFE, S.WFI, S.CHA, S.FSE, S.DIS], expires_at__gt=now, expires_at__lte=horizon
    ).select_related("maker__robot", "taker__robot")
    for order in orders:
        remaining = (order.expires_at - now).total_seconds()
        mark = deadline_mark(remaining, _step_window(order))
        if mark is None:
            continue
        for user, what, cons in deadline_targets(order):
            if user is None:
                continue
            tell(order, user, "deadline", action=True,
                 dedup=f"deadline:{order.status}:{int(order.expires_at.timestamp())}:{mark}:{what}",
                 left=fmt_left(remaining), what=("WHAT", what), cons=("CONSEQUENCE", cons))
    # onchain takes: hours to lock the taker bond
    for take in TakeOrder.objects.filter(expires_at__gt=now, expires_at__lte=horizon, lightning=False).select_related("order", "taker__robot"):
        order = take.order
        if not _is_onchain(order) or order.status != S.PUB:
            continue
        bond = OnchainBond.objects.filter(order=order, role="taker", take_order=take).order_by("-created_at").first()
        if bond is not None and bond.status not in (OnchainBond.Status.FUNDING,):
            continue
        remaining = (take.expires_at - now).total_seconds()
        mark = deadline_mark(remaining, order.t_to_expire(S.TAK))
        if mark is None:
            continue
        tell(order, take.taker, "deadline", action=True, dedup=f"deadline:take:{take.id}:{mark}",
             left=fmt_left(remaining), what=("WHAT", "bond_taker"), cons=("CONSEQUENCE", "take"))
    _sweep_rewards(now)
    _sweep_bonds(now)


def _sweep_rewards(now):
    try:
        from django.db import connection

        from api.logics import LEDGER_TABLE, ensure_ledger

        ensure_ledger()
        with connection.cursor() as c:
            c.execute(
                f"""SELECT robot_id, order_id, amount, fingerprint FROM {LEDGER_TABLE}
                    WHERE amount > 0 AND order_id IS NOT NULL AND kind NOT LIKE 'withdraw%%'
                    AND available_at > created_at + interval '1 minute'
                    AND available_at <= %s AND available_at > %s""",
                [now, now - timedelta(minutes=15)],
            )
            rows = c.fetchall()
    except Exception as e:
        logger.warning(f"rewards sweep failed: {e}")
        return
    from api.models import Order, Robot

    for robot_id, order_id, amount, fingerprint in rows:
        robot = Robot.objects.filter(id=robot_id).select_related("user").first()
        order = Order.objects.filter(id=order_id).first()
        if robot and order:
            tell(order, robot.user, "reward_ready", dedup=f"reward_ready:{fingerprint}", sats=int(amount))


def _sweep_bonds(now):
    """Bonds that became free to take back after the order ended (a penalty or a late
    confirmation can free them later than the end of the order)."""
    from api.models import OnchainBond

    try:
        bonds = OnchainBond.objects.filter(status=OnchainBond.Status.RETURNABLE)
        if hasattr(OnchainBond, "updated_at"):
            bonds = bonds.filter(updated_at__gt=now - timedelta(minutes=30))
        for bond in bonds.select_related("order", "owner__robot")[:200]:
            tell(bond.order, bond.owner, "bond_back", action=True, dedup=f"bond_back:{bond.id}", sats=int(bond.value or 0))
    except Exception as e:
        logger.warning(f"bonds sweep failed: {e}")


# --- signal --------------------------------------------------------------------------------


def on_status_changed(sender, order, old_status, new_status, **kwargs):
    """order_status_changed receiver: the notices are built by a celery worker a few seconds
    later (after the rest of the transition, e.g. the expiry reason, is saved)."""
    from django.db import transaction

    from api.tasks import send_notification

    taker_id = getattr(order, "_prev_taker_id", None) or order.taker_id
    message = f"step:{old_status}:{new_status}:{taker_id or ''}"

    def enqueue():
        try:
            send_notification.apply_async(kwargs=dict(order_id=order.id, message=message), countdown=3)
        except Exception as e:
            logger.warning(f"Order {order.id}: step notice not queued: {e}")

    transaction.on_commit(enqueue)
    from api.models import Order

    ended = (Order.Status.UCA, Order.Status.EXP, Order.Status.CCA, Order.Status.SUC, Order.Status.MLD, Order.Status.TLD)
    if new_status in ended and (
        _is_onchain(order)
        or getattr(order, "taker_lightning", False)
        or getattr(order, "maker_lightning", False)
        or str(getattr(order.taker_bond, "payment_hash", "") or "").startswith("onchain-bond-")  # mixta C
    ):
        transaction.on_commit(
            lambda: send_notification.apply_async(kwargs=dict(order_id=order.id, message="onchain_end"), countdown=120)
        )
