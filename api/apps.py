from django.apps import AppConfig


class ApiConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "api"

    def ready(self):
        # 0.9.4: a notice for every order step (api/step_alerts.py)
        from api import step_alerts
        from api.models.order import order_status_changed

        order_status_changed.connect(
            step_alerts.on_status_changed, dispatch_uid="roblosats_step_alerts"
        )
