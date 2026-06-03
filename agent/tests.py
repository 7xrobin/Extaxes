"""
Unit tests for the agent layer.
Covers: tax_engine, price_service, node helpers, routing, analysis.
No LLM calls — OpenAI and yfinance are mocked throughout.
"""
import unittest
from unittest.mock import patch, MagicMock
from django.test import TestCase

from agent.tax_engine import (
    effective_rate, tax_on_exit, vorabpauschale,
    sparerpauschbetrag_limit, exit_tax_applies, compare_etf_vs_stock,
    STANDARD_RATE, TEILFREISTELLUNG,
)
from agent.nodes import (
    _extract_number, _parse_goals, _estimate_tax_bracket,
    _msg_role, _msg_content, route_after_intake, route_after_qa,
    intake_node, analysis_node, fetch_prices_node,
    retrieve_node, simulate_node,
)


# ── Tax Engine ──────────────────────────────────────────────────────────────

class EffectiveRateTest(unittest.TestCase):
    def test_etf_acc_gets_30_pct_exemption(self):
        rate = effective_rate("etf_acc")
        self.assertAlmostEqual(rate, STANDARD_RATE * 0.70, places=6)

    def test_etf_dist_gets_30_pct_exemption(self):
        self.assertAlmostEqual(effective_rate("etf_dist"), effective_rate("etf_acc"), places=6)

    def test_stock_no_exemption(self):
        self.assertAlmostEqual(effective_rate("stock"), STANDARD_RATE, places=6)

    def test_savings_no_exemption(self):
        self.assertAlmostEqual(effective_rate("savings"), STANDARD_RATE, places=6)

    def test_unknown_asset_type_defaults_to_no_exemption(self):
        self.assertAlmostEqual(effective_rate("unknown"), STANDARD_RATE, places=6)

    def test_etf_rate_is_lower_than_stock_rate(self):
        self.assertLess(effective_rate("etf_acc"), effective_rate("stock"))


class TaxOnExitTest(unittest.TestCase):
    def test_positive_gain_etf_acc(self):
        gain = 1000.0
        tax = tax_on_exit(gain, "etf_acc")
        expected = gain * effective_rate("etf_acc")
        self.assertAlmostEqual(tax, expected, places=4)

    def test_positive_gain_stock(self):
        tax = tax_on_exit(1000.0, "stock")
        self.assertAlmostEqual(tax, 1000.0 * STANDARD_RATE, places=4)

    def test_zero_gain_returns_zero(self):
        self.assertEqual(tax_on_exit(0.0, "etf_acc"), 0.0)

    def test_negative_gain_returns_zero(self):
        self.assertEqual(tax_on_exit(-500.0, "stock"), 0.0)

    def test_etf_tax_less_than_stock_tax_for_same_gain(self):
        gain = 5000.0
        self.assertLess(tax_on_exit(gain, "etf_acc"), tax_on_exit(gain, "stock"))


class VorabpauschaleTest(unittest.TestCase):
    def test_etf_acc_returns_positive_value(self):
        vp = vorabpauschale(10_000.0, "etf_acc")
        self.assertGreater(vp, 0.0)

    def test_etf_dist_returns_zero(self):
        self.assertEqual(vorabpauschale(10_000.0, "etf_dist"), 0.0)

    def test_stock_returns_zero(self):
        self.assertEqual(vorabpauschale(10_000.0, "stock"), 0.0)

    def test_savings_returns_zero(self):
        self.assertEqual(vorabpauschale(10_000.0, "savings"), 0.0)

    def test_large_distributions_reduce_vorabpauschale_to_zero(self):
        # If distributions >= basisertrag, vorabpauschale should be 0
        vp = vorabpauschale(10_000.0, "etf_acc", distributions=99_999.0)
        self.assertEqual(vp, 0.0)

    def test_calculation_uses_basiszins_and_70_pct_factor(self):
        value = 100_000.0
        from agent.tax_engine import BASISZINS_2026, BASISERTRAG_FACTOR
        basisertrag = value * BASISZINS_2026 * BASISERTRAG_FACTOR
        taxable = basisertrag * (1 - TEILFREISTELLUNG["etf_acc"])
        expected = taxable * STANDARD_RATE
        self.assertAlmostEqual(vorabpauschale(value, "etf_acc"), expected, places=6)


