"""Ajustes del panel de administración para el trabajo del coordinador.

RoboSats registra sus modelos en `api/admin.py`, que vive dentro de la imagen y
no queremos tocar. Esta aplicación se carga después y reconfigura lo ya
registrado: qué columnas se ven, por qué se puede filtrar y en qué orden llegan
las cosas. Si mañana cambia el admin original, aquí solo se pierde el retoque,
no se rompe nada.
"""

from django.apps import AppConfig


class RoblosatsAdminConfig(AppConfig):
    name = "roblosats_admin"
    verbose_name = "Roblosats coordinator panel"

    def ready(self):
        from django.contrib import admin

        from api.models import LNPayment, Order, Robot

        registry = admin.site._registry

        if Order in registry:
            oa = registry[Order]
            # Lo primero que se mira de una orden: en qué estado está, de cuánto es,
            # si hay disputa y cuánto lleva. El resto son enlaces internos que
            # estorban en la lista y siguen estando dentro de la ficha.
            oa.list_display = (
                "id",
                "type",
                "status",
                "amt",
                "currency_link",
                "t0_satoshis",
                "is_disputed",
                "is_fiat_sent",
                "maker_link",
                "taker_link",
                "created_at",
                "expires_at",
            )
            oa.list_filter = (
                "status",
                "is_disputed",
                "is_fiat_sent",
                "type",
                "currency",
                "is_swap",
            )
            oa.ordering = ("-created_at",)
            oa.list_per_page = 50
            oa.date_hierarchy = "created_at"

            _dispute_panel(oa)

        if LNPayment in registry:
            pa = registry[LNPayment]
            pa.ordering = ("-created_at",)
            pa.list_per_page = 50

        if Robot in registry:
            ra = registry[Robot]
            ra.list_per_page = 50


def _dispute_panel(oa):
    from django.urls import path, reverse
    from django.utils.html import format_html

    from roblosats_admin import disputes
    from roblosats_admin.i18n import _lazy
    from roblosats_admin.messaging import message_parties

    if getattr(oa, "_roblosats_dispute_panel", False):
        return
    oa._roblosats_dispute_panel = True

    original_urls = oa.get_urls

    def get_urls():
        wrap = oa.admin_site.admin_view
        own = [
            path(
                "disputes/",
                wrap(lambda request: disputes.disputes_view(oa, request)),
                name="api_order_disputes",
            ),
            path(
                "<int:order_id>/dispute/",
                wrap(
                    lambda request, order_id: disputes.dispute_view(
                        oa, request, order_id
                    )
                ),
                name="api_order_dispute",
            ),
        ]
        return own + original_urls()

    oa.get_urls = get_urls

    def dispute_link(obj):
        if not obj.is_disputed:
            return ""
        return format_html(
            '<a href="{}" style="font-weight:600;color:rgb(234 88 12)">⚖️ {}</a>',
            reverse("admin:api_order_dispute", args=[obj.pk]),
            _lazy("Dispute"),
        )

    dispute_link.short_description = _lazy("Dispute")
    columns = list(oa.list_display)
    position = columns.index("is_disputed") + 1 if "is_disputed" in columns else 1
    columns.insert(position, dispute_link)
    oa.list_display = tuple(columns)

    oa.actions = [*oa.actions, message_parties]
