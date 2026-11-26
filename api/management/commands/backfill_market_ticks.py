"""Roblosats 0.9.15: market ticks for completed trades that never got one.

Until 0.9.15 a contract was logged as a market tick only when the taker had locked a Lightning
bond: onchain orders taken with an onchain bond and mixta D (order.taker_bond is None) were
missing from the 24 h and lifetime volumes. This creates one tick per such completed trade,
timestamped when the trade finished. Dry run unless --apply; run it once.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from api.models import MarketTick, Order


def missing_tick_orders():
    """Completed trades (successful, or decided disputes) taken without a Lightning bond."""
    return Order.objects.filter(
        status__in=(Order.Status.SUC, Order.Status.MLD, Order.Status.TLD),
        taker__isnull=False,
        taker_bond__isnull=True,
        last_satoshis__gt=0,
    ).order_by("id")


class Command(BaseCommand):
    help = "Create the market ticks of completed trades taken with an onchain bond (dry run unless --apply)"

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        orders = list(missing_tick_orders())
        total = 0
        for order in orders:
            total += order.last_satoshis
            self.stdout.write(f"order {order.id}: {order.last_satoshis} sats, finished {order.contract_finalization_time}")
        self.stdout.write(f"{len(orders)} trade(s), {total / 1e8:.8f} BTC")
        if not options["apply"]:
            self.stdout.write("dry run: nothing written (use --apply)")
            return
        made = 0
        with transaction.atomic():
            for order in orders:
                tick = MarketTick.log_a_tick(order)
                if tick is None:
                    continue
                when = order.contract_finalization_time or order.created_at
                MarketTick.objects.filter(pk=tick.pk).update(timestamp=when)
                order.log(f"Market tick backfilled (0.9.15): MarketTick({tick.id})")
                made += 1
        self.stdout.write(f"{made} tick(s) created")
