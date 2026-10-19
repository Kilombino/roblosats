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

        from .filters import BilingualChoicesFieldListFilter

        registry = admin.site._registry

        from .admin_i18n import CoordinatorAdminI18nMixin

        for model, model_admin in registry.items():
            current_class = model_admin.__class__

            if issubclass(current_class, CoordinatorAdminI18nMixin):
                continue

            bilingual_class = type(
                "Bilingual" + current_class.__name__,
                (CoordinatorAdminI18nMixin, current_class),
                {},
            )

            model_admin.__class__ = bilingual_class


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
                ("status", BilingualChoicesFieldListFilter),
                "is_disputed",
                "is_fiat_sent",
                ("type", BilingualChoicesFieldListFilter),
                "currency",
                "is_swap",
            )
            oa.ordering = ("-created_at",)
            oa.list_per_page = 50
            oa.date_hierarchy = "created_at"

        if LNPayment in registry:
            pa = registry[LNPayment]
            pa.ordering = ("-created_at",)
            pa.list_per_page = 50

        from api.models import OnchainPayment

        if LNPayment in registry:
            registry[LNPayment].list_filter = (
                ("type", BilingualChoicesFieldListFilter),
                ("concept", BilingualChoicesFieldListFilter),
                ("status", BilingualChoicesFieldListFilter),
            )

        if OnchainPayment in registry:
            registry[OnchainPayment].list_filter = (
                ("concept", BilingualChoicesFieldListFilter),
                ("status", BilingualChoicesFieldListFilter),
            )

        if Robot in registry:
            ra = registry[Robot]
            ra.list_per_page = 50
