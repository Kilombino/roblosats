"""Language selection for the Roblosats coordinator administration."""

from django.contrib.admin.views.decorators import staff_member_required
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_POST
from django.shortcuts import redirect
from django.utils.http import url_has_allowed_host_and_scheme

from .language import LANGUAGES


@staff_member_required
@csrf_protect
@require_POST
def set_admin_language(request):
    """Save the administrator's preferred coordinator language."""
    language = request.POST.get("language", "es")

    if language not in LANGUAGES:
        language = "es"

    next_url = request.POST.get("next", "/coordinator/")

    if not url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        next_url = "/coordinator/"

    response = redirect(next_url)
    response.set_cookie(
        "roblosats_admin_language",
        language,
        max_age=365 * 24 * 60 * 60,
        path="/coordinator/",
        secure=request.is_secure(),
        httponly=True,
        samesite="Lax",
    )
    return response
