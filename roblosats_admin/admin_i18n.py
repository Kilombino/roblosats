"""Request-aware translations for coordinator administration."""

from django.utils.translation import get_language
from .language import translate


class CoordinatorAdminI18nMixin:
    """Translate admin actions and form choices without changing values."""

    def _coordinator_translate(self, text):
        language = get_language() or "es"
        language = language.split("-")[0]
        return translate(str(text), language)

    def get_action_choices(self, request, default_choices=None):
        if default_choices is None:
            choices = super().get_action_choices(request)
        else:
            choices = super().get_action_choices(
                request, default_choices=default_choices
            )

        return [
            (value, self._coordinator_translate(label))
            for value, label in choices
        ]

    def formfield_for_choice_field(self, db_field, request, **kwargs):
        formfield = super().formfield_for_choice_field(
            db_field, request, **kwargs
        )

        if formfield is not None and db_field.choices:
            formfield.choices = [
                (value, self._coordinator_translate(label))
                for value, label in formfield.choices
            ]

        return formfield
