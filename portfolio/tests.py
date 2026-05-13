"""
Unit and integration tests for the portfolio app.
Covers: UserProfile model, Holding model, views (overview, upload_csv, add_manual,
holdings_partial, tax_partial). Price fetching is mocked throughout.
"""
import csv
import io
from unittest.mock import patch

from django.test import TestCase, Client

from portfolio.models import UserProfile, Holding, Goal, ExitRule


# ── Model Tests ──────────────────────────────────────────────────────────────

class UserProfileInvestableSurplusTest(TestCase):
    def _profile(self, savings, emergency):
        return UserProfile(savings_total=savings, emergency_fund_floor=emergency)

    def test_positive_surplus(self):
        self.assertAlmostEqual(self._profile(50_000, 10_000).investable_surplus(), 40_000)

    def test_zero_surplus_when_savings_equals_floor(self):
        self.assertEqual(self._profile(10_000, 10_000).investable_surplus(), 0)

    def test_never_negative(self):
        self.assertEqual(self._profile(5_000, 10_000).investable_surplus(), 0)

    def test_zero_floor(self):
        self.assertAlmostEqual(self._profile(20_000, 0).investable_surplus(), 20_000)


class UserProfileStrTest(TestCase):
    def test_str_returns_user_id(self):
        p = UserProfile(user_id="alice")
        self.assertEqual(str(p), "alice")


class GoalStrTest(TestCase):
    def setUp(self):
        self.profile = UserProfile.objects.create(user_id="test-goal-str")

    def test_str_includes_name_and_user_id(self):
        goal = Goal.objects.create(profile=self.profile, name="Retirement")
        self.assertIn("Retirement", str(goal))
        self.assertIn("test-goal-str", str(goal))


class HoldingStrTest(TestCase):
    def setUp(self):
        self.profile = UserProfile.objects.create(user_id="test-holding-str")

    def test_str_includes_ticker_and_user_id(self):
        h = Holding.objects.create(
            profile=self.profile,
            ticker="VWCE.DE",
            asset_type="etf_acc",
            units=10,
            avg_purchase_price=90,
        )
        self.assertIn("VWCE.DE", str(h))
        self.assertIn("test-holding-str", str(h))


class HoldingRelationshipTest(TestCase):
    def setUp(self):
        self.profile = UserProfile.objects.create(user_id="test-rel")

    def test_holding_accessible_via_profile(self):
        Holding.objects.create(
            profile=self.profile,
            ticker="EUNL.DE",
            asset_type="etf_acc",
            units=5,
            avg_purchase_price=60,
        )
        self.assertEqual(self.profile.holdings.count(), 1)

    def test_exit_rule_cascade_deletes_with_holding(self):
        h = Holding.objects.create(
            profile=self.profile,
            ticker="XEON.DE",
            asset_type="etf_acc",
            units=3,
            avg_purchase_price=50,
        )
        ExitRule.objects.create(holding=h)
        self.assertEqual(ExitRule.objects.count(), 1)
        h.delete()
        self.assertEqual(ExitRule.objects.count(), 0)


# ── View Tests ────────────────────────────────────────────────────────────────

class OverviewViewTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.profile = UserProfile.objects.create(user_id="demo")
        Holding.objects.create(
            profile=self.profile,
            ticker="VWCE.DE",
            asset_type="etf_acc",
            units=10,
            avg_purchase_price=90,
        )

    @patch("portfolio.views.get_prices", return_value={"VWCE.DE": 100.0})
    def test_returns_200(self, _mock):
        response = self.client.get("/portfolio/")
        self.assertEqual(response.status_code, 200)

    @patch("portfolio.views.get_prices", return_value={"VWCE.DE": 100.0})
    def test_holding_values_calculated(self, _mock):
        self.client.get("/portfolio/")
        h = Holding.objects.get(ticker="VWCE.DE")
        self.assertAlmostEqual(h.current_price, 100.0)
        self.assertAlmostEqual(h.current_value, 1000.0)
        self.assertAlmostEqual(h.unrealised_gain, 100.0)

    @patch("portfolio.views.get_prices", return_value={"VWCE.DE": 100.0})
    def test_context_contains_totals(self, _mock):
        response = self.client.get("/portfolio/")
        self.assertIn("total_invested", response.context)
        self.assertIn("total_value", response.context)
        self.assertIn("total_gain", response.context)


