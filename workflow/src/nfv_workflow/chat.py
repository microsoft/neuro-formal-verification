"""Workflow authentication policy around the unchanged NFV model client."""

import os

from copilot import CopilotClient

from nfv.shared.copilot_client import CopilotChat
from nfv.shared.account import Account, AccountError, check_account

from .github import Abstain


class WorkflowChat(CopilotChat):
    async def start(self):
        if not self._provider and os.environ.get("GITHUB_ACTIONS") != "true":
            return await super().start()
        self._client = CopilotClient(log_level="error")
        await self._client.start()
        if self._provider:
            return
        # Do not fetch or print private entitlement/organization data in Actions.
        try:
            status = await self._client.get_auth_status()
            login = getattr(status, "login", None)
            if not status.isAuthenticated or not login:
                raise Abstain("Dedicated Copilot credential has no authenticated identity")
            self.account = Account(login=login)
            try:
                check_account(self.account, allow_emu=self._allow_emu)
            except AccountError:
                raise Abstain("Copilot identity violates NFV_EXPECTED_LOGIN / NFV_ALLOW_EMU policy") from None
            available = {model.id for model in await self._client.list_models()}
            if self._model not in available:
                raise Abstain("Configured NFV_MODEL is unavailable to the dedicated Copilot credential")
        except BaseException:
            await self.stop()
            raise
