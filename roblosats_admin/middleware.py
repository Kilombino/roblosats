"""Coordinator-only language activation for Roblosats."""

from django.utils import translation
from django.utils.deprecation import MiddlewareMixin

from .language import get_language


class CoordinatorLanguageMiddleware(MiddlewareMixin):
    """Apply the selected language only to coordinator admin requests."""

    def process_request(self, request):
        if not request.path.startswith("/coordinator/"):
            return None

        request._roblosats_previous_language = translation.get_language()
        language = get_language(request)
        translation.activate(language)
        request.LANGUAGE_CODE = language
        return None

    def process_response(self, request, response):
        if request.path.startswith("/coordinator/"):
            previous = getattr(request, "_roblosats_previous_language", None)
            if previous:
                translation.activate(previous)
            else:
                translation.deactivate()

        return response