class SparerpauschbetragTest(unittest.TestCase):
    def test_single_gets_1000(self):
        self.assertEqual(sparerpauschbetrag_limit(is_married=False), 1_000.0)

    def test_married_gets_2000(self):
        self.assertEqual(sparerpauschbetrag_limit(is_married=True), 2_000.0)

    def test_default_is_single(self):
        self.assertEqual(sparerpauschbetrag_limit(), 1_000.0)


class ExitTaxTest(unittest.TestCase):
    def test_below_threshold_no_warning(self):
        self.assertFalse(exit_tax_applies(499_999.0))

    def test_at_threshold_no_warning(self):
        self.assertFalse(exit_tax_applies(500_000.0))

    def test_above_threshold_triggers_warning(self):
        self.assertTrue(exit_tax_applies(500_001.0))

    def test_zero_no_warning(self):
        self.assertFalse(exit_tax_applies(0.0))


class CompareEtfVsStockTest(unittest.TestCase):
    def setUp(self):
        self.result = compare_etf_vs_stock(10_000.0)

    def test_result_has_required_keys(self):
        for key in ("gain", "etf", "stock", "etf_advantage"):
            self.assertIn(key, self.result)

    def test_gain_matches_input(self):
        self.assertEqual(self.result["gain"], 10_000.0)

    def test_etf_tax_less_than_stock_tax(self):
        self.assertLess(self.result["etf"]["tax"], self.result["stock"]["tax"])

    def test_etf_advantage_is_positive(self):
        self.assertGreater(self.result["etf_advantage"], 0.0)

    def test_you_keep_equals_gain_minus_tax(self):
        for side in ("etf", "stock"):
            self.assertAlmostEqual(
                self.result[side]["you_keep"],
                self.result["gain"] - self.result[side]["tax"],
                places=4,
            )

    def test_zero_gain_returns_zero_tax(self):
        r = compare_etf_vs_stock(0.0)
        self.assertEqual(r["etf"]["tax"], 0.0)
        self.assertEqual(r["stock"]["tax"], 0.0)


# ── Price Service ────────────────────────────────────────────────────────────

class GetPriceTest(unittest.TestCase):
    @patch("agent.price_service.yf.Ticker")
    def test_returns_last_price_from_fast_info(self, mock_ticker_cls):
        mock_ticker = MagicMock()
        mock_ticker.fast_info.get.return_value = 157.50
        mock_ticker_cls.return_value = mock_ticker
        from agent.price_service import get_price
        self.assertEqual(get_price("VWCE.DE"), 157.50)

    @patch("agent.price_service.yf.Ticker")
    def test_falls_back_to_info_when_fast_info_returns_none(self, mock_ticker_cls):
        mock_ticker = MagicMock()
        mock_ticker.fast_info.get.return_value = None
        mock_ticker.info = {"regularMarketPrice": 200.0}
        mock_ticker_cls.return_value = mock_ticker
        from agent.price_service import get_price
        self.assertEqual(get_price("AAPL"), 200.0)

    @patch("agent.price_service.yf.Ticker")
    def test_returns_zero_on_exception(self, mock_ticker_cls):
        mock_ticker_cls.side_effect = Exception("network error")
        from agent.price_service import get_price
        self.assertEqual(get_price("BROKEN"), 0.0)

    @patch("agent.price_service.yf.Ticker")
    def test_returns_zero_when_price_is_none(self, mock_ticker_cls):
        mock_ticker = MagicMock()
        mock_ticker.fast_info.get.return_value = None
        mock_ticker.info = {"regularMarketPrice": None}
        mock_ticker_cls.return_value = mock_ticker
        from agent.price_service import get_price
        self.assertEqual(get_price("AAPL"), 0.0)


