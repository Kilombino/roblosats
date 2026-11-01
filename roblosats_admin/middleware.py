from django.conf import settings
from django.http import HttpResponseRedirect
from django.utils import translation
from django.utils.cache import patch_vary_headers
from django.utils.translation.trans_real import parse_accept_lang_header

ADMIN_PREFIX = "/coordinator/"

PANEL_LANGUAGES = [
    ("en", "English"),
    ("es", "Español"),
    ("de", "Deutsch"),
    ("fr", "Français"),
    ("it", "Italiano"),
    ("pt", "Português"),
    ("ru", "Русский"),
    ("zh-hans", "简体中文"),
    ("ja", "日本語"),
]
PANEL_CODES = {code for code, _name in PANEL_LANGUAGES}


def panel_language(code):
    code = (code or "").lower().replace("_", "-")
    if code.startswith("zh"):
        return "zh-hans"
    base = code.split("-")[0]
    return base if base in PANEL_CODES else None


def browser_language(request):
    header = request.META.get("HTTP_ACCEPT_LANGUAGE", "")
    for code, _quality in parse_accept_lang_header(header):
        lang = panel_language(code)
        if lang:
            return lang
    return None


def default_language():
    return panel_language(settings.LANGUAGE_CODE) or "en"


class AdminLocaleMiddleware:
    """Activates the panel language only for admin requests."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.path.startswith(ADMIN_PREFIX):
            return self.get_response(request)

        chosen = request.GET.get("lang")
        if chosen is not None and request.method == "GET":
            lang = panel_language(chosen)
            query = request.GET.copy()
            query.pop("lang", None)
            target = request.path + (f"?{query.urlencode()}" if query else "")
            response = HttpResponseRedirect(target)
            if lang:
                response.set_cookie(
                    settings.LANGUAGE_COOKIE_NAME,
                    lang,
                    max_age=365 * 24 * 3600,
                    path=ADMIN_PREFIX,
                    samesite="Lax",
                    httponly=True,
                )
            return response

        lang = (
            panel_language(request.COOKIES.get(settings.LANGUAGE_COOKIE_NAME))
            or browser_language(request)
            or default_language()
        )
        with translation.override(lang):
            request.LANGUAGE_CODE = lang
            response = self.get_response(request)
        response.headers.setdefault("Content-Language", lang)
        patch_vary_headers(response, ("Accept-Language", "Cookie"))
        return response
