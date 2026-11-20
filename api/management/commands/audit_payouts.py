import re
from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db.models import Sum

from api.escrow import flow as escrow_flow
from api.lightning.node import LNNode
from api.models import LNPayment, OnchainPayment, Order, Robot

REWARDED = re.compile(r"Robot\((\d+),[^)]*\) was rewarded (\d+) Sats")
RETURNED = re.compile(r"Robot\((\d+),[^)]*\) was returned (\d+) Sats")


def _corrected(order, proxy):
    """A correction of a wrong slash_return was already booked in the reward ledger."""
    from django.db import connection

    from api.logics import LEDGER_TABLE, ensure_ledger

    ensure_ledger()
    with connection.cursor() as c:
        c.execute(
            f"SELECT 1 FROM {LEDGER_TABLE} WHERE fingerprint = %s",
            [f"correction:{order.id}:slash_return:{proxy.payment_hash}"],
        )
        return c.fetchone() is not None


LEDGER_KINDS_NOT_IN_LOGS = ("swap_surcharge_back", "dispute_bond_back")


def ledger_credits(kinds):
    """{robot_id: sats} credited in coord_reward_ledger with these kinds ({} without the table)."""
    from django.db import connection, transaction

    out = defaultdict(int)
    try:
        with transaction.atomic(), connection.cursor() as c:
            c.execute(
                "SELECT robot_id, SUM(amount) FROM coord_reward_ledger WHERE kind = ANY(%s) AND amount > 0 "
                "GROUP BY robot_id",
                [list(kinds)],
            )
            for rid, sats in c.fetchall():
                out[int(rid)] += int(sats)
    except Exception:
        return {}
    return out


class Command(BaseCommand):
    help = (
        "Read-only audit of past rewards and payouts against the payout safety rules: "
        "rewards credited more than once per order, rewards whose slashed bond the "
        "lightning node does not show as settled, robots that withdrew more than they "
        "were ever credited, buyer payouts not covered by a settled escrow, and buyers paid both by lightning and onchain. Changes nothing."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--no-lnd", action="store_true", help="skip the lightning node lookups"
        )

    def handle(self, *args, **options):
        lnd = not options["no_lnd"]
        problems = 0
        credited = defaultdict(int)

        def report(text):
            nonlocal problems
            problems += 1
            self.stdout.write(f"  - {text}")

        self.stdout.write("Rewards credited from slashed bonds:")
        for order in Order.objects.filter(logs__contains="was rewarded").order_by("id"):
            rewards = REWARDED.findall(order.logs)
            if len(rewards) > 1:
                total = sum(int(s) for _, s in rewards)
                report(
                    f"order {order.id}: rewarded {len(rewards)} times ({total} sats in all)"
                )
            # Only the first credit per order counts as legitimate.
            robot, sats = rewards[0]
            credited[int(robot)] += int(sats)
            returned = RETURNED.findall(order.logs)
            if returned:
                credited[int(returned[0][0])] += int(returned[0][1])
            # rl31-beta22: an onchain maker bond behind a proxy (hybrid B) is checked onchain,
            # and nothing of it may go back to the maker that lost it (order #23).
            proxy = order.maker_bond if escrow_flow.is_bond_proxy(order.maker_bond) else None
            if proxy is not None and proxy.status == LNPayment.Status.SETLED:
                if not escrow_flow.proxy_is_penalized(proxy):
                    report(f"order {order.id}: proxy {proxy.payment_hash} is SETTLED but its onchain bond is not penalized")
                elif returned and not _corrected(order, proxy):
                    report(
                        f"order {order.id}: robot {returned[0][0]} was credited back {returned[0][1]} sats "
                        f"of its penalized onchain bond (the penalty paid {escrow_flow.proxy_penalty_sats(proxy)})"
                    )
            if lnd:
                for bond in (order.maker_bond, order.taker_bond):
                    if bond is None or bond.status != LNPayment.Status.SETLED:
                        continue
                    if escrow_flow.is_bond_proxy(bond):
                        continue  # not a Lightning invoice: checked above
                    try:
                        ok = LNNode.double_check_htlc_is_settled(bond.payment_hash)
                    except Exception as e:
                        ok = None
                        report(
                            f"order {order.id}: could not look up bond {bond.payment_hash} ({e})"
                        )
                    if ok is False:
                        report(
                            f"order {order.id}: bond {bond.payment_hash} is SETTLED in the database but not in the lightning node"
                        )

        # 0.9.10: other legitimate credits that are not in the order logs: the unused part of a
        # hybrid B swap surcharge going back to the seller, and a Lightning taker bond credited
        # back after an onchain dispute (robot 370: 616 sats of swap_surcharge_back showed as a
        # withdrawal nobody had credited).
        for rid, sats in ledger_credits(LEDGER_KINDS_NOT_IN_LOGS).items():
            credited[rid] += sats

        self.stdout.write(
            "Reward withdrawals against what was credited (admin credits are not in the logs: check those by hand):"
        )
        withdrawn = (
            LNPayment.objects.filter(
                concept=LNPayment.Concepts.WITHREWA, status=LNPayment.Status.SUCCED
            )
            .values("receiver__robot")
            .annotate(sats=Sum("num_satoshis"))
        )
        for row in withdrawn:
            rid = row["receiver__robot"]
            robot = Robot.objects.filter(pk=rid).first()
            balance = robot.earned_rewards if robot else 0
            if row["sats"] + balance > credited[rid] + 100:
                name = robot.user.username if robot else "?"
                report(
                    f"robot {rid} ({name}): withdrew {row['sats']} + holds {balance}, "
                    f"but was credited {credited[rid]} by slashed bonds"
                )

        self.stdout.write("Buyer payouts:")
        for payout in LNPayment.objects.filter(
            concept=LNPayment.Concepts.PAYBUYER, status=LNPayment.Status.SUCCED
        ):
            order = Order.objects.filter(payout=payout).first()
            if order is None:
                report(f"payout {payout.payment_hash}: paid but no order has it")
                continue
            escrow = order.trade_escrow
            if escrow is None or escrow.status != LNPayment.Status.SETLED:
                report(
                    f"order {order.id}: buyer paid {payout.num_satoshis} without a settled escrow"
                )
            elif escrow.num_satoshis < payout.num_satoshis:
                report(
                    f"order {order.id}: buyer paid {payout.num_satoshis} > escrow {escrow.num_satoshis}"
                )
            elif lnd:
                try:
                    if not LNNode.double_check_htlc_is_settled(escrow.payment_hash):
                        report(
                            f"order {order.id}: escrow is SETTLED in the database but not in the lightning node"
                        )
                except Exception as e:
                    report(f"order {order.id}: could not look up the escrow ({e})")

        self.stdout.write("Buyers paid both by lightning and onchain:")
        for order in Order.objects.filter(
            payout__status=LNPayment.Status.SUCCED,
            payout_tx__status__in=[
                OnchainPayment.Status.QUEUE,
                OnchainPayment.Status.MEMPO,
                OnchainPayment.Status.CONFI,
            ],
        ):
            report(
                f"order {order.id}: lightning payout {order.payout.payment_hash} succeeded and onchain payout {order.payout_tx.id} is {order.payout_tx.get_status_display()}"
            )

        self.stdout.write(f"{problems} finding(s).")