class GetPricesTest(unittest.TestCase):
    @patch("agent.price_service.get_price")
    def test_returns_dict_for_all_tickers(self, mock_get_price):
        mock_get_price.side_effect = lambda t: {"VWCE.DE": 100.0, "AAPL": 200.0}.get(t, 0.0)
        from agent.price_service import get_prices
        result = get_prices(["VWCE.DE", "AAPL"])
        self.assertEqual(result, {"VWCE.DE": 100.0, "AAPL": 200.0})

    @patch("agent.price_service.get_price")
    def test_empty_list_returns_empty_dict(self, mock_get_price):
        from agent.price_service import get_prices
        self.assertEqual(get_prices([]), {})


# ── Node Helpers ─────────────────────────────────────────────────────────────

class ExtractNumberTest(unittest.TestCase):
    def test_plain_integer(self):
        self.assertEqual(_extract_number("85000"), 85000.0)

    def test_with_euro_sign(self):
        self.assertEqual(_extract_number("€85000"), 85000.0)

    def test_with_comma_separator(self):
        self.assertEqual(_extract_number("85,000"), 85000.0)

    def test_k_suffix(self):
        self.assertEqual(_extract_number("85k"), 85000.0)

    def test_decimal_k_suffix(self):
        self.assertEqual(_extract_number("1.5k"), 1500.0)

    def test_euro_with_comma_and_k(self):
        self.assertEqual(_extract_number("€8k"), 8000.0)

    def test_no_number_returns_zero(self):
        self.assertEqual(_extract_number("skip"), 0.0)

    def test_empty_string_returns_zero(self):
        self.assertEqual(_extract_number(""), 0.0)

    def test_sentence_with_number(self):
        result = _extract_number("I have about 15000 saved")
        self.assertEqual(result, 15000.0)


class ParseGoalsTest(unittest.TestCase):
    def test_single_goal(self):
        goals = _parse_goals("financial independence")
        self.assertEqual(len(goals), 1)
        self.assertEqual(goals[0]["name"], "financial independence")
        self.assertEqual(goals[0]["priority"], 1)

    def test_multiple_goals_split_by_comma(self):
        goals = _parse_goals("financial independence, buy a house by 2030")
        self.assertEqual(len(goals), 2)
        self.assertEqual(goals[1]["priority"], 2)

    def test_not_sure_returns_fallback(self):
        goals = _parse_goals("not sure")
        self.assertEqual(len(goals), 1)
        self.assertEqual(goals[0]["name"], "grow savings")

    def test_skip_returns_fallback(self):
        goals = _parse_goals("skip")
        self.assertEqual(len(goals), 1)

    def test_dont_know_returns_fallback(self):
        goals = _parse_goals("don't know")
        self.assertEqual(len(goals), 1)

    def test_goals_have_required_keys(self):
        goals = _parse_goals("save more")
        required = {"name", "target_amount", "target_date", "monthly_allocation", "priority"}
        self.assertEqual(set(goals[0].keys()), required)


class EstimateTaxBracketTest(unittest.TestCase):
    def test_low_salary_14_pct(self):
        self.assertEqual(_estimate_tax_bracket("15000"), 0.14)

    def test_mid_salary_30_pct(self):
        self.assertEqual(_estimate_tax_bracket("30000"), 0.30)

    def test_upper_mid_salary_37_pct(self):
        self.assertEqual(_estimate_tax_bracket("50000"), 0.37)

    def test_high_salary_42_pct(self):
        self.assertEqual(_estimate_tax_bracket("80000"), 0.42)

    def test_skip_returns_default_42(self):
        self.assertEqual(_estimate_tax_bracket("skip"), 0.42)

    def test_zero_returns_default_42(self):
        self.assertEqual(_estimate_tax_bracket("0"), 0.42)


