#!/usr/bin/env python3
"""
Which GitHub account is paying for, and shaping, a run.

The Copilot SDK does not hold its own credential: `authType: gh-cli` means it
resolves whichever `gh` account is *active* at client-start time. A machine with
several accounts logged in (a personal one and an Enterprise Managed User) will
therefore silently switch identity between runs after a `gh auth switch`, moving
the billing and -- more importantly for the results -- changing which models are
available and which policies are applied. A run logged without its identity is
not reproducible, so this module resolves the account up front and records it.

EMU detection is not a heuristic guess. GitHub usernames may contain only
alphanumerics and single hyphens, so an underscore is impossible in a normal
login; EMU accounts are provisioned as `<idp-username>_<enterprise-shortcode>`
and are the only logins on github.com that contain one.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field

# Set to a login to pin a run to one account; a mismatch aborts.
ENV_EXPECTED_LOGIN = "NFV_EXPECTED_LOGIN"
# Set to 1 to permit a run on an EMU account (it is refused by default).
ENV_ALLOW_EMU = "NFV_ALLOW_EMU"


class AccountError(RuntimeError):
    """The active account is not the one this run is allowed to use."""


def is_emu_login(login: str) -> bool:
    """True if `login` is an Enterprise Managed User.

    GitHub restricts usernames to alphanumerics and single hyphens, so the
    underscore in an EMU's `<user>_<shortcode>` is a positive identification
    rather than a guess.
    """
    return "_" in login


def emu_shortcode(login: str) -> str | None:
    return login.rsplit("_", 1)[1] if is_emu_login(login) else None


@dataclass
class Account:
    login: str
    auth_type: str | None = None
    host: str | None = None
    copilot_plan: str | None = None
    sku: str | None = None
    orgs: list[str] = field(default_factory=list)
    api_endpoint: str | None = None
    # Populated only when the Copilot entitlement endpoint is reachable.
    quota_entitlement: int | None = None
    quota_remaining: int | None = None
    quota_percent_remaining: float | None = None
    quota_reset: str | None = None

    @property
    def is_emu(self) -> bool:
        return is_emu_login(self.login)

    @property
    def shortcode(self) -> str | None:
        return emu_shortcode(self.login)

    def one_line(self) -> str:
        bits = [self.login]
        if self.copilot_plan:
            bits.append(self.copilot_plan)
        if self.orgs:
            bits.append("orgs: " + ",".join(self.orgs))
        tag = f"EMU ACCOUNT (shortcode: {self.shortcode})" if self.is_emu else "non-EMU"
        return f"{' | '.join(bits)} [{tag}]"

    def render(self) -> str:
        lines = [
            f"login            : {self.login}",
            f"account type     : {'EMU (enterprise managed user)' if self.is_emu else 'non-EMU'}",
        ]
        if self.is_emu:
            lines.append(f"emu shortcode    : {self.shortcode}")
        if self.auth_type:
            lines.append(f"auth             : {self.auth_type} @ {self.host}")
        if self.copilot_plan:
            lines.append(f"copilot plan     : {self.copilot_plan}")
        if self.sku:
            lines.append(f"sku              : {self.sku}")
        if self.orgs:
            lines.append(f"organizations    : {', '.join(self.orgs)}")
        if self.api_endpoint:
            lines.append(f"copilot endpoint : {self.api_endpoint}")
        if self.quota_remaining is not None:
            lines.append(
                f"premium quota    : {self.quota_remaining:,} of "
                f"{self.quota_entitlement:,} left"
                + (f" ({self.quota_percent_remaining:.1f}%)"
                   if self.quota_percent_remaining is not None else "")
            )
        if self.quota_reset:
            lines.append(f"quota resets     : {self.quota_reset}")
        return "\n".join(lines)


def _gh_json(path: str) -> dict | None:
    """GET a GitHub API path via `gh`, or None if unavailable."""
    try:
        p = subprocess.run(["gh", "api", path], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0 or not p.stdout.strip():
        return None
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        return None


def detect_account(login: str | None = None) -> Account | None:
    """Resolve the account the Copilot SDK will bill this run to.

    `login` should come from the SDK's own auth status when available, since
    that is the identity the SDK actually uses; the REST calls only enrich it.
    Returns None when the account cannot be determined at all, so a caller can
    distinguish "not EMU" from "unknown".
    """
    entitlement = _gh_json("/copilot_internal/user") or {}
    if login is None:
        login = entitlement.get("login") or (_gh_json("/user") or {}).get("login")
    if not login:
        return None

    acct = Account(login=login)
    acct.copilot_plan = entitlement.get("copilot_plan")
    acct.sku = entitlement.get("access_type_sku")
    acct.orgs = list(entitlement.get("organization_login_list") or [])
    acct.api_endpoint = (entitlement.get("endpoints") or {}).get("api")
    acct.quota_reset = entitlement.get("quota_reset_date_utc") or entitlement.get("quota_reset_date")

    # The premium_interactions snapshot is the metered one; `chat` and
    # `completions` report `unlimited` and would mask an exhausted quota.
    premium = (entitlement.get("quota_snapshots") or {}).get("premium_interactions") or {}
    if premium:
        ent = premium.get("entitlement")
        rem = premium.get("remaining")
        acct.quota_entitlement = int(ent) if isinstance(ent, (int, float)) else None
        acct.quota_remaining = int(rem) if isinstance(rem, (int, float)) else None
        pct = premium.get("percent_remaining")
        acct.quota_percent_remaining = float(pct) if isinstance(pct, (int, float)) else None
    return acct


def check_account(acct: Account | None, allow_emu: bool | None = None) -> list[str]:
    """Return warnings, or raise AccountError if the run must not proceed.

    Refusing an unexpected account by default is deliberate: an experiment that
    silently ran on the wrong identity produces numbers that cannot be compared
    with the rest of the table, and the mistake is invisible after the fact.
    """
    expected = os.environ.get(ENV_EXPECTED_LOGIN, "").strip()
    if acct is None:
        if expected:
            raise AccountError(
                f"could not determine the active GitHub account, so "
                f"{ENV_EXPECTED_LOGIN}={expected!r} cannot be enforced"
            )
        return ["could not determine the active GitHub account; "
                "run provenance will be incomplete"]

    warnings: list[str] = []
    if expected and expected != acct.login:
        raise AccountError(
            f"active account is {acct.login!r} but {ENV_EXPECTED_LOGIN} pins this run to "
            f"{expected!r}; switch with `gh auth switch --user {expected}` or clear the pin"
        )

    if acct.is_emu:
        if allow_emu is None:
            allow_emu = os.environ.get(ENV_ALLOW_EMU, "").strip() in ("1", "true", "yes")
        if not allow_emu:
            raise AccountError(
                f"active account {acct.login!r} is an Enterprise Managed User "
                f"(shortcode {acct.shortcode!r}). EMU tenants apply their own model "
                f"availability and content policies, so results are not comparable with "
                f"non-EMU runs. Switch accounts with `gh auth switch`, or pass --allow-emu "
                f"(or set {ENV_ALLOW_EMU}=1) to record the run as EMU."
            )
        warnings.append(
            f"running on EMU account {acct.login!r} — model availability and policy "
            f"may differ from non-EMU runs; results are tagged accordingly"
        )

    if acct.quota_percent_remaining is not None and acct.quota_percent_remaining < 10.0:
        warnings.append(
            f"premium quota is {acct.quota_percent_remaining:.1f}% remaining "
            f"({acct.quota_remaining:,} left) — a long run may exhaust it mid-way"
        )
    return warnings


def resolve_and_check(login: str | None = None,
                      allow_emu: bool | None = None,
                      verbose: bool = True) -> Account | None:
    """Detect the account, enforce the policy, and print what was found."""
    acct = detect_account(login)
    warnings = check_account(acct, allow_emu=allow_emu)
    if verbose and acct is not None:
        print("--- account ---")
        print(acct.render())
    for w in warnings:
        print(f"WARNING: {w}")
    return acct


if __name__ == "__main__":
    import sys

    try:
        resolve_and_check(allow_emu=True)
    except AccountError as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)
