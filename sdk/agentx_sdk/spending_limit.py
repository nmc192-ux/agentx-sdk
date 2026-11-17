"""AgentX SDK — the owner's spending limits (payments_v1.md §8.3 item 2).

A profile's owner can cap what the profile commits as a buyer: at most
``usd_a_day`` in any 24 hours and ``usd_a_job`` a job. AgentX's own caps hold
whatever the limit says.

- **Lowering** needs the agent token alone and is in force at once.
- **Raising** needs the payout wallet's signature, then waits 24 hours. Until
  then the limit in force stays, and the token alone can cancel the raise.

**The SDK holds no key.** :meth:`SpendingLimitNamespace.raise_with` takes a
``sign`` function, as the payout wallet calls do, and calls it only if
AgentX's text says it moves no funds and names the amounts you asked for.
Every write needs an access token issued in the last 15 minutes; the client
renews its pair itself when AgentX asks.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Optional

from .payments import NO_FUNDS_SENTENCE, PaymentCheckError, _refuse, short_address
from .payout_wallet import Signer, _signature

if TYPE_CHECKING:
    from .client import AgentXClient

ME = "/agents/me/spending-limit"
RAISE_PROMPT = "raise_spending_limit"

_AMOUNTS = re.compile(r" to (\S+) USD a day and (\S+) USD a job\. ")


def _usd(value: Any, field: str) -> str:
    """An amount goes to the server as the caller's string ("20", "2.50"); the
    server parses it. A number is refused here: no float stands for money."""
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string such as \"2.50\", not {type(value).__name__}")
    return value


def _value(text: str) -> Optional[Decimal]:
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def check_raise_text(challenge: object, usd_a_day: str, usd_a_job: str) -> str:
    """The text to sign from a raise challenge, or :class:`PaymentCheckError`.

    Refused unless it is the raise prompt, says it moves no funds (in the text
    and in ``moves_funds``), and names exactly the two amounts asked for."""
    if not isinstance(challenge, dict):
        raise _refuse("unreadable_prompt", "AgentX's answer is not a challenge; nothing was signed.")
    text = challenge.get("text_to_sign")
    if (challenge.get("prompt") != RAISE_PROMPT or not isinstance(text, str)
            or NO_FUNDS_SENTENCE not in text or challenge.get("moves_funds") is not False):
        raise _refuse("unreadable_prompt",
                      f"The text to sign is not a spending-limit raise that says \"{NO_FUNDS_SENTENCE}\"; "
                      "nothing was signed.")
    found = _AMOUNTS.search(text)
    asked = (_value(usd_a_day), _value(usd_a_job))
    if found is None or None in asked or (_value(found.group(1)), _value(found.group(2))) != asked:
        raise _refuse("amount_mismatch",
                      f"The text to sign does not name {usd_a_day} USD a day and {usd_a_job} USD a job; "
                      "nothing was signed.")
    return text


class SpendingLimitNamespace:
    """The owner's spending limits; ``client.spending_limit``."""

    def __init__(self, client: "AgentXClient") -> None:
        self._client = client

    def get(self) -> dict[str, Any]:
        """This profile's limits (``GET /agents/me/spending-limit``): ``limit``
        (``usd_a_day``, ``usd_a_job``; ``None`` if never set, then AgentX's
        caps are the limit), ``pending_raise`` (a signed raise in its wait,
        with ``effective_at``), ``agentx_caps`` and ``raise_wait_hours``."""
        return self._client._get(ME)

    def lower(self, *, usd_a_day: Optional[str] = None, usd_a_job: Optional[str] = None) -> dict[str, Any]:
        """Lower one limit or both with the token alone (``PUT``); in force at
        once. Amounts are strings ("20", "2.50"). Asking for more than the
        limit in force is answered 403 ``wallet_signature_required``: use
        :meth:`raise_with`."""
        body: dict[str, str] = {}
        if usd_a_day is not None:
            body["usd_a_day"] = _usd(usd_a_day, "usd_a_day")
        if usd_a_job is not None:
            body["usd_a_job"] = _usd(usd_a_job, "usd_a_job")
        if not body:
            raise ValueError("Give usd_a_day, usd_a_job or both.")
        return self._client._put(ME, body)

    def raise_challenge(self, usd_a_day: str, usd_a_job: str) -> dict[str, Any]:
        """The text the payout wallet signs to raise both limits to these
        amounts (``POST .../raise/challenge``), valid 10 minutes, usable once.
        409/422 when the amounts are above AgentX's caps or not a raise."""
        return self._client._post(f"{ME}/raise/challenge", {
            "usd_a_day": _usd(usd_a_day, "usd_a_day"), "usd_a_job": _usd(usd_a_job, "usd_a_job")})

    def raise_limit(self, signature: str) -> dict[str, Any]:
        """Send the payout wallet's signature over the last challenge
        (``POST .../raise``). The raise is ``pending`` and in force 24 hours
        later; a raise that already waited is replaced
        (``replaced_pending_raise``). The amounts are the challenge's own."""
        return self._client._post(f"{ME}/raise", {"signature": signature})

    def raise_with(self, usd_a_day: str, usd_a_job: str, sign: Signer) -> dict[str, Any]:
        """:meth:`raise_challenge`, ``sign``, :meth:`raise_limit` in one call.
        ``sign`` gets the text (``personal_sign``, EIP-191) and is not called
        unless the text is a raise to exactly these amounts that moves no
        funds (:func:`check_raise_text`; else :class:`PaymentCheckError`)."""
        challenge = self.raise_challenge(usd_a_day, usd_a_job)
        check_raise_text(challenge, usd_a_day, usd_a_job)
        address = challenge.get("address")
        self._client._log.info(
            "AgentX asks the payout wallet %s to sign: raise the spending limit to %s USD a day and "
            "%s USD a job. %s", short_address(address) if isinstance(address, str) else "?",
            usd_a_day, usd_a_job, NO_FUNDS_SENTENCE)
        return self.raise_limit(_signature(sign, challenge))

    def cancel_pending(self) -> dict[str, Any]:
        """Cancel a signed raise still in its wait, with the token alone
        (``DELETE .../pending``). The limit in force is unchanged. 409 when
        no raise waits."""
        return self._client._delete(f"{ME}/pending")


__all__ = ["SpendingLimitNamespace", "check_raise_text", "PaymentCheckError"]
