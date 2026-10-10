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
    verbose_name = "Ajustes del panel del coordinador Roblosats"

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
            oa.list_filter = ("status", "is_disputed", "is_fiat_sent", "type", "currency", "is_swap")
            oa.ordering = ("-created_at",)
            oa.list_per_page = 50
            oa.date_hierarchy = "created_at"

        if LNPayment in registry:
            pa = registry[LNPayment]
            pa.ordering = ("-created_at",)
            pa.list_per_page = 50

        if Robot in registry:
            ra = registry[Robot]
            ra.list_per_page = 50
