"""Pay to open, in two halves, for a host that has a language model between
the owner and the wallet (payments v1 §4.7, §6.4).

**This server holds no key, takes none and signs nothing.** ``check`` reads
the job's record, the worker's public wallet history and AgentX's payment
request, refuses unless they agree, and returns the plain sentence and the
typed data for the buyer wallet. ``send`` takes the signature the wallet made
and checks the payment against AgentX again before it leaves.

Both stand on the Python SDK's reviewed code and add no check of their own:
``agentx_sdk.payments`` (the checks, their order and the reason codes of
``client.payments.pay_and_open``) and ``agentx_sdk.pay_cli`` (the payment as
``agentx-pay`` files it, and the re-check at ``send``). The SDK's code is
synchronous, so it runs in a worker thread; :class:`_Bridge` gives it this
server's signed-in HTTP client in the place of an ``AgentXClient``.
"""
from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from typing import Any

import httpx
from agentx_sdk.pay_cli import send_checked, to_file
from agentx_sdk.payments import (
    PAYMENT_SIGNATURE_HEADER, PaymentCheckError, PaymentNotAccepted, PaymentPending, PaymentsNamespace,
    ResultMismatch, Unsigned, _Job,
)
from mcp.server.mcpserver.exceptions import ToolError

from .api import AgentXAPI, Unreachable

NOTHING_SENT = "Nothing was signed or sent."
SAME_AGAIN = ("Do NOT sign another payment. Call agentx_pay_send again with the same payment and the "
              "same signature.")
NEXT_STEP = (
    "Show the owner payment.sentence exactly as written; never reword it. Have the buyer wallet "
    "(from_wallet) sign payment.typed_data with eth_signTypedData_v4; the wallet must show the same "
    "amount and the same `to` address as the sentence. Then call agentx_pay_send with this payment, "
    "unchanged, and the signature, before payment.sign_before. This server signs nothing."
)


class _Bridge:
    """What ``agentx_sdk.payments`` asks of a client, answered by this
    server's :class:`AgentXAPI`. Called from a worker thread; every request
    goes back to the event loop the tool runs on."""

    def __init__(self, api: AgentXAPI, loop: asyncio.AbstractEventLoop, agent_did: str | None):
        self._api, self._loop = api, loop
        self.identity = SimpleNamespace(agent_did=agent_did)
        self._log = logging.getLogger("agentx_mcp.pay")
        # Whether a request carrying the wallet's signature has gone out.
        self.signed_sent = False
        self.payments = PaymentsNamespace(self)  # type: ignore[arg-type]

    def _send(self, method: str, path: str, *, extra_headers: dict[str, str] | None = None,
              checked: bool = False) -> httpx.Response:
        if extra_headers and PAYMENT_SIGNATURE_HEADER in extra_headers:
            self.signed_sent = True
        asked = asyncio.run_coroutine_threadsafe(
            self._api.unchecked(method, path, headers=extra_headers), self._loop)
        try:
            return asked.result()
        except Unreachable as lost:
            # The SDK sends the same signed payment again on exactly this.
            raise httpx.TransportError(str(lost)) from None

    def _get(self, path: str) -> Any:
        answer = self._send("GET", path)
        if answer.status_code >= 300:
            raise ToolError(self._api.error_text(answer))
        try:
            return answer.json()
        except ValueError:
            raise ToolError(f"AgentX returned a non-JSON answer ({answer.status_code})") from None


def job_of(task_id: str | None, bounty_id: str | None, submission_id: str | None) -> _Job:
    """The job the arguments name: a task, or a bounty's submission."""
    try:
        if task_id and not (bounty_id or submission_id):
            return _Job(task_id=task_id)
        if bounty_id and submission_id and not task_id:
            return _Job(bounty_id=bounty_id, submission_id=submission_id)
    except ValueError:
        pass
    raise ToolError(f"Give task_id, or bounty_id with submission_id. {NOTHING_SENT}")


def _not_accepted(api: AgentXAPI, no: PaymentNotAccepted, signed: bool) -> ToolError:
    said = api.scrub(str(no))
    if not signed:
        return ToolError(f"AgentX is not taking a payment for this job now: {said}. {NOTHING_SENT}")
    wait = f" Wait {no.retry_after} seconds before signing another." if no.retry_after else ""
    return ToolError(f"The signed payment was sent and AgentX did not accept it: {said}.{wait}")


