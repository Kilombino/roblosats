
#####################################################################
# === Roblosats: extensión del settings.py OFICIAL (append-only) =====
# Este bloque se AÑADE al final del settings.py de upstream en el build.
# Así heredamos SIEMPRE el settings.py oficial (y sus fixes) y solo
# mantenemos aquí lo específico de Roblosats. CSRF_TRUSTED_ORIGINS y
# CHANNEL_LAYERS ya los da upstream (por env) — no se tocan aquí.
#####################################################################

TIME_ZONE = config("ADMIN_TIME_ZONE", cast=str, default="UTC")  # the admin shows times in this zone

# unfold DEBE ir antes de django.contrib.admin; roblosats_admin al final.
_UNFOLD_APPS = ["unfold", "unfold.contrib.filters", "unfold.contrib.forms", "unfold.contrib.import_export"]
INSTALLED_APPS = _UNFOLD_APPS + [a for a in INSTALLED_APPS if a not in _UNFOLD_APPS and a != "roblosats_admin"] + ["roblosats_admin"]

# carpeta de plantillas del panel Roblosats (montada en el contenedor)
TEMPLATES[0]["DIRS"] = list(TEMPLATES[0]["DIRS"]) + ["/usr/src/robosats/roblosats_templates"]

# Django Unfold admin theme

def _count_orders(*status_values):
    """Cuenta órdenes en esos estados. Se llama al pintar el menú, no al arrancar."""
    def _callback(request):
        from api.models import Order

        n = Order.objects.filter(status__in=status_values).count()
        return str(n) if n else None

    return _callback


# Estados que le importan a quien lleva el coordinador (api/models/order.py)
EN_CURSO = [6, 7, 8, 9, 10]      # esperando colateral/factura, chat, fiat enviado
EN_DISPUTA = [11, 16]            # en disputa y esperando resolución
ADMIN_ROOT = "/coordinator/"

def roblosats_dashboard(request, context=None):
    """Datos de la pantalla de inicio del panel. Unfold pasa el contexto que ya trae
    y espera recibirlo de vuelta con lo nuestro añadido."""
    from django.urls import reverse
    from django.utils import timezone
    from api.models import Order

    ahora = timezone.now()

    def hora(momento):
        """Los datetime llegan en UTC; el coordinador los lee en su zona (ADMIN_TIME_ZONE)."""
        return timezone.localtime(momento).strftime("%d/%m %H:%M") if momento else ""

    def cuenta_atras(limite):
        """Lo que queda para que venza el plazo del estado actual de la orden.

        `expires_at` se reescribe en cada cambio de estado (api/logics.py), asi que
        siempre es la fecha limite de lo que la orden esta esperando ahora mismo.
        """
        if limite is None:
            return "", False
        segundos = int((limite - ahora).total_seconds())
        if segundos <= 0:
            return "vencido", True
        minutos, _ = divmod(segundos, 60)
        horas, minutos = divmod(minutos, 60)
        dias, horas = divmod(horas, 24)
        if dias:
            resto = f"{dias} d {horas} h"
        elif horas:
            resto = f"{horas} h {minutos} min"
        else:
            resto = f"{minutos} min"
        verbo = "queda" if resto.startswith("1 ") and " " not in resto[2:] else "quedan"
        return f"{verbo} {resto}", segundos < 1800

    def url_orden(pk):
        return reverse("admin:api_order_change", args=[pk])

    def lista(**filtros):
        query = "&".join(f"{k}={v}" for k, v in filtros.items())
        return f"{ADMIN_ROOT}api/order/?{query}"

    vivas = (
        Order.objects.filter(status__in=EN_CURSO)
        .select_related("currency")
        .order_by("-last_satoshis_time", "-created_at")[:10]
    )

    ordenes_vivas = []
    for o in vivas:
        queda, urgente = cuenta_atras(o.expires_at)
        ordenes_vivas.append(
            {
                "id": o.id,
                "tipo": o.get_type_display(),
                "importe": f"{o.amount or o.max_amount or '?'} {o.currency}",
                "estado": o.get_status_display(),
                "desde": hora(o.created_at),
                "hasta": hora(o.expires_at),
                "queda": queda,
                "urgente": urgente,
                "link": url_orden(o.id),
            }
        )

    saldo_local = saldo_remoto = "?"
    try:
        from api.lightning.lnd import LNDNode

        balance = LNDNode.channel_balance()
        saldo_local = f"{balance['local_balance']:,}".replace(",", ".")
        saldo_remoto = f"{balance['remote_balance']:,}".replace(",", ".")
    except Exception:
        pass  # si el nodo no responde, el panel se pinta igual

    en_curso = Order.objects.filter(status__in=EN_CURSO).count()
    disputas = Order.objects.filter(status=11).count()
    esperando = Order.objects.filter(status=16).count()
    publicas = Order.objects.filter(status=1).count()
    completadas = Order.objects.filter(status=14).count()
    # 4 es la que anula quien la hizo y 12 la que se deshace de mutuo acuerdo.
    # Las expiradas (5) no cuentan aqui: nadie las canceló, se les pasó el plazo
    # en el libro. Van aparte en el pie de la tarjeta.
    canceladas = Order.objects.filter(status__in=(4, 12)).count()
    expiradas = Order.objects.filter(status=5).count()

    datos = {
        "disputas_abiertas": disputas + esperando,
        "enlaces": {"disputas": lista(status__exact=11)},
        "tarjetas": [
            {"titulo": "En curso", "valor": en_curso, "link": lista(status__in="6%2C7%2C8%2C9%2C10"),
             "pie": "colateral, chat o fiat enviado"},
            {"titulo": "En el libro", "valor": publicas, "link": lista(status__exact=1),
             "pie": "esperando quien las tome"},
            {"titulo": "Disputas", "valor": disputas + esperando, "link": lista(status__exact=11),
             "pie": "abiertas y por resolver"},
            {"titulo": "Completadas", "valor": completadas, "link": lista(status__exact=14),
             "pie": "histórico"},
            {"titulo": "Canceladas", "valor": canceladas, "link": lista(status__in="4%2C12"),
             "pie": f"anuladas y de mutuo acuerdo · {expiradas} expirada{'s' if expiradas != 1 else ''}"},
        ],
        "ordenes_vivas": ordenes_vivas,
        "accesos": [
            {"titulo": "Todas las órdenes", "link": f"{ADMIN_ROOT}api/order/"},
            {"titulo": "Robots", "link": f"{ADMIN_ROOT}api/robot/"},
            {"titulo": "Pagos Lightning", "link": f"{ADMIN_ROOT}api/lnpayment/"},
            {"titulo": "Pagos onchain", "link": f"{ADMIN_ROOT}api/onchainpayment/"},
        ],
        "coordinador": {
            "alias": config("COORDINATOR_ALIAS", cast=str, default="Roblosats"),
            "version": f"{VERSION['major']}.{VERSION['minor']}.{VERSION['patch']}" if "VERSION" in globals() else "",
            "lnd": config("LND_GRPC_HOST", cast=str, default=""),
            "saldo_local": saldo_local,
            "saldo_remoto": saldo_remoto,
        },
    }
    if context is None:
        return datos
    context.update(datos)
    return context

