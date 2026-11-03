from django.contrib import admin
from django.http import HttpResponseRedirect
from django.urls import reverse

from roblosats_admin import alerts
from roblosats_admin.models import TelegramAlerts


@admin.register(TelegramAlerts)
class TelegramAlertsAdmin(admin.ModelAdmin):
    def has_module_permission(self, request):
        return alerts.can_configure(request)

    def has_view_permission(self, request, obj=None):
        return alerts.can_configure(request)

    def has_change_permission(self, request, obj=None):
        return alerts.can_configure(request)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        return alerts.settings_view(self, request)

    def change_view(self, request, object_id, form_url="", extra_context=None):
        return HttpResponseRedirect(
            reverse("admin:roblosats_admin_telegramalerts_changelist")
        )
