# ruff: noqa: F821

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

# panel language (dispute panel and admin chrome): admin requests only
MIDDLEWARE = list(MIDDLEWARE) + ["roblosats_admin.middleware.AdminLocaleMiddleware"]

# carpeta de plantillas del panel Roblosats (montada en el contenedor)
TEMPLATES[0]["DIRS"] = list(TEMPLATES[0]["DIRS"]) + ["/usr/src/robosats/roblosats_templates"]

# Django Unfold admin theme
from django.utils.translation import pgettext_lazy as _roblosats_pgettext_lazy  # noqa: E402


def _rl(message):
    return _roblosats_pgettext_lazy("roblosats-admin", message)


def _count_orders(*status_values):
    def _callback(request):
        from api.models import Order

        n = Order.objects.filter(status__in=status_values).count()
        return str(n) if n else None

    return _callback


EN_CURSO = [6, 7, 8, 9, 10]
EN_DISPUTA = [11, 16]
ADMIN_ROOT = "/coordinator/"

UNFOLD = {
    "SITE_TITLE": f"{config('COORDINATOR_ALIAS', cast=str, default='Roblosats')} Admin",
    "SITE_HEADER": config("COORDINATOR_ALIAS", cast=str, default="Roblosats"),
    "SITE_SUBHEADER": _rl("Coordinator panel"),
    "SITE_SYMBOL": "local_fire_department",
    "DASHBOARD_CALLBACK": "roblosats_admin.dashboard.dashboard",
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
                "title": _rl("Coordinator"),
                "separator": True,
                "collapsible": False,
                "items": [
                    {"title": _rl("Home"), "icon": "home", "link": ADMIN_ROOT},
                    {
                        "title": _rl("Orders in progress"),
                        "icon": "pending_actions",
                        "link": ADMIN_ROOT + "api/order/?status__in=6%2C7%2C8%2C9%2C10",
                        "badge": _count_orders(*EN_CURSO),
                    },
                    {
                        "title": _rl("In the book (public)"),
                        "icon": "storefront",
                        "link": ADMIN_ROOT + "api/order/?status__exact=1",
                        "badge": _count_orders(1),
                    },
                    {
                        "title": _rl("Paying the buyer"),
                        "icon": "bolt",
                        "link": ADMIN_ROOT + "api/order/?status__exact=13",
                        "badge": _count_orders(13),
                    },
                    {
                        "title": _rl("Failed payouts"),
                        "icon": "flash_off",
                        "link": ADMIN_ROOT + "api/order/failed-payouts/",
                        "badge": _count_orders(15),
                    },
                    {
                        "title": _rl("Completed"),
                        "icon": "task_alt",
                        "link": ADMIN_ROOT + "api/order/?status__exact=14",
                    },
                    {
                        "title": _rl("All orders"),
                        "icon": "list_alt",
                        "link": ADMIN_ROOT + "api/order/",
                    },
                    {
                        "title": _rl("Telegram alerts"),
                        "icon": "notifications",
                        "link": ADMIN_ROOT + "roblosats_admin/telegramalerts/",
                        "permission": "roblosats_admin.dashboard.can_configure_alerts",
                    },
                ],
            },
            {
                "title": _rl("Disputes"),
                "separator": True,
                "collapsible": False,
                "items": [
                    {
                        "title": _rl("Open"),
                        "icon": "gavel",
                        "link": ADMIN_ROOT + "api/order/disputes/?view=open",
                        "badge": _count_orders(*EN_DISPUTA),
                    },
                    {
                        "title": _rl("Waiting for your decision"),
                        "icon": "hourglass_top",
                        "link": ADMIN_ROOT + "api/order/disputes/?view=waiting",
                        "badge": _count_orders(16),
                    },
                    {
                        "title": _rl("History"),
                        "icon": "history",
                        "link": ADMIN_ROOT + "api/order/disputes/?view=history",
                    },
                ],
            },
            {
                "title": _rl("Money"),
                "separator": True,
                "collapsible": True,
                "items": [
                    {"title": _rl("Lightning payments"), "icon": "flash_on", "link": ADMIN_ROOT + "api/lnpayment/"},
                    {"title": _rl("On-chain payments"), "icon": "link", "link": ADMIN_ROOT + "api/onchainpayment/"},
                ],
            },
            {
                "title": _rl("People and market"),
                "separator": True,
                "collapsible": True,
                "items": [
                    {"title": _rl("Robots"), "icon": "smart_toy", "link": ADMIN_ROOT + "api/robot/"},
                    {"title": _rl("Users"), "icon": "person", "link": ADMIN_ROOT + "auth/user/"},
                    {"title": _rl("Currencies"), "icon": "payments", "link": ADMIN_ROOT + "api/currency/"},
                    {"title": _rl("Market prices"), "icon": "trending_up", "link": ADMIN_ROOT + "api/markettick/"},
                ],
            },
        ],
    },
}
