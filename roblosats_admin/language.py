"""English and Spanish labels for the Roblosats coordinator admin."""

from django.conf import settings

LANGUAGES = ("es", "en")

TRANSLATIONS = {
    "Coordinator Dashboard": "Panel del coordinador",
    "BUY": "COMPRA",
    "SELL": "VENTA",
    "Waiting for maker bond": "Esperando fianza del creador",
    "Public": "Pública",
    "Paused": "Pausada",
    "Waiting for taker bond": "Esperando fianza del tomador",
    "Cancelled": "Cancelada",
    "Waiting for trade collateral and buyer invoice": "Esperando colateral e invoice del comprador",
    "Waiting only for seller trade collateral": "Esperando colateral del vendedor",
    "Waiting only for buyer invoice": "Esperando invoice del comprador",
    "Sending fiat - In chatroom": "Enviando fiat - En el chat",
    "Fiat sent - In chatroom": "Fiat enviado - En el chat",
    "In dispute": "En disputa",
    "Collaboratively cancelled": "Cancelada de mutuo acuerdo",
    "Sending satoshis to buyer": "Enviando satoshis al comprador",
    "Successful trade": "Operación completada",
    "Failed lightning network routing": "Error de enrutamiento Lightning",
    "Wait for dispute resolution": "Esperando resolución de disputa",
    "Maker lost dispute": "El creador perdió la disputa",
    "Taker lost dispute": "El tomador perdió la disputa",

    "No.": "N.º",
    "Type": "Tipo",
    "Amount": "Importe",
    "Status": "Estado",
    "Created": "Desde",
    "Expires": "Hasta",
    "dispute": "disputa",
    "disputes": "disputas",
    "dispute_plural_suffix": "s",
    "{count} dispute awaiting resolution":
        "{count} disputa esperando resolución",
    "{count} disputes awaiting resolution":
        "{count} disputas esperando resolución",
    "awaiting resolution": "esperando resolución",
    "node": "nodo",
    "channels": "canales",
    "local sats": "sats locales",
    "remote sats": "sats remotos",

    "Coordinator": "Coordinador",
    "Home": "Inicio",
    "Active Orders": "Órdenes en curso",
    "Public Orders": "En el libro (públicas)",
    "Paying the Buyer": "Pagando al comprador",
    "Completed": "Completadas",
    "All Orders": "Todas las órdenes",
    "Disputes": "Disputas",
    "Open": "Abiertas",
    "Awaiting Resolution": "Esperando resolución",
    "Full History": "Historial completo",
    "Payments": "Dinero",
    "Lightning Payments": "Pagos Lightning",
    "On-chain Payments": "Pagos onchain",
    "People and Market": "Gente y mercado",
    "Robots": "Robots",
    "Users": "Usuarios",
    "Currencies": "Divisas",
    "Market Prices": "Precios de mercado",
    "Active": "En curso",
    "Order Book": "En el libro",
    "Canceled": "Canceladas",
    "Collateral, chat, or fiat sent": "colateral, chat o fiat enviado",
    "Waiting for takers": "esperando quien las tome",
    "Open and awaiting resolution": "abiertas y por resolver",
    "History": "histórico",
    "Expired": "Vencida",
    "{time} remaining": "quedan {time}",
    "Canceled or mutually canceled · {count} expired":
        "anuladas y de mutuo acuerdo · {count} expiradas",
    "Your Attention Required": "Requiere tu decisión",
    "No active orders at the moment.": "Ninguna operación en curso ahora mismo.",
    "Return to coordinator dashboard": "Volver al panel del coordinador",
}

TRANSLATIONS.update({'Close public order': 'Cerrar orden pública', 'Solve dispute: maker wins': 'Resolver disputa: gana el creador', 'Solve dispute: taker wins': 'Resolver disputa: gana el tomador', 'Solve dispute: return everything': 'Resolver disputa: devolver todo', 'Solve dispute: successful trade': 'Resolver disputa: operación completada', 'Compute median trade completion time': 'Calcular tiempo mediano de operaciones', 'Regular invoice': 'Factura normal', 'hold invoice': 'Factura retenida', 'Keysend': 'Keysend', 'Maker bond': 'Fianza del creador', 'Taker bond': 'Fianza del tomador', 'Trade escrow': 'Depósito de garantía', 'Payment to buyer': 'Pago al comprador', 'Withdraw rewards': 'Retirar recompensas', 'Devfund donation': 'Donación al fondo de desarrollo', 'Generated': 'Generado', 'Locked': 'Bloqueado', 'Settled': 'Liquidado', 'Returned': 'Devuelto', 'Valid': 'Válido', 'In flight': 'En curso', 'Succeeded': 'Completado', 'Routing failed': 'Error de enrutamiento', 'In mempool': 'En mempool', 'Confirmed': 'Confirmado', 'Queued': 'En cola', "Payment isn't failed (yet)": 'El pago todavía no ha fallado', 'There are more routes to try, but the payment timeout was exceeded.': 'Quedan rutas por probar, pero se agotó el tiempo del pago.', 'All possible routes were tried and failed permanently. Or there were no routes to the destination at all.': 'Todas las rutas fallaron o no había rutas disponibles.', 'A non-recoverable error has occurred.': 'Se produjo un error irrecuperable.', 'Payment details are incorrect (unknown hash, invalid amount or invalid final CLTV delta).': 'Los detalles del pago son incorrectos.', "Insufficient unlocked balance in RoboSats' node.": 'Saldo desbloqueado insuficiente en el nodo RoboSats.'})



def format_remaining(time_text, language):
    """Format the remaining time in English or Spanish."""
    if language == "es":
        verb = "queda" if time_text == "1 min" else "quedan"
        return f"{verb} {time_text}"
    return f"{time_text} remaining"


def format_expired(count, language):
    """Format expired-order counts with correct pluralization."""
    if language == "es":
        noun = "expirada" if count == 1 else "expiradas"
        return f"anuladas y de mutuo acuerdo · {count} {noun}"

    noun = "expired order" if count == 1 else "expired orders"
    return f"Canceled or mutually canceled · {count} {noun}"


def get_language(request):
    """Return the coordinator's selected language, defaulting to Spanish."""
    language = request.COOKIES.get("roblosats_admin_language", "es")
    return language if language in LANGUAGES else "es"


def translate(text, language):
    """Translate a coordinator label to the selected language."""
    if language == "es":
        return TRANSLATIONS.get(text, text)
    return text


def translate_navigation(request):
    """Return sidebar labels translated for this request."""
    import copy

    navigation = copy.deepcopy(settings.ROBLOSATS_ADMIN_NAVIGATION)
    language = get_language(request)

    for group in navigation:
        group["title"] = translate(group["title"], language)
        for item in group.get("items", []):
            item["title"] = translate(item["title"], language)

    return navigation


def coordinator_subheader(request):
    """Return the admin subheader in the selected language."""
    return translate("Coordinator Dashboard", get_language(request))
