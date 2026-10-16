import asyncio
from contextlib import redirect_stdout
import io
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch


class WorkflowChatTests(unittest.TestCase):
    def test_actions_direct_auth_checks_identity_without_private_entitlement_probes(self):
        from nfv_workflow.chat import WorkflowChat
        client = SimpleNamespace(start=AsyncMock(), stop=AsyncMock(),
                                 get_auth_status=AsyncMock(return_value=SimpleNamespace(
                                     login="test-user", isAuthenticated=True)),
                                 list_models=AsyncMock(return_value=[SimpleNamespace(id="test-model")]))
        stream = io.StringIO()
        async def exercise():
            async with WorkflowChat(model="test-model") as chat:
                self.assertEqual(chat.account.login, "test-user")
                self.assertIsNone(chat.account.copilot_plan)
                self.assertEqual(chat.account.orgs, [])
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}, clear=True), \
                patch("nfv_workflow.chat.CopilotClient", return_value=client), \
                patch("nfv.shared.account.detect_account", side_effect=AssertionError("Private metadata")), \
                redirect_stdout(stream):
            asyncio.run(exercise())
        self.assertEqual(stream.getvalue(), "")
        client.get_auth_status.assert_awaited_once()
        client.list_models.assert_awaited_once()
        client.stop.assert_awaited_once()

    def test_actions_auth_failure_stops_client_and_does_not_expose_identity(self):
        from nfv_workflow.chat import WorkflowChat
        from nfv_workflow.github import Abstain
        for login, authenticated, policy in (("", False, {}),
                                             ("test-user", True, {"NFV_EXPECTED_LOGIN": "different"})):
            with self.subTest(login=login):
                client = SimpleNamespace(start=AsyncMock(), stop=AsyncMock(),
                                         get_auth_status=AsyncMock(return_value=SimpleNamespace(
                                             login=login, isAuthenticated=authenticated)))
                with patch.dict(os.environ, {"GITHUB_ACTIONS": "true", **policy}, clear=True), \
                        patch("nfv_workflow.chat.CopilotClient", return_value=client):
                    with self.assertRaises(Abstain) as error:
                        asyncio.run(WorkflowChat(model="test-model").start())
                self.assertNotIn("test-user", str(error.exception))
                client.stop.assert_awaited_once()

    def test_proxy_requests_use_fresh_tool_free_sessions_without_copilot_auth_probes(self):
        try:
            from copilot.generated.session_events import SessionEventType
            from nfv_workflow.chat import WorkflowChat
        except ImportError:
            self.skipTest("Copilot SDK is not installed")

        sessions, requests = [], []
        class Session:
            def on(self, callback):
                self.callback = callback
                return lambda: None
            async def send_and_wait(self, prompt, timeout):
                self.callback(SimpleNamespace(type=SessionEventType.ASSISTANT_MESSAGE,
                                               data=SimpleNamespace(content=prompt)))
            async def disconnect(self):
                self.closed = True
        class Client:
            start = AsyncMock()
            stop = AsyncMock()
            get_auth_status = AsyncMock(side_effect=AssertionError("Not a Copilot-authenticated client"))
            list_models = AsyncMock(side_effect=AssertionError("Not a Copilot-authenticated client"))
            async def create_session(self, **kwargs):
                requests.append(kwargs)
                session = Session()
                sessions.append(session)
                return session
        client = Client()
        provider = {"type": "openai", "base_url": "http://api-proxy:10002",
                    "api_key": "placeholder"}
        async def exercise():
            async with WorkflowChat(model="test-model", concurrency=1, provider=provider) as chat:
                first = await chat.ask("first system", "first property")
                second = await chat.ask("second system", "second property")
                self.assertEqual(first.text, "first property")
                self.assertEqual(second.text, "second property")
        with patch("nfv_workflow.chat.CopilotClient", return_value=client):
            asyncio.run(exercise())
        self.assertEqual(len(sessions), 2)
        self.assertIsNot(sessions[0], sessions[1])
        self.assertTrue(all(session.closed for session in sessions))
        self.assertEqual([request["system_message"]["content"] for request in requests],
                         ["first system", "second system"])
        for request in requests:
            self.assertEqual(request["system_message"]["mode"], "replace")
            self.assertEqual(request["available_tools"], [])
            self.assertEqual(request["provider"], provider)
        client.get_auth_status.assert_not_called()
        client.list_models.assert_not_called()
