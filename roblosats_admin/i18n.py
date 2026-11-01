from django.utils.translation import pgettext, pgettext_lazy

CONTEXT = "roblosats-admin"


def _(message):
    return pgettext(CONTEXT, message)


def _lazy(message):
    return pgettext_lazy(CONTEXT, message)
