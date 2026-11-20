"""Validators whose limit comes from a setting the coordinator configures (MIN_TRADE,
MAX_TRADE). Django writes a validator's limit into the migrations, so with plain
MinValueValidator(settings.MIN_TRADE) every coordinator whose limits differ from the ones the
migrations were made with got "Your models in app(s): 'api' have changes that are not yet
reflected in a migration" at every start (0.9.9). These validate with the configured value but
are written to the migrations by the setting's name, the same on every coordinator."""

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator


class _FromSetting:
    def __init__(self, setting, factor=1):
        self.setting = setting
        self.factor = factor
        super().__init__(getattr(settings, setting) * factor)

    def deconstruct(self):
        kwargs = {"factor": self.factor} if self.factor != 1 else {}
        return f"api.models.validators.{type(self).__name__}", (self.setting,), kwargs


class MinFromSetting(_FromSetting, MinValueValidator):
    pass


class MaxFromSetting(_FromSetting, MaxValueValidator):
    pass