class AddManualViewTest(TestCase):
    def setUp(self):
        self.client = Client()
        UserProfile.objects.create(user_id="demo")

    def test_creates_holding_and_exit_rule(self):
        self.client.post("/portfolio/manual/", {
            "ticker": "vwce.de",
            "isin": "IE00B3RBWM25",
            "asset_type": "etf_acc",
            "units": "10",
            "avg_purchase_price": "90",
        })
        self.assertEqual(Holding.objects.count(), 1)
        self.assertEqual(ExitRule.objects.count(), 1)

    def test_ticker_uppercased(self):
        self.client.post("/portfolio/manual/", {
            "ticker": "vwce.de",
            "asset_type": "etf_acc",
            "units": "5",
            "avg_purchase_price": "95",
        })
        self.assertEqual(Holding.objects.first().ticker, "VWCE.DE")

    def test_redirects_to_portfolio(self):
        response = self.client.post("/portfolio/manual/", {
            "ticker": "VWCE.DE",
            "asset_type": "etf_acc",
            "units": "1",
            "avg_purchase_price": "100",
        })
        self.assertRedirects(response, "/portfolio/", fetch_redirect_response=False)


class UploadCsvViewTest(TestCase):
    def setUp(self):
        self.client = Client()
        UserProfile.objects.create(user_id="demo")

    def _make_csv(self, rows):
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=[
            "ticker", "isin", "asset_type", "units", "avg_purchase_price", "purchase_date"
        ])
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
        buf.seek(0)
        return io.BytesIO(buf.read().encode())

    def test_creates_holdings_from_csv(self):
        f = self._make_csv([
            {"ticker": "VWCE.DE", "isin": "IE00B3RBWM25", "asset_type": "etf_acc",
             "units": "10", "avg_purchase_price": "90", "purchase_date": "2023-01-01"},
            {"ticker": "EUNL.DE", "isin": "IE00B4L5Y983", "asset_type": "etf_acc",
             "units": "5",  "avg_purchase_price": "60", "purchase_date": "2023-06-01"},
        ])
        self.client.post("/portfolio/upload/csv/", {"csv_file": f})
        self.assertEqual(Holding.objects.count(), 2)

    def test_no_file_returns_400(self):
        response = self.client.post("/portfolio/upload/csv/")
        self.assertEqual(response.status_code, 400)

    def test_missing_required_columns_returns_400(self):
        f = io.BytesIO(b"name,price\nfoo,10")
        response = self.client.post("/portfolio/upload/csv/", {"csv_file": f})
        self.assertEqual(response.status_code, 400)


class HoldingsPartialViewTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.profile = UserProfile.objects.create(user_id="demo")

    def test_returns_200_with_no_holdings(self):
        response = self.client.get("/portfolio/holdings/")
        self.assertEqual(response.status_code, 200)

    def test_renders_holdings_template(self):
        response = self.client.get("/portfolio/holdings/")
        self.assertTemplateUsed(response, "portfolio/holdings.html")


class TaxPartialViewTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.profile = UserProfile.objects.create(user_id="demo")

    def test_returns_200_with_no_holdings(self):
        response = self.client.get("/portfolio/tax/")
        self.assertEqual(response.status_code, 200)

    def test_renders_tax_template(self):
        response = self.client.get("/portfolio/tax/")
        self.assertTemplateUsed(response, "portfolio/tax_summary.html")

    def test_tax_rows_populated(self):
        Holding.objects.create(
            profile=self.profile,
            ticker="VWCE.DE",
            asset_type="etf_acc",
            units=10,
            avg_purchase_price=90,
            current_value=1000,
            unrealised_gain=100,
        )
        response = self.client.get("/portfolio/tax/")
        self.assertEqual(len(response.context["tax_rows"]), 1)