UNFOLD = {
    "SITE_TITLE": f"{config('COORDINATOR_ALIAS', cast=str, default='Roblosats')} Admin",
    "SITE_HEADER": config("COORDINATOR_ALIAS", cast=str, default="Roblosats"),
    "SITE_SUBHEADER": "Panel del coordinador",
    "SITE_SYMBOL": "local_fire_department",
    "DASHBOARD_CALLBACK": "robosats.settings.roblosats_dashboard",
    "SHOW_HISTORY": True,
    "SHOW_VIEW_ON_SITE": True,
    "THEME": None,
    "COLORS": {
        "primary": {
            "50": "255 247 237",
            "100": "255 237 213",
            "200": "254 215 170",
            "300": "253 186 116",
            "400": "251 146 60",
            "500": "249 115 22",
            "600": "234 88 12",
            "700": "194 65 12",
            "800": "154 52 18",
            "900": "124 45 18",
            "950": "67 20 7",
        },
    },
    "SIDEBAR": {
        "show_search": True,
        "show_all_applications": True,
        "navigation": [
            {
                "title": "Coordinador",
                "separator": True,
                "collapsible": False,
                "items": [
                    {"title": "Inicio", "icon": "home", "link": ADMIN_ROOT},
                    {
                        "title": "Órdenes en curso",
                        "icon": "pending_actions",
                        "link": ADMIN_ROOT + "api/order/?status__in=6%2C7%2C8%2C9%2C10",
                        "badge": _count_orders(*EN_CURSO),
                    },
                    {
                        "title": "En el libro (públicas)",
                        "icon": "storefront",
                        "link": ADMIN_ROOT + "api/order/?status__exact=1",
                        "badge": _count_orders(1),
                    },
                    {
                        "title": "Pagando al comprador",
                        "icon": "bolt",
                        "link": ADMIN_ROOT + "api/order/?status__exact=13",
                        "badge": _count_orders(13),
                    },
                    {
                        "title": "Completadas",
                        "icon": "task_alt",
                        "link": ADMIN_ROOT + "api/order/?status__exact=14",
                    },
                    {
                        "title": "Todas las órdenes",
                        "icon": "list_alt",
                        "link": ADMIN_ROOT + "api/order/",
                    },
                ],
            },
            {
                "title": "Disputas",
                "separator": True,
                "collapsible": False,
                "items": [
                    {
                        "title": "Abiertas",
                        "icon": "gavel",
                        "link": ADMIN_ROOT + "api/order/?status__exact=11",
                        "badge": _count_orders(11),
                    },
                    {
                        "title": "Esperando resolución",
                        "icon": "hourglass_top",
                        "link": ADMIN_ROOT + "api/order/?status__exact=16",
                        "badge": _count_orders(16),
                    },
                    {
                        "title": "Historial completo",
                        "icon": "history",
                        "link": ADMIN_ROOT + "api/order/?is_disputed__exact=1",
                    },
                ],
            },
            {
                "title": "Dinero",
                "separator": True,
                "collapsible": True,
                "items": [
                    {"title": "Pagos Lightning", "icon": "flash_on", "link": ADMIN_ROOT + "api/lnpayment/"},
                    {"title": "Pagos onchain", "icon": "link", "link": ADMIN_ROOT + "api/onchainpayment/"},
                ],
            },
            {
                "title": "Gente y mercado",
                "separator": True,
                "collapsible": True,
                "items": [
                    {"title": "Robots", "icon": "smart_toy", "link": ADMIN_ROOT + "api/robot/"},
                    {"title": "Usuarios", "icon": "person", "link": ADMIN_ROOT + "auth/user/"},
                    {"title": "Divisas", "icon": "payments", "link": ADMIN_ROOT + "api/currency/"},
                    {"title": "Precios de mercado", "icon": "trending_up", "link": ADMIN_ROOT + "api/markettick/"},
                ],
            },
        ],
    },
}
