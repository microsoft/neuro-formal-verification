#!/usr/bin/env python3
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""
Async wrapper around the Copilot SDK (github-copilot-sdk 1.0.4).

Deliberately minimal: the LLM in this pipeline only ever produces formal
artifacts, so this exposes a single `ask(system, prompt) -> str` with no tools
enabled. Every call runs in a fresh session, so nothing carries over between
entries — no accumulated context, and no chance of one entry's verdict
influencing another's translation.
"""

from __future__ import annotations

import asyncio
import contextvars
import sys as _sys
from contextlib import contextmanager
from dataclasses import dataclass, field

from copilot import CopilotClient
from copilot.generated.session_events import SessionEventType

from nfv.shared.account import Account, resolve_and_check


@dataclass
class LLMResponse:
    text: str
    model: str
    elapsed_s: float
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    nano_aiu: int = 0
    api_calls: int = 0


@dataclass
class UsageTotals:
    """Process-wide running total, so a run can report what it actually spent."""
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    nano_aiu: int = 0
    api_calls: int = 0
    # Transport-level retries (see CopilotChat.ask). Not a model behaviour; kept
    # so an arm whose numbers depended on flaky infrastructure is not silent.
    transport_retries: int = 0
    # Retries broken down by cause ("429", "capierror: 500", ...), so a rate
    # limit is distinguishable from a flaky socket without reading the log.
    transport_retry_kinds: dict = field(default_factory=dict)
    # Wall time attribution. `api_seconds` is time inside a call; `sem_wait_seconds`
    # is time queued behind the concurrency limit. Without the split a run that is
    # merely over-subscribed looks identical to a slow service.
    api_seconds: float = 0.0
    sem_wait_seconds: float = 0.0
    max_call_seconds: float = 0.0
    # Latest quota snapshot the server attached to a usage event. This is the
    # same figure the GitHub billing page renders, but read at call time, so it
    # is not subject to that page's reporting lag.
    used_requests: int | None = None
    remaining_percentage: float | None = None
    quota_reset_date: str | None = None

    def add(self, r: "LLMResponse") -> None:
        self.input_tokens += r.input_tokens
        self.output_tokens += r.output_tokens
        self.reasoning_tokens += r.reasoning_tokens
        self.cached_tokens += r.cached_tokens
        self.nano_aiu += r.nano_aiu
        self.api_calls += r.api_calls

    @property
    def aiu(self) -> float:
        """AI credits actually consumed (nano_aiu is billed in 1e-9 units)."""
        return self.nano_aiu / 1e9

    def render(self) -> str:
        lines = [
            f"api calls        : {self.api_calls}",
            f"input tokens     : {self.input_tokens:,}",
            f"output tokens    : {self.output_tokens:,}",
            f"reasoning tokens : {self.reasoning_tokens:,}",
            f"cached tokens    : {self.cached_tokens:,}",
            f"AI credits (AIU) : {self.aiu:.6f}",
        ]
        if self.api_calls:
            lines.append(f"api time         : {self.api_seconds:,.0f}s "
                         f"(mean {self.api_seconds / self.api_calls:.1f}s, "
                         f"max {self.max_call_seconds:.0f}s)")
            lines.append(f"queued on sem    : {self.sem_wait_seconds:,.0f}s")
        if self.transport_retries:
            kinds = ", ".join(f"{k}={v}" for k, v in
                              sorted(self.transport_retry_kinds.items()))
            lines.append(f"transport retries: {self.transport_retries} ({kinds})")
        if self.used_requests is not None:
            lines.append(f"quota used reqs  : {self.used_requests}")
        if self.remaining_percentage is not None:
            lines.append(f"quota remaining  : {self.remaining_percentage:.2f}%")
        if self.quota_reset_date:
            lines.append(f"quota resets     : {self.quota_reset_date}")
        return "\n".join(lines)


# Per-entry accounting. A ContextVar rather than a delta on the shared total,
# because entries run concurrently and deltas would interleave.
_scope: contextvars.ContextVar["UsageTotals | None"] = contextvars.ContextVar(
    "nfv_usage_scope", default=None)


# Transport retry policy. Deliberately small: the point is to survive a flaky
# response, not to paper over a service that is genuinely down.
_TRANSPORT_ATTEMPTS = 4
_TRANSPORT_BACKOFF = 2.0

# A call slower than this is reported as it happens. Chosen well above the
# observed healthy latency (a few seconds) so a healthy run stays quiet.
_SLOW_CALL_S = 120.0

# Observed intermittently on a model that `list_models` reports as available:
#   CAPIError: 400 The requested model is not supported.
# It is not deterministic -- the same prompt succeeds on retry -- so treating it
# as a verification outcome would be simply wrong.
_TRANSIENT = (
    "the requested model is not supported",
    "capierror: 500", "capierror: 502", "capierror: 503", "capierror: 504",
    "429", "rate limit", "too many requests",
    "temporarily unavailable", "service unavailable",
    "connection reset", "connection aborted", "server disconnected",
)


def _is_transient(exc: BaseException) -> bool:
    """True for failures that carry no information about the program under test.

    A timeout is deliberately excluded: it is a real budget being exceeded and
    must stay visible as such.
    """
    if isinstance(exc, TimeoutError):
        return False
    msg = f"{type(exc).__name__}: {exc}".lower()
    return any(t in msg for t in _TRANSIENT)


def _transient_kind(exc: BaseException) -> str:
    """Which transient pattern matched, so retries can be counted by cause."""
    msg = f"{type(exc).__name__}: {exc}".lower()
    for t in _TRANSIENT:
        if t in msg:
            return t
    return "other"


@contextmanager
def usage_scope():
    """Accumulate the usage of every `ask` made inside this block."""
    totals = UsageTotals()
    token = _scope.set(totals)
    try:
        yield totals
    finally:
        _scope.reset(token)


def new_usage_scope() -> UsageTotals:
    """Start a usage scope for the current asyncio task and return its totals.

    Each entry runs as its own Task, and every Task gets a copy of the context,
    so the binding is task-local and dies with the task -- no explicit reset and
    no interleaving between concurrently running entries.
    """
    totals = UsageTotals()
    _scope.set(totals)
    return totals


class CopilotChat:
    """Shared client; each `ask` gets its own stateless session."""
    def __init__(self, model: str = "gpt-5.5", concurrency: int = 4,
                 allow_emu: bool | None = None, provider: dict | None = None):
        self._model = model
        # Optional BYOK provider (SDK ProviderConfig); bypasses Copilot API auth.
        self._provider = provider
        self._client: CopilotClient | None = None
        self._sem = asyncio.Semaphore(concurrency)
        self._allow_emu = allow_emu
        self.usage = UsageTotals()
        self.account: Account | None = None

    async def start(self) -> None:
        self._client = CopilotClient(log_level="error")
        await self._client.start()
        # Identity first: the SDK bills and applies policy per the *active* gh
        # account, so an unexpected or EMU account must stop the run before it
        # spends anything, not be discovered in the log afterwards.
        try:
            status = await self._client.get_auth_status()
            login = getattr(status, "login", None)
        except Exception:
            login = None
        try:
            self.account = resolve_and_check(login, allow_emu=self._allow_emu)
        except Exception:
            await self._client.stop()
            self._client = None
            raise
        # Fail loudly at startup rather than 232 times mid-run.
        try:
            available = [getattr(m, "id", str(m)) for m in await self._client.list_models()]
        except Exception:
            return
        if available and self._model not in available:
            await self._client.stop()
            self._client = None
            raise ValueError(
                f"model {self._model!r} not available; choose one of: {', '.join(sorted(available))}"
            )

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.stop()
            self._client = None

    async def __aenter__(self) -> "CopilotChat":
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.stop()

    async def ask(self, system: str, prompt: str, timeout: float = 180.0) -> LLMResponse:
        """Ask once, retrying only *transport* failures.

        An API error is not a verification outcome: if the service returns
        "model is not supported" or a 5xx, letting it propagate turns an entry
        into an `abstain` and silently understates the arm. These retries
        therefore protect the measurement rather than alter it -- no prompt,
        model or verifier behaviour changes, and the count is recorded in
        `usage.transport_retries` so the cost stays visible.
        """
        if self._client is None:
            raise RuntimeError("client not started; use 'async with CopilotChat(...)'")

        import asyncio as _asyncio

        last: Exception | None = None
        for attempt in range(_TRANSPORT_ATTEMPTS):
            try:
                return await self._ask_once(system, prompt, timeout)
            except Exception as exc:
                if not _is_transient(exc) or attempt == _TRANSPORT_ATTEMPTS - 1:
                    raise
                last = exc
                kind = _transient_kind(exc)
                self.usage.transport_retries += 1
                self.usage.transport_retry_kinds[kind] = (
                    self.usage.transport_retry_kinds.get(kind, 0) + 1)
                _scoped = _scope.get()
                if _scoped is not None:
                    _scoped.transport_retries += 1
                    _scoped.transport_retry_kinds[kind] = (
                        _scoped.transport_retry_kinds.get(kind, 0) + 1)
                delay = _TRANSPORT_BACKOFF * (2 ** attempt)
                # Printed, not just counted: a rate limit that only shows up as
                # a bigger wall_s is indistinguishable from a slow model, which
                # is exactly the confusion this line exists to prevent.
                print(f"[transport] retry {attempt + 1}/{_TRANSPORT_ATTEMPTS} "
                      f"after {kind}: {type(exc).__name__}: {str(exc)[:160]} "
                      f"(sleeping {delay:.0f}s)", file=_sys.stderr, flush=True)
                await _asyncio.sleep(delay)
        raise last  # unreachable; kept so the contract is explicit

    async def _ask_once(self, system: str, prompt: str, timeout: float = 180.0) -> LLMResponse:
        import time

        queued = time.time()
        async with self._sem:
            start = time.time()
            waited = start - queued
            for _t in (self.usage, _scope.get()):
                if _t is not None:
                    _t.sem_wait_seconds += waited
            extra = {"provider": self._provider} if self._provider else {}
            session = await self._client.create_session(
                model=self._model,
                system_message={"mode": "replace", "content": system},
                available_tools=[],
                **extra,
            )
            chunks: list[str] = []
            used = {"in": 0, "out": 0, "reason": 0, "cache": 0, "aiu": 0, "calls": 0}

            def _on_event(event) -> None:
                if event.type == SessionEventType.ASSISTANT_MESSAGE:
                    content = getattr(event.data, "content", None)
                    if content:
                        chunks.append(content)
                elif event.type == SessionEventType.ASSISTANT_USAGE:
                    d = event.data
                    used["calls"] += 1
                    used["in"] += getattr(d, "input_tokens", 0) or 0
                    used["out"] += getattr(d, "output_tokens", 0) or 0
                    used["reason"] += getattr(d, "reasoning_tokens", 0) or 0
                    used["cache"] += getattr(d, "cache_read_tokens", 0) or 0
                    cu = getattr(d, "copilot_usage", None)
                    if cu is not None:
                        used["aiu"] += getattr(cu, "total_nano_aiu", 0) or 0
                    snaps = getattr(d, "_quota_snapshots", None) or {}
                    # Only the metered quota is meaningful: `chat` and
                    # `completions` report unlimited/0 and, being sent last on
                    # some responses, would otherwise overwrite the real figure.
                    premium = None
                    if hasattr(snaps, "get"):
                        premium = snaps.get("premium_interactions")
                    if premium is not None:
                        ur = getattr(premium, "_used_requests", None)
                        if ur is not None:
                            self.usage.used_requests = ur
                        rp = getattr(premium, "_remaining_percentage", None)
                        if rp is not None:
                            self.usage.remaining_percentage = rp
                        rd = getattr(premium, "_reset_date", None)
                        if rd is not None:
                            self.usage.quota_reset_date = str(rd)

            unsubscribe = session.on(_on_event)
            try:
                await session.send_and_wait(prompt, timeout=timeout)
            finally:
                try:
                    unsubscribe()
                except Exception:
                    pass
                try:
                    await session.disconnect()
                except Exception:
                    pass

            resp = LLMResponse(
                text="\n".join(chunks).strip(),
                model=self._model,
                elapsed_s=time.time() - start,
                input_tokens=used["in"],
                output_tokens=used["out"],
                reasoning_tokens=used["reason"],
                cached_tokens=used["cache"],
                nano_aiu=used["aiu"],
                api_calls=used["calls"],
            )
            self.usage.add(resp)
            scoped = _scope.get()
            if scoped is not None:
                scoped.add(resp)
            for _t in (self.usage, scoped):
                if _t is not None:
                    _t.api_seconds += resp.elapsed_s
                    _t.max_call_seconds = max(_t.max_call_seconds, resp.elapsed_s)
            if resp.elapsed_s >= _SLOW_CALL_S:
                print(f"[transport] slow call {resp.elapsed_s:.0f}s "
                      f"(queued {waited:.0f}s, out {resp.output_tokens} tok)",
                      file=_sys.stderr, flush=True)
            return resp


async def _selftest() -> int:
    async with CopilotChat(model="gpt-5.5", concurrency=1) as chat:
        resp = await chat.ask(
            "You reply with exactly one word and nothing else.",
            "Reply with the single word: ready",
        )
        print(f"model   : {resp.model}")
        print(f"elapsed : {resp.elapsed_s:.1f}s")
        print(f"response: {resp.text!r}")
        print("--- usage ---")
        print(chat.usage.render())
        if chat.usage.api_calls == 0:
            print("WARNING: no ASSISTANT_USAGE events observed; accounting is blind")
        return 0 if resp.text else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_selftest()))