async def _in_thread(api: AgentXAPI, work: Any) -> tuple[_Bridge, Any]:
    creds = api.require_credentials()
    bridge = _Bridge(api, asyncio.get_running_loop(), creds.agent_did)
    return bridge, await asyncio.to_thread(work, bridge)


async def check(api: AgentXAPI, job: _Job, wallet: str | None, max_usd: str | None) -> dict[str, Any]:
    """The first half. Signs nothing and sends no payment."""
    try:
        _, ready = await _in_thread(api, lambda bridge: bridge.payments._ready(job, wallet, max_usd))
    except PaymentCheckError as refused:
        raise ToolError(f"{refused} {NOTHING_SENT}") from None
    except PaymentNotAccepted as no:
        raise _not_accepted(api, no, signed=False) from None
    except ResultMismatch as mismatch:
        # Already paid, and the result is not the one the delivery's fingerprint names.
        return {"status": "paid_result_mismatch", "warning": str(mismatch), "opened": mismatch.opened,
                "nothing_signed_or_sent": True}
    except httpx.TransportError:
        raise ToolError(f"Could not reach AgentX at {api.base_url}. {NOTHING_SENT}") from None
    except ToolError:
        raise
    except Exception as failed:  # an answer nobody expected is no payment to sign
        raise ToolError(f"AgentX gave an answer that could not be read ({type(failed).__name__}). "
                        f"{NOTHING_SENT}") from None
    if not isinstance(ready, Unsigned):
        return {"status": "already_paid", "nothing_signed_or_sent": True, "opened": ready}
    return {
        "status": "ready_to_sign",
        "nothing_signed_or_sent": True,
        "from_wallet": ready.typed["message"]["from"],
        "payment": to_file(ready),
        "next": NEXT_STEP,
    }


def _payment(given: object) -> object:
    """The payment as ``agentx_pay_check`` returned it; a host that passes it
    as JSON text is read too. Its content is checked by the SDK."""
    if isinstance(given, str):
        try:
            return json.loads(given)
        except ValueError:
            raise ToolError(f"payment is not the object agentx_pay_check returned. {NOTHING_SENT}") from None
    return given


async def send(api: AgentXAPI, payment: object, signature: str, wait_seconds: float) -> dict[str, Any]:
    """The second half: the payment is checked against AgentX again, then sent
    with the signature made elsewhere."""
    filed = _payment(payment)
    bridges: list[_Bridge] = []

    def work(bridge: _Bridge) -> dict[str, Any]:
        bridges.append(bridge)
        return send_checked(bridge, filed, signature, wait_seconds=wait_seconds)

    def sent() -> bool:
        return bool(bridges) and bridges[0].signed_sent

    try:
        _, opened = await _in_thread(api, work)
    except PaymentCheckError as refused:
        raise ToolError(f"{refused} Nothing was sent: the signature did not leave this server. "
                        "Call agentx_pay_check again and sign the new payment.") from None
    except PaymentPending:
        return {"status": "settling", "message": "The payment was sent and is not settled yet. " + SAME_AGAIN}
    except PaymentNotAccepted as no:
        raise _not_accepted(api, no, signed=no.signed) from None
    except ResultMismatch as mismatch:
        return {"status": "paid_result_mismatch", "opened": mismatch.opened,
                "warning": f"The payment went through, but: {mismatch}"}
    except (ToolError, httpx.TransportError) as failed:
        if sent():
            raise ToolError(f"The signed payment was sent and its outcome is not known "
                            f"({api.scrub(str(failed))}). {SAME_AGAIN}") from None
        if isinstance(failed, ToolError):
            raise ToolError(f"{failed} Nothing was sent.") from None
        raise ToolError(f"Could not reach AgentX at {api.base_url}. Nothing was sent.") from None
    except Exception as failed:  # an answer nobody expected: say which side of the signature it fell on
        if sent():
            raise ToolError(f"The signed payment was sent and its outcome is not known "
                            f"({type(failed).__name__}). {SAME_AGAIN}") from None
        raise ToolError(f"AgentX gave an answer that could not be read ({type(failed).__name__}). "
                        "Nothing was sent.") from None
    return {"status": "paid", "opened": opened}


__all__ = ["check", "job_of", "send"]
