"""
Unit and integration tests for the chat app.
The LangGraph graph is mocked so no SQLite checkpoints or LLM calls are made.
"""
from unittest.mock import MagicMock, patch

from django.test import TestCase, Client


def _make_mock_state(messages=None):
    state = MagicMock()
    state.values = {"messages": messages or []}
    return state


# ── _to_dict helper ──────────────────────────────────────────────────────────

class ToDictTest(TestCase):
    def test_passthrough_for_plain_dict(self):
        from chat.views import _to_dict
        msg = {"role": "user", "content": "hello"}
        self.assertEqual(_to_dict(msg), msg)

    def test_ai_message_maps_to_assistant(self):
        from chat.views import _to_dict
        msg = MagicMock()
        msg.type = "ai"
        msg.content = "Hi there"
        result = _to_dict(msg)
        self.assertEqual(result["role"], "assistant")
        self.assertEqual(result["content"], "Hi there")

    def test_human_message_maps_to_user(self):
        from chat.views import _to_dict
        msg = MagicMock()
        msg.type = "human"
        msg.content = "Question?"
        result = _to_dict(msg)
        self.assertEqual(result["role"], "user")


# ── chat_page view ────────────────────────────────────────────────────────────

class ChatPageExistingMessagesTest(TestCase):
    """When messages already exist in graph state, render them without re-invoking."""

    def setUp(self):
        self.client = Client()
        messages = [
            {"role": "assistant", "content": "Welcome! What is your name?"},
            {"role": "user", "content": "Robin"},
        ]
        self.mock_state = _make_mock_state(messages)

    @patch("chat.views.graph")
    def test_returns_200(self, mock_graph):
        mock_graph.get_state.return_value = self.mock_state
        response = self.client.get("/chat/")
        self.assertEqual(response.status_code, 200)

    @patch("chat.views.graph")
    def test_messages_in_context(self, mock_graph):
        mock_graph.get_state.return_value = self.mock_state
        response = self.client.get("/chat/")
        self.assertEqual(len(response.context["messages"]), 2)

    @patch("chat.views.graph")
    def test_does_not_invoke_when_messages_exist(self, mock_graph):
        mock_graph.get_state.return_value = self.mock_state
        self.client.get("/chat/")
        mock_graph.invoke.assert_not_called()


class ChatPageFirstLoadTest(TestCase):
    """On first load (no messages), graph is invoked twice to bootstrap intake."""

    def setUp(self):
        self.client = Client()

    @patch("chat.views.graph")
    def test_graph_invoked_twice_on_first_load(self, mock_graph):
        empty_state = _make_mock_state([])
        seeded_state = _make_mock_state([
            {"role": "assistant", "content": "How much do you earn?"}
        ])
        mock_graph.get_state.side_effect = [empty_state, seeded_state]
        mock_graph.invoke.return_value = None
        self.client.get("/chat/")
        self.assertEqual(mock_graph.invoke.call_count, 2)

    @patch("chat.views.graph")
    def test_second_invoke_called_with_none(self, mock_graph):
        empty_state = _make_mock_state([])
        seeded_state = _make_mock_state([
            {"role": "assistant", "content": "First intake question"}
        ])
        mock_graph.get_state.side_effect = [empty_state, seeded_state]
        mock_graph.invoke.return_value = None
        self.client.get("/chat/")
        # Second call should be invoke(None, config)
        second_call_args = mock_graph.invoke.call_args_list[1]
        self.assertIsNone(second_call_args[0][0])


# ── send_message view ─────────────────────────────────────────────────────────

class SendMessageTest(TestCase):
    def setUp(self):
        self.client = Client()

    @patch("chat.views.graph")
    def test_empty_message_returns_empty_response(self, mock_graph):
        response = self.client.post("/chat/message/", {"message": "   "})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"")
        mock_graph.invoke.assert_not_called()

    @patch("chat.views.graph")
    def test_valid_message_invokes_graph_twice(self, mock_graph):
        before = _make_mock_state([{"role": "assistant", "content": "Q1?"}])
        after = _make_mock_state([
            {"role": "assistant", "content": "Q1?"},
            {"role": "user",      "content": "My answer"},
            {"role": "assistant", "content": "Q2?"},
        ])
        mock_graph.get_state.side_effect = [before, after]
        mock_graph.invoke.return_value = None

        self.client.post("/chat/message/", {"message": "My answer"})
        self.assertEqual(mock_graph.invoke.call_count, 2)

    @patch("chat.views.graph")
    def test_returns_only_new_messages(self, mock_graph):
        before = _make_mock_state([{"role": "assistant", "content": "Q1?"}])
        after = _make_mock_state([
            {"role": "assistant", "content": "Q1?"},
            {"role": "user",      "content": "answer"},
            {"role": "assistant", "content": "Q2?"},
        ])
        mock_graph.get_state.side_effect = [before, after]
        mock_graph.invoke.return_value = None

        response = self.client.post("/chat/message/", {"message": "answer"})
        # Should use message.html partial
        self.assertTemplateUsed(response, "chat/message.html")
        # Only 2 new messages (user + assistant), not all 3
        self.assertEqual(len(response.context["messages"]), 2)

    @patch("chat.views.graph")
    def test_user_message_merged_into_first_invoke(self, mock_graph):
        mock_graph.get_state.return_value = _make_mock_state([])
        mock_graph.invoke.return_value = None

        self.client.post("/chat/message/", {"message": "hello"})
        first_call_args = mock_graph.invoke.call_args_list[0][0][0]
        messages = first_call_args.get("messages", [])
        self.assertTrue(any(
            (m.get("content") == "hello" if isinstance(m, dict) else getattr(m, "content", "") == "hello")
            for m in messages
        ))


# ── reset_session view ────────────────────────────────────────────────────────

class ResetSessionTest(TestCase):
    def setUp(self):
        self.client = Client()

    @patch("chat.views.graph")
    @patch("sqlite3.connect")
    def test_clears_checkpoints_for_thread(self, mock_connect, mock_graph):
        mock_conn = MagicMock()
        mock_connect.return_value = mock_conn
        mock_graph.invoke.return_value = None

        self.client.post("/chat/reset/")

        # DELETE executed on checkpoint tables
        calls = [str(c) for c in mock_conn.execute.call_args_list]
        self.assertTrue(any("checkpoints" in c for c in calls))

    @patch("chat.views.graph")
    @patch("sqlite3.connect")
    def test_reinitialises_graph_after_reset(self, mock_connect, mock_graph):
        mock_conn = MagicMock()
        mock_connect.return_value = mock_conn
        mock_graph.invoke.return_value = None

        self.client.post("/chat/reset/")
        self.assertEqual(mock_graph.invoke.call_count, 2)

    @patch("chat.views.graph")
    @patch("sqlite3.connect")
    def test_does_not_delete_django_db_records(self, mock_connect, mock_graph):
        """Reset must only clear LangGraph checkpoint data, not Django models."""
        from portfolio.models import UserProfile
        UserProfile.objects.create(user_id="demo")

        mock_conn = MagicMock()
        mock_connect.return_value = mock_conn
        mock_graph.invoke.return_value = None

        self.client.post("/chat/reset/")
        # Django DB record still exists
        self.assertEqual(UserProfile.objects.filter(user_id="demo").count(), 1)