class MsgHelpersTest(unittest.TestCase):
    def test_msg_role_dict_user(self):
        self.assertEqual(_msg_role({"role": "user", "content": "hi"}), "user")

    def test_msg_role_dict_assistant(self):
        self.assertEqual(_msg_role({"role": "assistant", "content": "hi"}), "assistant")

    def test_msg_role_langgraph_ai_message(self):
        msg = MagicMock()
        msg.type = "ai"
        self.assertEqual(_msg_role(msg), "assistant")

    def test_msg_role_langgraph_human_message(self):
        msg = MagicMock()
        msg.type = "human"
        self.assertEqual(_msg_role(msg), "user")

    def test_msg_content_dict(self):
        self.assertEqual(_msg_content({"role": "user", "content": "hello"}), "hello")

    def test_msg_content_langgraph_message(self):
        msg = MagicMock()
        msg.content = "hello from agent"
        self.assertEqual(_msg_content(msg), "hello from agent")

    def test_msg_content_empty_dict(self):
        self.assertEqual(_msg_content({}), "")


# ── Routing Functions ────────────────────────────────────────────────────────

class RouteAfterIntakeTest(unittest.TestCase):
    def test_routes_to_upload_when_current_node_is_upload(self):
        state = {"current_node": "upload"}
        self.assertEqual(route_after_intake(state), "upload")

    def test_routes_to_continue_intake_otherwise(self):
        state = {"current_node": "intake"}
        self.assertEqual(route_after_intake(state), "continue_intake")

    def test_routes_to_continue_intake_when_key_missing(self):
        self.assertEqual(route_after_intake({}), "continue_intake")


class RouteAfterQaTest(unittest.TestCase):
    def test_save_routes_to_approval(self):
        self.assertEqual(route_after_qa({"current_node": "save"}), "approval")

    def test_adjust_routes_to_plan(self):
        self.assertEqual(route_after_qa({"current_node": "adjust"}), "plan")

    def test_answer_routes_to_answer_pipeline(self):
        self.assertEqual(route_after_qa({"current_node": "answer"}), "answer")

    def test_missing_node_defaults_to_answer(self):
        self.assertEqual(route_after_qa({}), "answer")


# ── Tool Nodes ────────────────────────────────────────────────────────────────

class FetchPricesNodeTest(unittest.TestCase):
    @patch("agent.nodes.fetch_prices")
    def test_stores_prices_in_state(self, mock_fetch):
        mock_fetch.return_value = {"VWCE.DE": 100.0}
        state = {"holdings": [{"ticker": "VWCE.DE"}]}
        result = fetch_prices_node(state)
        self.assertEqual(result["prices"], {"VWCE.DE": 100.0})
        self.assertEqual(result["current_node"], "analysis")
        mock_fetch.assert_called_once_with(["VWCE.DE"])

    @patch("agent.nodes.fetch_prices")
    def test_empty_holdings_fetches_no_tickers(self, mock_fetch):
        mock_fetch.return_value = {}
        result = fetch_prices_node({"holdings": []})
        self.assertEqual(result["prices"], {})
        mock_fetch.assert_called_once_with([])


class RetrieveNodeTest(unittest.TestCase):
    @patch("agent.nodes.retrieve_tax_context")
    def test_stores_retrieved_context_for_last_user_message(self, mock_retrieve):
        mock_retrieve.return_value = "SOURCES..."
        state = {"messages": [
            {"role": "assistant", "content": "plan"},
            {"role": "user", "content": "how does Vorabpauschale work?"},
        ]}
        result = retrieve_node(state)
        self.assertEqual(result["retrieved_context"], "SOURCES...")
        mock_retrieve.assert_called_once_with("how does Vorabpauschale work?")


class SimulateNodeTest(unittest.TestCase):
    @patch("agent.nodes.compute_projection")
    def test_stores_projection_context(self, mock_proj):
        mock_proj.return_value = "PROJECTION..."
        state = {"messages": [{"role": "user", "content": "how much in 10 years?"}]}
        result = simulate_node(state)
        self.assertEqual(result["projection_context"], "PROJECTION...")
        # called with (query, state-as-dict)
        self.assertEqual(mock_proj.call_args[0][0], "how much in 10 years?")


# ── Analysis consumes prices from state ───────────────────────────────────────

