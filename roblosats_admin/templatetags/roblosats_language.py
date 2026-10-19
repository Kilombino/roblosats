"""Template tags for the Roblosats coordinator language selector."""

from django import template

from roblosats_admin.language import get_language

register = template.Library()


@register.simple_tag(takes_context=True)
def coordinator_language(context):
    """Return the current administrator's preferred language."""
    request = context.get("request")

    if request is None:
        return "es"

    return get_language(request)


@register.simple_tag(takes_context=True)
def coordinator_text(context, text):
    """Translate dashboard text using the coordinator language cookie."""
    from roblosats_admin.language import translate

    request = context.get("request")
    language = get_language(request) if request else "es"
    return translate(text, language)


@register.simple_tag(takes_context=True)
def coordinator_disputes(context, count):
    """Return a localized dispute notification."""
    from roblosats_admin.language import translate

    request = context.get("request")
    language = get_language(request) if request else "es"

    count = int(count)
    if count == 1:
        message = "{count} dispute awaiting resolution"
    else:
        message = "{count} disputes awaiting resolution"

    return translate(message, language).format(count=count)
