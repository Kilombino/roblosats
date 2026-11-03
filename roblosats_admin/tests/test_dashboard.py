from unittest.mock import patch

from django.test import RequestFactory, TestCase
from django.urls import reverse

from api.models import Order
from roblosats_admin import dashboard
from roblosats_admin.tests.test_disputes import make_disputed_order, make_user


@patch("roblosats_admin.dashboard._channel_balance", return_value=("?", "?"))
class DashboardDisputeLinkTest(TestCase):
    def data(self):
        return dashboard.dashboard(RequestFactory().get("/coordinator/"))

    def test_one_dispute_opens_it_directly(self, _balance):
        order = make_disputed_order(Order.Types.BUY, make_user("maker"), make_user("taker"), status=Order.Status.WFR)
        d = self.data()
        url = reverse("admin:api_order_dispute", args=[order.pk])
        self.assertEqual(d["open_disputes"], 1)
        self.assertEqual(d["links"]["disputes"], url)
        self.assertEqual([c["link"] for c in d["cards"] if c["value"] == 1 and c["link"] == url], [url])

    def test_several_disputes_lead_to_the_list(self, _balance):
        make_disputed_order(Order.Types.BUY, make_user("m1"), make_user("t1"))
        make_disputed_order(Order.Types.SELL, make_user("m2"), make_user("t2"), status=Order.Status.WFR)
        d = self.data()
        self.assertEqual(d["open_disputes"], 2)
        self.assertTrue(d["links"]["disputes"].endswith("api/order/disputes/?view=open"))
