"""Request-aware translations for coordinator choice filters."""

from django.contrib.admin.filters import ChoicesFieldListFilter
from django.utils import translation

from .language import translate


class BilingualChoicesFieldListFilter(ChoicesFieldListFilter):
    """Translate choice labels without changing filter values."""

    def choices(self, changelist):
        language = translation.get_language() or "es"
        language = language.split("-")[0]

        for choice in super().choices(changelist):
            choice = choice.copy()

            # Django already translates the built-in "All" label.
            # Preserve facet-count suffixes when present.
            display = str(choice["display"])
            translated = translate(display, language)

            if translated == display and " (" in display:
                label, suffix = display.rsplit(" (", 1)
                translated_label = translate(label, language)
                if translated_label != label:
                    translated = translated_label + " (" + suffix

            choice["display"] = translated
            yield choice