class AnalysisUsesStatePricesTest(unittest.TestCase):
    @patch("agent.nodes.get_prices")
    def test_prefers_state_prices_over_fetching(self, mock_get_prices):
        state = {
            "holdings": [{
                "ticker": "VWCE.DE", "isin": "", "asset_type": "etf_acc",
                "units": 10.0, "avg_purchase_price": 100.0, "purchase_date": "",
                "current_price": 0.0, "current_value": 0.0,
                "unrealised_gain": 0.0, "unrealised_gain_pct": 0.0,
            }],
            "prices": {"VWCE.DE": 130.0},
            "is_married": False,
        }
        result = analysis_node(state)
        self.assertAlmostEqual(result["holdings"][0]["current_value"], 1300.0)
        mock_get_prices.assert_not_called()


# ── Intake Node ───────────────────────────────────────────────────────────────

class IntakeNodeTest(unittest.TestCase):
    def _make_state(self, intake_step=0, messages=None, **kwargs):
        return {
            "user_id": "demo",
            "intake_step": intake_step,
            "messages": messages or [],
            **kwargs,
        }

    def test_first_call_asks_first_question(self):
        state = self._make_state(intake_step=0)
        result = intake_node(state)
        msgs = result["messages"]
        self.assertEqual(len(msgs), 1)
        self.assertIn("saved in total", msgs[-1]["content"])
        self.assertEqual(result["intake_step"], 1)

    def test_second_call_parses_savings_and_asks_emergency_fund(self):
        state = self._make_state(
            intake_step=1,
            messages=[
                {"role": "assistant", "content": "Q1"},
                {"role": "user", "content": "50000"},
            ],
        )
        result = intake_node(state)
        self.assertEqual(result.get("savings_total"), 50000.0)
        self.assertIn("emergency fund", result["messages"][-1]["content"])
        self.assertEqual(result["intake_step"], 2)

    def test_after_all_questions_moves_to_upload(self):
        # Simulate state with step=6 (all questions answered)
        state = self._make_state(
            intake_step=6,
            messages=[{"role": "user", "content": "72000"}],
        )
        result = intake_node(state)
        self.assertEqual(result["current_node"], "upload")

    def test_parses_risk_profile_c_as_growth(self):
        state = self._make_state(
            intake_step=5,
            messages=[
                {"role": "assistant", "content": "Q5"},
                {"role": "user", "content": "C"},
            ],
        )
        result = intake_node(state)
        self.assertEqual(result.get("risk_profile"), "growth")

    def test_parses_risk_profile_a_as_conservative(self):
        state = self._make_state(
            intake_step=5,
            messages=[
                {"role": "assistant", "content": "Q5"},
                {"role": "user", "content": "A"},
            ],
        )
        result = intake_node(state)
        self.assertEqual(result.get("risk_profile"), "conservative")


# ── Analysis Node ─────────────────────────────────────────────────────────────

