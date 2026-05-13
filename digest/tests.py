"""
Unit and integration tests for the digest app.
LangGraph graph and digest_node are mocked — no LLM calls or SQLite I/O.
"""
from unittest.mock import MagicMock, patch

from django.test import TestCase, Client


def _make_mock_state(messages=None):
    state = MagicMock()
    state.values = {"messages": messages or []}
    return state


# ── digest_page view ──────────────────────────────────────────────────────────

class DigestPageNoDigestTest(TestCase):
    def setUp(self):
        self.client = Client()

    @patch("digest.views.graph")
    def test_returns_200(self, mock_graph):
        mock_graph.get_state.return_value = _make_mock_state([])
        response = self.client.get("/digest/")
        self.assertEqual(response.status_code, 200)

    @patch("digest.views.graph")
    def test_digest_none_when_no_messages(self, mock_graph):
        mock_graph.get_state.return_value = _make_mock_state([])
        response = self.client.get("/digest/")
        self.assertIsNone(response.context["digest"])

    @patch("digest.views.graph")
    def test_digest_none_when_state_unavailable(self, mock_graph):
        mock_graph.get_state.side_effect = Exception("no state")
        response = self.client.get("/digest/")
        self.assertIsNone(response.context["digest"])


class DigestPageFindsLastDigestTest(TestCase):
    """digest_page should return the most recent assistant message that looks like a digest."""

    def setUp(self):
        self.client = Client()

    @patch("digest.views.graph")
    def test_finds_long_assistant_message_with_keywords(self, mock_graph):
        digest_text = (
            "Portfolio summary: Your ETF allocation is solid. "
            "Tax allowance used: €0 of €1,000. "
            "Educational note: Vorabpauschale applies to accumulating ETFs. " * 5
        )
        messages = [
            {"role": "user",      "content": "generate digest"},
            {"role": "assistant", "content": digest_text},
        ]
        mock_graph.get_state.return_value = _make_mock_state(messages)
        response = self.client.get("/digest/")
        self.assertEqual(response.context["digest"], digest_text)

    @patch("digest.views.graph")
    def test_ignores_short_assistant_messages(self, mock_graph):
        messages = [
            {"role": "assistant", "content": "Short reply."},
        ]
        mock_graph.get_state.return_value = _make_mock_state(messages)
        response = self.client.get("/digest/")
        self.assertIsNone(response.context["digest"])

    @patch("digest.views.graph")
    def test_ignores_user_messages(self, mock_graph):
        long_user_msg = "portfolio " * 50  # long but role=user
        messages = [{"role": "user", "content": long_user_msg}]
        mock_graph.get_state.return_value = _make_mock_state(messages)
        response = self.client.get("/digest/")
        self.assertIsNone(response.context["digest"])

    @patch("digest.views.graph")
    def test_returns_latest_digest_when_multiple(self, mock_graph):
        old = "Portfolio update... tax allowance... educational note. " * 10
        new = "New portfolio summary... tax allowance used... allowance reset. " * 10
        messages = [
            {"role": "assistant", "content": old},
            {"role": "assistant", "content": new},
        ]
        mock_graph.get_state.return_value = _make_mock_state(messages)
        response = self.client.get("/digest/")
        self.assertEqual(response.context["digest"], new)


# ── generate_digest view ──────────────────────────────────────────────────────

class GenerateDigestTest(TestCase):
    def setUp(self):
        self.client = Client()

    @patch("digest.views.digest_node")
    @patch("digest.views.graph")
    def test_returns_200(self, mock_graph, mock_digest_node):
        mock_graph.get_state.return_value = _make_mock_state([])
        mock_digest_node.return_value = {
            "messages": [{"role": "assistant", "content": "Weekly digest here."}]
        }
        response = self.client.post("/digest/generate/")
        self.assertEqual(response.status_code, 200)

    @patch("digest.views.digest_node")
    @patch("digest.views.graph")
    def test_digest_text_in_response(self, mock_graph, mock_digest_node):
        mock_graph.get_state.return_value = _make_mock_state([])
        mock_digest_node.return_value = {
            "messages": [{"role": "assistant", "content": "Your weekly wrap-up."}]
        }
        response = self.client.post("/digest/generate/")
        self.assertIn(b"Your weekly wrap-up.", response.content)

    @patch("digest.views.digest_node")
    @patch("digest.views.graph")
    def test_html_characters_escaped(self, mock_graph, mock_digest_node):
        mock_graph.get_state.return_value = _make_mock_state([])
        mock_digest_node.return_value = {
            "messages": [{"role": "assistant", "content": "<b>Bold & important</b>"}]
        }
        response = self.client.post("/digest/generate/")
        self.assertNotIn(b"<b>", response.content)
        self.assertIn(b"&lt;b&gt;", response.content)
        self.assertIn(b"&amp;", response.content)

    @patch("digest.views.digest_node")
    @patch("digest.views.graph")
    def test_exception_returns_error_message(self, mock_graph, mock_digest_node):
        mock_graph.get_state.side_effect = Exception("db error")
        response = self.client.post("/digest/generate/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Could not generate digest", response.content)

    @patch("digest.views.digest_node")
    @patch("digest.views.graph")
    def test_digest_node_called_with_current_state(self, mock_graph, mock_digest_node):
        state_values = {"messages": [], "user_id": "demo"}
        mock_state = MagicMock()
        mock_state.values = state_values
        mock_graph.get_state.return_value = mock_state
        mock_digest_node.return_value = {
            "messages": [{"role": "assistant", "content": "digest"}]
        }
        self.client.post("/digest/generate/")
        mock_digest_node.assert_called_once_with(state_values)
