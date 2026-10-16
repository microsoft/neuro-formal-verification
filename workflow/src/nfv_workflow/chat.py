"""Workflow authentication policy around the unchanged NFV model client."""

from copilot import CopilotClient

from nfv.shared.copilot_client import CopilotChat


class WorkflowChat(CopilotChat):
    async def start(self):
        if not self._provider:
            return await super().start()
        # BYOK authenticates at the proxy, not at Copilot's identity/model-list API.
        self._client = CopilotClient(log_level="error")
        await self._client.start()
