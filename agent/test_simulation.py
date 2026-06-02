"""
Tests for the simulation engine and historical price lookup.
Kept separate from tests.py (which has unrelated pre-existing import rot) so these
run independently. No network/LLM — yfinance is mocked.
"""
import unittest
from unittest.mock import patch, MagicMock

from agent.simulation import (
    project_growth, required_monthly_for_target, recommend_investment,
    DEFAULT_ANNUAL_RETURN_PCT,
)
from agent.price_service import get_period_start_price, get_period_returns


class ProjectGrowthTest(unittest.TestCase):
    def test_no_growth_no_contrib_is_flat(self):
        r = project_growth(1000, 0, annual_return_pct=0, years=5)
        self.assertAlmostEqual(r["final_gross"], 1000, places=2)
        self.assertEqual(r["total_gain"], 0.0)
        self.assertEqual(r["final_net"], r["final_gross"])  # no gain → no tax

    def test_contributions_counted_without_growth(self):
        r = project_growth(0, 100, annual_return_pct=0, years=2)
        self.assertAlmostEqual(r["total_contributed"], 2400, places=2)
        self.assertAlmostEqual(r["final_gross"], 2400, places=2)

    def test_growth_produces_gain_and_tax(self):
        r = project_growth(10000, 0, annual_return_pct=7, years=10)
        self.assertGreater(r["final_gross"], 10000)
        self.assertGreater(r["total_gain"], 0)
        self.assertGreater(r["tax_on_gain"], 0)
        self.assertLess(r["final_net"], r["final_gross"])

    def test_series_spans_year_zero_to_horizon(self):
        r = project_growth(500, 50, years=8)
        self.assertEqual(r["series"][0]["year"], 0)
        self.assertEqual(r["series"][-1]["year"], 8)
        self.assertEqual(len(r["series"]), 9)

    def test_default_return_applied(self):
        r = project_growth(1000, 0, years=1)
        self.assertEqual(r["inputs"]["annual_return_pct"], DEFAULT_ANNUAL_RETURN_PCT)
        self.assertAlmostEqual(r["final_gross"], 1070, delta=1.0)


class RequiredMonthlyTest(unittest.TestCase):
    def test_zero_when_target_already_met(self):
        self.assertEqual(required_monthly_for_target(1000, 10, 7, start_amount=100000), 0.0)

    def test_no_growth_is_linear(self):
        self.assertAlmostEqual(required_monthly_for_target(12000, 1, 0, 0), 1000, places=2)

    def test_reaching_target_is_consistent_with_projection(self):
        need = required_monthly_for_target(50000, 10, 7, start_amount=5000)
        r = project_growth(5000, need, annual_return_pct=7, years=10)
        self.assertAlmostEqual(r["final_gross"], 50000, delta=50)


class RecommendInvestmentTest(unittest.TestCase):
    def test_clamps_negatives_and_sums_first_year(self):
        rec = recommend_investment(-100, 200)
        self.assertEqual(rec["lump_sum_available"], 0.0)
        self.assertEqual(rec["first_year_total"], 2400.0)


class PeriodStartPriceTest(unittest.TestCase):
    def test_unknown_range_returns_zero(self):
        self.assertEqual(get_period_start_price("VWCE.DE", "ALL"), 0.0)

    @patch("agent.price_service.yf.Ticker")
    def test_returns_first_close_in_window(self, mock_ticker):
        import pandas as pd
        inst = MagicMock()
        inst.history.return_value = pd.DataFrame({"Close": [90.0, 95.0, 100.0]})
        mock_ticker.return_value = inst
        self.assertEqual(get_period_start_price("VWCE.DE", "1M"), 90.0)

    @patch("agent.price_service.yf.Ticker")
    def test_empty_history_returns_zero(self, mock_ticker):
        import pandas as pd
        inst = MagicMock()
        inst.history.return_value = pd.DataFrame()
        mock_ticker.return_value = inst
        self.assertEqual(get_period_start_price("VWCE.DE", "1Y"), 0.0)


class PeriodReturnsTest(unittest.TestCase):
    @patch("agent.price_service.yf.Ticker")
    def test_longer_window_larger_gain_when_monotonic(self, mock_ticker):
        import pandas as pd
        idx = pd.date_range(end="2026-06-01", periods=365, freq="D")
        closes = pd.Series([100.0 + i * 0.1 for i in range(365)], index=idx)
        inst = MagicMock()
        inst.history.return_value = pd.DataFrame({"Close": closes})
        mock_ticker.return_value = inst
        r = get_period_returns("VWCE.DE")
        # On a monotonically rising series, a longer trailing window = bigger % gain.
        self.assertGreater(r["1Y"], r["6M"])
        self.assertGreater(r["6M"], r["3M"])
        self.assertGreater(r["3M"], r["1M"])
        self.assertGreater(r["1M"], 0)

    @patch("agent.price_service.yf.Ticker")
    def test_empty_history_all_zero(self, mock_ticker):
        import pandas as pd
        inst = MagicMock()
        inst.history.return_value = pd.DataFrame()
        mock_ticker.return_value = inst
        self.assertEqual(set(get_period_returns("VWCE.DE").values()), {0.0})