class AnalysisNodeTest(unittest.TestCase):
    def _make_holding(self, ticker="VWCE.DE", asset_type="etf_acc",
                      units=10.0, avg_price=100.0):
        return {
            "ticker": ticker, "isin": "", "asset_type": asset_type,
            "units": units, "avg_purchase_price": avg_price,
            "purchase_date": "", "current_price": 0.0,
            "current_value": 0.0, "unrealised_gain": 0.0, "unrealised_gain_pct": 0.0,
        }

    @patch("agent.nodes.get_prices")
    def test_calculates_gain_correctly(self, mock_prices):
        mock_prices.return_value = {"VWCE.DE": 120.0}
        state = {
            "holdings": [self._make_holding("VWCE.DE", units=10, avg_price=100.0)],
            "is_married": False,
        }
        result = analysis_node(state)
        h = result["holdings"][0]
        self.assertAlmostEqual(h["current_price"], 120.0)
        self.assertAlmostEqual(h["current_value"], 1200.0)
        self.assertAlmostEqual(h["unrealised_gain"], 200.0)
        self.assertAlmostEqual(h["unrealised_gain_pct"], 20.0)

    @patch("agent.nodes.get_prices")
    def test_calculates_portfolio_totals(self, mock_prices):
        mock_prices.return_value = {"VWCE.DE": 120.0, "AAPL": 200.0}
        state = {
            "holdings": [
                self._make_holding("VWCE.DE", units=10, avg_price=100.0),
                self._make_holding("AAPL", asset_type="stock", units=5, avg_price=150.0),
            ],
            "is_married": False,
        }
        result = analysis_node(state)
        self.assertAlmostEqual(result["total_invested"], 1750.0)
        self.assertAlmostEqual(result["total_current_value"], 2200.0)
        self.assertAlmostEqual(result["total_unrealised_gain"], 450.0)

    @patch("agent.nodes.get_prices")
    def test_empty_holdings_returns_zero_totals(self, mock_prices):
        mock_prices.return_value = {}
        result = analysis_node({"holdings": [], "is_married": False})
        self.assertEqual(result["total_invested"], 0.0)
        self.assertEqual(result["total_current_value"], 0.0)
        self.assertEqual(result["total_unrealised_gain"], 0.0)

    @patch("agent.nodes.get_prices")
    def test_negative_gain_not_taxed(self, mock_prices):
        mock_prices.return_value = {"AAPL": 50.0}
        state = {
            "holdings": [self._make_holding("AAPL", asset_type="stock",
                                            units=10, avg_price=100.0)],
            "is_married": False,
        }
        result = analysis_node(state)
        tax_pos = result["tax"]["positions"][0]
        self.assertEqual(tax_pos["tax_if_sold_now"], 0.0)

    @patch("agent.nodes.get_prices")
    def test_exit_tax_warning_triggered_above_threshold(self, mock_prices):
        mock_prices.return_value = {"BIG.DE": 100.0}
        state = {
            "holdings": [self._make_holding("BIG.DE", units=6000, avg_price=100.0)],
            "is_married": False,
        }
        result = analysis_node(state)
        self.assertTrue(result["tax"]["exit_tax_warning"])

    @patch("agent.nodes.get_prices")
    def test_married_gets_2000_allowance(self, mock_prices):
        mock_prices.return_value = {}
        result = analysis_node({"holdings": [], "is_married": True})
        self.assertEqual(result["tax"]["sparerpauschbetrag_remaining"], 2000.0)

    @patch("agent.nodes.get_prices")
    def test_vorabpauschale_only_for_etf_acc(self, mock_prices):
        mock_prices.return_value = {"STOCK": 100.0}
        state = {
            "holdings": [self._make_holding("STOCK", asset_type="stock",
                                            units=10, avg_price=80.0)],
            "is_married": False,
        }
        result = analysis_node(state)
        tax_pos = result["tax"]["positions"][0]
        self.assertEqual(tax_pos["vorabpauschale_annual"], 0.0)
        self.assertEqual(result["tax"]["vorabpauschale_total_estimate"], 0.0)


# ── Validators: ETF / UCITS suitability gate ─────────────────────────────────

from agent.validators import (
    validate_etf_suggestions, validate_plan_alignment,
)


class ValidateEtfSuggestionsTest(TestCase):
    def test_eu_suffix_ticker_has_no_warning(self):
        out = validate_etf_suggestions([{"ticker": "VWCE.DE", "exchange": "XETRA"}])
        self.assertNotIn("warning", out[0])

    def test_known_us_etf_flagged_as_non_ucits(self):
        out = validate_etf_suggestions([{"ticker": "SPY", "exchange": "NYSEARCA"}])
        self.assertIn("warning", out[0])
        self.assertIn("UCITS", out[0]["warning"])

    def test_valid_exchange_without_eu_suffix_passes(self):
        out = validate_etf_suggestions([{"ticker": "VUSA", "exchange": "LSE"}])
        self.assertNotIn("warning", out[0])

    def test_unknown_listing_gets_verify_warning(self):
        out = validate_etf_suggestions([{"ticker": "ZZZZ", "exchange": "NASDAQ"}])
        self.assertIn("warning", out[0])
        self.assertIn("verify", out[0]["warning"].lower())

    def test_returns_same_list_object(self):
        suggestions = [{"ticker": "VWCE.DE", "exchange": "XETRA"}]
        self.assertIs(validate_etf_suggestions(suggestions), suggestions)


class ValidatePlanAlignmentTest(TestCase):
    def _plan(self, *cats):
        return {"categories": [{"name": n, "allocation_pct": p} for n, p in cats]}

    def test_aligned_balanced_plan_has_no_warnings(self):
        plan = self._plan(("Core World ETF", 60), ("Euro Bonds", 40))
        self.assertEqual(validate_plan_alignment(plan, "balanced"), [])

    def test_conservative_with_high_equity_warns(self):
        plan = self._plan(("World Equity", 80), ("Bonds", 20))
        warnings = validate_plan_alignment(plan, "conservative")
        self.assertTrue(warnings)
        self.assertIn("high", warnings[0])

    def test_growth_with_low_equity_warns(self):
        plan = self._plan(("World Equity", 40), ("Bonds", 60))
        warnings = validate_plan_alignment(plan, "growth")
        self.assertTrue(warnings)
        self.assertIn("low", warnings[0])

    def test_empty_plan_returns_no_warnings(self):
        self.assertEqual(validate_plan_alignment(None, "conservative"), [])

    def test_unknown_risk_falls_back_to_balanced_band(self):
        plan = self._plan(("World Equity", 70), ("Bonds", 30))
        self.assertEqual(validate_plan_alignment(plan, "mystery"), [])


# ── Shared type catalog + Teilfreistellung helpers ────────────────────────────

class TypeCatalogTest(unittest.TestCase):
    def test_get_available_types_returns_asset_types_and_categories(self):
        from agent.tools import get_available_types
        result = get_available_types()
        self.assertIn("asset_types", result)
        self.assertIn("categories", result)
        self.assertTrue(result["asset_types"])
        self.assertTrue(result["categories"])

    def test_asset_type_values_match_holding_choices(self):
        from agent import catalog
        # The shared catalog must stay in lock-step with the model's DB values.
        self.assertEqual(
            {t["value"] for t in catalog.ASSET_TYPES},
            set(TEILFREISTELLUNG.keys()),
        )

    def test_is_equity_category_known_and_unknown(self):
        from agent import catalog
        self.assertTrue(catalog.is_equity_category("Core World ETF"))
        self.assertFalse(catalog.is_equity_category("Bonds / Stability"))
        self.assertIsNone(catalog.is_equity_category("Some Made-Up Bucket"))

    def test_plan_alignment_uses_catalog_category_equity_flag(self):
        # "Bonds / Stability" is non-equity in the catalog, so a conservative
        # plan dominated by it must NOT trip the high-equity warning.
        plan = {"categories": [
            {"name": "Core World ETF", "allocation_pct": 40},
            {"name": "Bonds / Stability", "allocation_pct": 60},
        ]}
        self.assertEqual(validate_plan_alignment(plan, "conservative"), [])


class TeilfreistellungHelpersTest(unittest.TestCase):
    def test_pct_equity_etf_is_30(self):
        from agent.tax_engine import teilfreistellung_pct
        self.assertAlmostEqual(teilfreistellung_pct("etf_acc"), 0.30)
        self.assertAlmostEqual(teilfreistellung_pct("etf_dist"), 0.30)

    def test_pct_stock_and_unknown_are_zero(self):
        from agent.tax_engine import teilfreistellung_pct
        self.assertEqual(teilfreistellung_pct("stock"), 0.0)
        self.assertEqual(teilfreistellung_pct("mystery"), 0.0)

    def test_note_mentions_exemption_for_etf(self):
        from agent.tax_engine import teilfreistellung_note
        self.assertIn("exempt", teilfreistellung_note("etf_acc").lower())

    def test_note_for_stock_says_no_exemption(self):
        from agent.tax_engine import teilfreistellung_note
        self.assertIn("no teilfreistellung", teilfreistellung_note("stock").lower())
