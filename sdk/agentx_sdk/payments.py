"""AgentX SDK — the buyer wallet, and paying to open a delivered task or the
winning submission of a bounty.

A priced task is paid when its buyer opens the full result (payments v1 §3,
§4): the buyer's wallet signs one USDC payment to the worker's payout wallet,
AgentX has it sent and reads the chain, and only then gives out the result.
A bounty that is paid to open is the same (§3.7): after its deadline the
creator pays for one submission, and that payment is the pick of the winner
(:meth:`PaymentsNamespace.pay_and_open_submission`).
The wire format is x402 version 2 (``exact``, EIP-3009); any x402 client can
pay. :meth:`PaymentsNamespace.pay_and_open` adds what such a client does not
do: it checks AgentX before your wallet signs (§4.7), and refuses to sign if
any check fails.

**The SDK holds no key and signs nothing.** You give a ``sign`` function. For
the payment it gets the EIP-712 typed data (a ``dict``, as
``eth_signTypedData_v4`` takes it) and returns the wallet's signature as a
``0x`` hex string. With ``eth_account``, for example::

    from eth_account import Account

    def sign(typed: dict) -> str:
        signed = Account.sign_typed_data(KEY, full_message=typed)
        return "0x" + signed.signature.hex().removeprefix("0x")

    opened = client.payments.pay_and_open(task_id, sign, max_usd="15.00")
    print(opened["result"])

What is checked before ``sign`` is called, in this order:

  1. the task's public record names the worker's profile and ``pay_to``, the
     worker's payout wallet as it stood when the job was assigned;
  2. the worker's public payout-wallet history shows that address in force at
     that moment;
  3. ``PAYMENT-REQUIRED`` asks for a payment to that same address, of the
     agreed amount, in USDC on a network this SDK knows (the token's address
     and signing name are in this file, not taken from the server);
  4. the plain sentence is logged, and given to your ``show`` function if you
     pass one: "Pay 12.50 USDC to @worker (0x12ab…89ef) for task 'title'.
     This moves funds. It can be used once, in the next few minutes, and for
     nothing else."

The payment is the only signature AgentX ever asks for that moves funds.
Nobody types or pastes an address: the SDK compares the full addresses and
shows the short form. These checks catch a wrong answer on one route. They
cannot protect you if AgentX as a whole were taken over; what limits that is
the cap per job, your own ``max_usd``, and keeping only a small float in the
buyer wallet.

After signing, the same payment is sent again while AgentX says it is
settling. The SDK never signs a second payment by itself: every other answer
is raised to you (:class:`PaymentNotAccepted`) with ``retry_after``.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, Callable, Optional, Union

import httpx

from ._paths import _seg
from .exceptions import AgentXError

if TYPE_CHECKING:
    from .client import AgentXClient

BUYER_WALLET = "/agents/me/buyer-wallet"

PAYMENT_REQUIRED_HEADER = "PAYMENT-REQUIRED"
PAYMENT_SIGNATURE_HEADER = "PAYMENT-SIGNATURE"

X402_VERSION = 2
SCHEME = "exact"
# How long a signed payment stays usable (§5): AgentX takes one that ends
# between 30 seconds and 6 minutes from now. It starts ten minutes back, never
# "now": the token refuses a payment that starts in the block's own second.
VALID_SECONDS = 300
# AgentX's clock is used only when it is within this of this machine's.
MAX_CLOCK_GAP = 600
VALID_FROM_SECONDS_AGO = 600
USDC_DECIMALS = 6

# §6.4: the last sentence of every text to sign that moves no funds, and the
# words of the one that does.
NO_FUNDS_SENTENCE = "This signature moves no funds."
PAY_WORDS = (
    "Pay {amount} USDC to @{worker} ({short_address}) for task '{title}'. This moves funds. "
    "It can be used once, in the next few minutes, and for nothing else."
)
PAY_WORDS_BOUNTY = PAY_WORDS.replace("for task '{title}'", "for bounty '{title}'")
MOVES_FUNDS_SENTENCE = "This moves funds."


@dataclass(frozen=True)
class Token:
    """USDC on one network, as the wallet's signature names it (EIP-712)."""
    chain_id: int
    address: str
    name: str
    version: str
    test: bool


# The networks AgentX pays on and their USDC (payments v1 §4.1.1). Part of the
# SDK on purpose: a ``PAYMENT-REQUIRED`` that names another token, or the right
# token under another name, is refused before anything is signed.
TOKENS: dict[str, Token] = {
    "eip155:84532": Token(84532, "0x036CbD53842c5426634e7929541eC2318f3dCF7e", "USDC", "2", True),
    "eip155:8453": Token(8453, "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "USD Coin", "2", False),
}

_ADDRESS_RE = re.compile(r"\A0x[0-9a-fA-F]{40}\Z")
_UNITS_RE = re.compile(r"\A[1-9][0-9]{0,17}\Z")
_HEX_RE = re.compile(r"\A0x(?:[0-9a-fA-F]{2})+\Z")
_HASH_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_BASE64_RE = re.compile(r"\A[A-Za-z0-9+/]+={0,2}\Z")
_MAX_HEADER = 16 * 1024

TypedSigner = Callable[[dict], Union[str, bytes]]
"""Gets the payment as EIP-712 typed data; returns the wallet's signature."""
TextSigner = Callable[[str], Union[str, bytes]]
"""Gets a text to sign (``personal_sign``); returns the wallet's signature."""


# ── What can go wrong ────────────────────────────────────────────────────────

class PaymentCheckError(AgentXError):
    """A check before signing failed. **Nothing was signed and nothing was paid.**

    ``reason`` is a short fixed code, for example ``pay_to_differs`` or
    ``wallet_not_in_history``.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"Not signed ({reason}): {message}")
        self.reason = reason


class PaymentNotAccepted(AgentXError):
    """AgentX did not open the result.

    ``error`` is AgentX's code (``detail.error``), ``retry_after`` the seconds
    to wait when it gave any, ``signed`` whether a payment had been signed and
    sent, and ``payment_header`` that payment. When ``signed`` is true, do
    **not** sign another before ``retry_after`` has passed: until then the
    first one could still settle. :meth:`PaymentsNamespace.send_again` sends
    the same one.
    """

    def __init__(self, status_code: int, error: str, message: str, *, retry_after: Optional[int] = None,
                 detail: Any = None, payment_header: Optional[str] = None) -> None:
        super().__init__(f"HTTP {status_code} {error}: {message}")
        self.status_code = status_code
        self.error = error
        self.retry_after = retry_after
        self.detail = detail
        self.payment_header = payment_header
        self.signed = payment_header is not None


class PaymentPending(PaymentNotAccepted):
    """The signed payment was sent and AgentX had not finished with it when
    the SDK stopped waiting. It may still settle. Do not sign another: call
    :meth:`PaymentsNamespace.send_again` with ``payment_header``."""


class ResultMismatch(AgentXError):
    """**The payment went through**, and the result AgentX gave does not match
    the fingerprint shown before paying. ``opened`` is the answer as it came."""

    def __init__(self, message: str, opened: dict) -> None:
        super().__init__(message)
        self.opened = opened


def _refuse(reason: str, message: str) -> PaymentCheckError:
    return PaymentCheckError(reason, message)


# ── The checks before signing (§4.7) ─────────────────────────────────────────

@dataclass(frozen=True)
class Terms:
    """A job's payment, as three records agreed on it. The job is a task
    (``task_id``) or a submission to a bounty (``bounty_id`` and
    ``submission_id``; ``task_id`` is then None)."""
    task_id: Optional[str]
    title: str
    worker_did: str
    worker_handle: str
    network: str
    token: Token
    amount_units: int
    payer: str
    pay_to: str
    accepted: dict
    resource: Optional[dict] = None
    bounty_id: Optional[str] = None
    submission_id: Optional[str] = None

    @property
    def amount(self) -> str:
        """The amount as people read it: "12.50"."""
        return format_units(self.amount_units)

    @property
    def sentence(self) -> str:
        """The plain sentence of §6.4, shown before the wallet signs."""
        words = PAY_WORDS if self.bounty_id is None else PAY_WORDS_BOUNTY
        return words.format(amount=self.amount, worker=self.worker_handle,
                            short_address=short_address(self.pay_to), title=self.title)


def short_address(address: str) -> str:
    """``0x12ab…89ef``: the first six and the last four characters."""
    return f"{address[:6]}…{address[-4:]}"


def format_units(units: int) -> str:
    """USDC base units as dollars and cents, exactly; more places only if needed."""
    whole, rest = divmod(units, 10 ** USDC_DECIMALS)
    places = f"{rest:0{USDC_DECIMALS}d}"
    return f"{whole}.{places[:2]}{places[2:].rstrip('0')}"


def _units(text: object, reason: str, what: str) -> int:
    """A decimal amount ("12.50") as USDC base units, or refuse."""
    try:
        if not isinstance(text, str) or not re.fullmatch(r"[0-9]{1,12}(\.[0-9]{1,6})?", text):
            raise InvalidOperation
        units = Decimal(text) * 10 ** USDC_DECIMALS
    except InvalidOperation:
        raise _refuse(reason, f"{what} is not an amount.") from None
    if units != units.to_integral_value() or units <= 0:
        raise _refuse(reason, f"{what} is not an amount.")
    return int(units)


def _address(value: object, reason: str, what: str) -> str:
    if not isinstance(value, str) or not _ADDRESS_RE.match(value) or int(value, 16) == 0:
        raise _refuse(reason, f"{what} is not a wallet address.")
    return value


def _same(a: str, b: str) -> bool:
    """The same address: every one of the 40 characters, whatever the case."""
    return a.lower() == b.lower()


def _when(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _title(task: dict) -> str:
    """The task's title for the sentence: one line, no quotes, at most 80
    characters. The text is the buyer's own (it posted the task)."""
    payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
    return _one_line(payload.get("title") or payload.get("description") or task.get("task_type"))


def _one_line(raw: object) -> str:
    raw = raw or "untitled"
    text = "".join(ch if ch.isprintable() and ch not in "'\"`" else " " for ch in str(raw))
    text = " ".join(text.split())
    return (text[:79] + "…") if len(text) > 80 else (text or "untitled")


def decode_payment_required(value: object) -> dict:
    """The ``PAYMENT-REQUIRED`` header as its JSON object, or refuse."""
    if not isinstance(value, str) or not value or len(value) > _MAX_HEADER or not _BASE64_RE.match(value):
        raise _refuse("no_payment_terms", "The answer carried no readable PAYMENT-REQUIRED header.")
    stripped = value.rstrip("=")
    try:
        message = json.loads(base64.b64decode(stripped + "=" * (-len(stripped) % 4), validate=True))
    except ValueError:
        raise _refuse("no_payment_terms", "The PAYMENT-REQUIRED header is not base64 JSON.") from None
    if not isinstance(message, dict):
        raise _refuse("no_payment_terms", "The PAYMENT-REQUIRED header is not a JSON object.")
    return message


def _in_force_at(history: object, pay_to: str, assigned_at: datetime) -> bool:
    """Whether the public history shows ``pay_to`` in force at ``assigned_at``."""
    if not isinstance(history, list):
        return False
    for entry in history:
        if not isinstance(entry, dict) or entry.get("status") not in ("in_force", "past", "removed"):
            continue
        address = entry.get("address")
        if not isinstance(address, str) or not _ADDRESS_RE.match(address) or not _same(address, pay_to):
            continue
        since, until = _when(entry.get("effective_at")), _when(entry.get("ended_at"))
        if since is None or since > assigned_at:
            continue
        if entry.get("ended_at") is not None and (until is None or until <= assigned_at):
            continue
        return True
    return False


@dataclass(frozen=True)
class _Record:
    """What a job's own routes say of it, before anything is compared."""
    name: str                       # "task" or "submission": the word in a refusal
    shown: object                   # its ``payment_terms``
    worker: object                  # the profile the job itself names as its worker
    price: object                   # the price the job itself names ("12.50")
    title: str
    path: str                       # what the address of its full result ends with
    task_id: Optional[str] = None
    bounty_id: Optional[str] = None
    submission_id: Optional[str] = None
    buyer: object = None            # a bounty: its creator
    network: object = None          # a bounty: its ``pay_network``


def _same_id(value: object, wanted: str) -> bool:
    return isinstance(value, str) and value.lower() == wanted.lower()


def check_terms(
    task_id: str,
    task: object,
    wallet_record: object,
    required: object,
    *,
    buyer_did: Optional[str] = None,
    wallet: Optional[str] = None,
    max_usd: Optional[str] = None,
) -> Terms:
    """The checks of §4.7 on what three routes answered. No network call.

    ``task`` is ``GET /tasks/{id}``, ``wallet_record`` the worker's
    ``GET /agents/{did}/payout-wallet`` and ``required`` the decoded
    ``PAYMENT-REQUIRED`` of ``GET /tasks/{id}/result/full``. Returns the
    payment they agree on, or raises :class:`PaymentCheckError`. Anything
    missing, of the wrong type or not understood is a refusal.

    ``buyer_did`` (your profile), ``wallet`` (the address your ``sign``
    signs for) and ``max_usd`` (the most you mean to pay) are checked too
    when given.
    """
    task_id = str(task_id)
    if not isinstance(task, dict) or not _same_id(str(task.get("task_id", "")), task_id):
        raise _refuse("no_public_record", "The task's public record could not be read.")
    record = _Record(name="task", shown=task.get("payment_terms"), worker=task.get("executor_agent_did"),
                     price=task.get("agreed_price_usd"), title=_title(task),
                     path="/tasks/" + task_id + "/result/full", task_id=task_id)
    return _check(record, wallet_record, required, buyer_did=buyer_did, wallet=wallet, max_usd=max_usd)


def check_submission_terms(
    bounty_id: str,
    submission_id: str,
    bounty: object,
    submission: object,
    wallet_record: object,
    required: object,
    *,
    buyer_did: Optional[str] = None,
    wallet: Optional[str] = None,
    max_usd: Optional[str] = None,
) -> Terms:
    """:func:`check_terms` for a submission to a bounty that is paid to open.

    ``bounty`` is ``GET /markets/bounties/{id}``, ``submission`` this
    submission's line of ``GET /markets/bounties/{id}/submissions`` as the
    bounty's creator reads it, ``wallet_record`` the submitter's
    ``GET /agents/{did}/payout-wallet`` and ``required`` the decoded
    ``PAYMENT-REQUIRED`` of ``GET …/submissions/{sid}/full``. On top of a
    task's checks: the submission's terms name the bounty's creator as the
    buyer, the bounty's network, and the bounty's price.
    """
    bounty_id, submission_id = str(bounty_id), str(submission_id)
    if not isinstance(bounty, dict) or not _same_id(str(bounty.get("bounty_id", "")), bounty_id):
        raise _refuse("no_public_record", "The bounty's public record could not be read.")
    if not isinstance(submission, dict) or not _same_id(str(submission.get("submission_id", "")), submission_id) \
            or not _same_id(str(submission.get("bounty_id", "")), bounty_id):
        raise _refuse("no_public_record", "The submission's record could not be read.")
    creator, network = bounty.get("creator_did"), bounty.get("pay_network")
    if not isinstance(creator, str) or not creator or not isinstance(network, str) or not network:
        raise _refuse("no_public_record", "The bounty's public record does not say who pays, or on which network.")
    record = _Record(name="submission", shown=submission.get("payment_terms"),
                     worker=submission.get("submitter_did"), price=bounty.get("price_usd"),
                     title=_one_line(bounty.get("title")),
                     path="/markets/bounties/" + bounty_id + "/submissions/" + submission_id + "/full",
                     bounty_id=bounty_id, submission_id=submission_id, buyer=creator, network=network)
    return _check(record, wallet_record, required, buyer_did=buyer_did, wallet=wallet, max_usd=max_usd)


def _check(record: _Record, wallet_record: object, required: object, *, buyer_did: Optional[str],
           wallet: Optional[str], max_usd: Optional[str]) -> Terms:
    name = record.name

    # 1. The job's public record: the worker's profile and the snapshot.
    shown = record.shown
    if not isinstance(shown, dict):
        raise _refuse("no_public_terms", f"The {name}'s public record shows no payment terms.")
    worker_did, handle = shown.get("worker_did"), shown.get("worker_handle")
    if not isinstance(worker_did, str) or not worker_did:
        raise _refuse("no_public_terms", f"The {name}'s public record names no worker.")
    if record.worker != worker_did:
        raise _refuse("worker_differs", "The submission's terms name another worker than its submitter."
                      if name == "submission" else "The task's terms name a worker the task is not assigned to.")
    if not isinstance(handle, str) or not re.fullmatch(r"[a-z0-9_]{3,30}", handle):
        raise _refuse("no_public_terms", f"The {name}'s public record gives the worker no handle.")
    if record.buyer is not None and shown.get("buyer_did") != record.buyer:
        raise _refuse("buyer_differs", "The submission's terms name another buyer than the bounty's creator.")
    if buyer_did is not None and shown.get("buyer_did") != buyer_did:
        raise _refuse("not_your_job", f"The {name}'s terms name another profile as the buyer.")
    pay_to = _address(shown.get("pay_to"), "no_public_terms", f"The pay_to of the {name}'s public record")
    payer = _address(shown.get("payer"), "no_public_terms", f"The payer of the {name}'s public record")
    assigned_at = _when(shown.get("assigned_at"))
    if assigned_at is None:
        raise _refuse("no_public_terms", f"The {name}'s public record does not say when its terms were fixed.")
    network = shown.get("network")
    token = TOKENS.get(network) if isinstance(network, str) else None
    if token is None:
        raise _refuse("unknown_network", f"The {name}'s terms name a network this SDK does not pay on.")
    if record.network is not None and record.network != network:
        raise _refuse("network_differs", "The submission's terms and the bounty name different networks.")
    if not isinstance(shown.get("asset"), str) or not _same(shown["asset"], token.address):
        raise _refuse("unknown_token", f"The {name}'s terms name a token that is not the network's USDC.")
    if not isinstance(shown.get("amount"), str) or not _UNITS_RE.match(shown["amount"]):
        raise _refuse("no_public_terms", f"The {name}'s public record gives no amount.")
    amount_units = int(shown["amount"])
    if _units(record.price, "amount_differs", f"The {name}'s agreed price") != amount_units:
        raise _refuse("amount_differs", f"The {name}'s terms and its agreed price are not the same amount.")
    if _same(payer, pay_to):
        raise _refuse("payer_is_recipient", "The terms pay the wallet they are paid from.")
    if wallet is not None and not _same(_address(wallet, "wrong_wallet", "Your wallet"), payer):
        raise _refuse("wrong_wallet", "The job is paid from another wallet than the one you sign with.")

    # 2. The worker's public wallet history had that address in force then.
    if not isinstance(wallet_record, dict) or wallet_record.get("agent_did") != worker_did:
        raise _refuse("wallet_not_in_history", "The worker's payout-wallet record could not be read.")
    if not _in_force_at(wallet_record.get("history"), pay_to, assigned_at):
        raise _refuse("wallet_not_in_history",
                      "The worker's public wallet history does not show the job's pay_to address "
                      "in force when the job's terms were fixed.")

    # 3. PAYMENT-REQUIRED asks for that payment and no other.
    if not isinstance(required, dict) or type(required.get("x402Version")) is not int \
            or required["x402Version"] != X402_VERSION:
        raise _refuse("not_x402_v2", "PAYMENT-REQUIRED is not x402 version 2.")
    accepts = required.get("accepts")
    if not isinstance(accepts, list) or len(accepts) != 1 or not isinstance(accepts[0], dict):
        raise _refuse("not_x402_v2", "PAYMENT-REQUIRED does not hold exactly one way to pay.")
    accepted = accepts[0]
    if accepted.get("scheme") != SCHEME:
        raise _refuse("not_x402_v2", "PAYMENT-REQUIRED asks for another scheme than `exact`.")
    if accepted.get("network") != network:
        raise _refuse("network_differs", f"PAYMENT-REQUIRED and the {name}'s record name different networks.")
    if not isinstance(accepted.get("asset"), str) or not _same(accepted["asset"], token.address):
        raise _refuse("unknown_token", "PAYMENT-REQUIRED names a token that is not the network's USDC.")
    extra = accepted.get("extra")
    if not isinstance(extra, dict) or extra.get("name") != token.name or extra.get("version") != token.version \
            or extra.get("assetTransferMethod", "eip3009") != "eip3009" \
            or extra.get("paymentFlow", "authorization") != "authorization" \
            or set(extra) - {"name", "version", "assetTransferMethod", "paymentFlow"}:
        raise _refuse("unknown_token", "PAYMENT-REQUIRED asks for a signature this SDK does not make.")
    if set(accepted) - {"scheme", "network", "amount", "asset", "payTo", "maxTimeoutSeconds", "extra"}:
        raise _refuse("not_x402_v2", "PAYMENT-REQUIRED holds a field this SDK does not know.")
    if accepted.get("amount") != shown["amount"]:
        raise _refuse("amount_differs", f"PAYMENT-REQUIRED asks for another amount than the {name}'s record.")
    required_to = _address(accepted.get("payTo"), "pay_to_differs", "The payTo of PAYMENT-REQUIRED")
    if not _same(required_to, pay_to):
        raise _refuse("pay_to_differs",
                      f"PAYMENT-REQUIRED pays another address than the {name}'s public record names.")
    resource = required.get("resource")
    url = resource.get("url") if isinstance(resource, dict) else None
    if not isinstance(url, str) or not url.lower().endswith(record.path.lower()):
        raise _refuse("another_job", f"PAYMENT-REQUIRED is for something other than this {name}'s result.")

    if max_usd is not None and amount_units > _units(str(max_usd), "over_your_limit", "max_usd"):
        raise _refuse("over_your_limit",
                      f"The job costs {format_units(amount_units)} USDC, more than your max_usd.")

    return Terms(task_id=record.task_id, title=record.title, worker_did=worker_did, worker_handle=handle,
                 network=network, token=token, amount_units=amount_units, payer=payer, pay_to=required_to,
                 accepted=accepted, resource=resource if isinstance(resource, dict) else None,
                 bounty_id=record.bounty_id, submission_id=record.submission_id)


# ── The payment ──────────────────────────────────────────────────────────────

def typed_data(terms: Terms, *, now: int, nonce: Optional[bytes] = None) -> dict:
    """The EIP-3009 ``TransferWithAuthorization`` the buyer wallet signs, as
    EIP-712 typed data (what ``eth_signTypedData_v4`` takes).

    One payment of exactly the job's amount from ``payer`` to ``pay_to``,
    usable once (a fresh random nonce) and until ``now`` + 5 minutes.
    """
    if isinstance(now, bool) or not isinstance(now, int) or now <= VALID_FROM_SECONDS_AGO:
        raise ValueError("now is whole Unix seconds")
    nonce = secrets.token_bytes(32) if nonce is None else nonce
    if not isinstance(nonce, bytes) or len(nonce) != 32:
        raise ValueError("nonce is 32 bytes")
    return {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "TransferWithAuthorization": [
                {"name": "from", "type": "address"},
                {"name": "to", "type": "address"},
                {"name": "value", "type": "uint256"},
                {"name": "validAfter", "type": "uint256"},
                {"name": "validBefore", "type": "uint256"},
                {"name": "nonce", "type": "bytes32"},
            ],
        },
        "primaryType": "TransferWithAuthorization",
        "domain": {
            "name": terms.token.name,
            "version": terms.token.version,
            "chainId": terms.token.chain_id,
            "verifyingContract": terms.token.address,
        },
        "message": {
            "from": terms.payer,
            "to": terms.pay_to,
            "value": str(terms.amount_units),
            "validAfter": str(now - VALID_FROM_SECONDS_AGO),
            "validBefore": str(now + VALID_SECONDS),
            "nonce": "0x" + nonce.hex(),
        },
    }


def _hex_signature(signature: object) -> str:
    if isinstance(signature, (bytes, bytearray)) and signature:
        return "0x" + bytes(signature).hex()
    if isinstance(signature, str) and _HEX_RE.match(signature):
        return signature
    raise ValueError("sign() must return the signature as a 0x hex string or bytes.")


def payment_header(terms: Terms, typed: dict, signature: Union[str, bytes]) -> str:
    """The ``PAYMENT-SIGNATURE`` header: the x402 ``PaymentPayload`` with the
    terms exactly as AgentX sent them and the six signed fields."""
    payload: dict[str, Any] = {"x402Version": X402_VERSION}
    if terms.resource is not None:
        payload["resource"] = terms.resource
    payload["accepted"] = terms.accepted
    payload["payload"] = {"signature": _hex_signature(signature), "authorization": dict(typed["message"])}
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
    return base64.b64encode(body.encode("ascii")).decode("ascii")


def check_result(opened: object, fingerprint: Optional[str]) -> dict:
    """Check a paid answer: SHA-256 of the salt's bytes followed by the
    result's UTF-8 bytes is ``result_hash``, and that is the fingerprint read
    before paying. Raises :class:`ResultMismatch` (the money has moved)."""
    if not isinstance(opened, dict):
        raise ResultMismatch("The paid answer is not a JSON object.", {})
    result, salt, said = opened.get("result"), opened.get("salt"), opened.get("result_hash")
    try:
        digest = hashlib.sha256(bytes.fromhex(salt) + result.encode("utf-8")).hexdigest()
    except (AttributeError, TypeError, ValueError):
        raise ResultMismatch("The paid answer holds no result that can be checked.", opened) from None
    if digest != said:
        raise ResultMismatch("The result does not match the fingerprint AgentX gives with it.", opened)
    if fingerprint is not None and digest != fingerprint.lower():
        raise ResultMismatch("The result is not the one whose fingerprint was shown before paying. "
                             "Keep this answer; you can report the job as not as previewed.", opened)
    return opened


# ── client.payments ──────────────────────────────────────────────────────────

class PaymentsNamespace:
    """The buyer wallet and pay-to-open; ``client.payments``.

    Registering a wallet and paying need an access token issued in the last
    15 minutes; the client renews its pair by itself when AgentX asks.
    """

    #: How long, at most, one wait between two sends of the same payment is.
    MAX_WAIT_SECONDS = 30

    def __init__(self, client: "AgentXClient") -> None:
        self._client = client
        self._sleep: Callable[[float], None] = time.sleep
        self._clock: Callable[[], float] = time.monotonic

    # ── The buyer wallet (§6.2) ──────────────────────────────────────────────

    def buyer_wallet_record(self, agent_did: Optional[str] = None) -> dict[str, Any]:
        """A profile's buyer wallet and every past one
        (``GET /agents/{did}/buyer-wallet``; public). Defaults to your own."""
        did = agent_did or self._client._own_did("read the buyer wallet")
        return self._client._get(f"/agents/{_seg(did)}/buyer-wallet")

    def buyer_wallet_challenge(self, address: str) -> dict[str, Any]:
        """The text a new buyer wallet signs (``POST /agents/me/buyer-wallet/challenge``)."""
        return self._client._post(f"{BUYER_WALLET}/challenge", {"address": address})

    def register_buyer_wallet(self, address: str, signature: str) -> dict[str, Any]:
        """Register or change the wallet this profile pays from, with the
        wallet's signature (``PUT /agents/me/buyer-wallet``). In force at once.
        Registering moves no funds; every payment needs the wallet again."""
        return self._client._put(BUYER_WALLET, {"address": address, "signature": signature})

    def register_buyer_wallet_with(self, address: str, sign: TextSigner) -> dict[str, Any]:
        """:meth:`buyer_wallet_challenge`, ``sign``, :meth:`register_buyer_wallet`
        in one call. ``sign`` gets the text (``personal_sign``, EIP-191). It is
        not called unless AgentX's text says it moves no funds."""
        challenge = self.buyer_wallet_challenge(address)
        text = challenge.get("text_to_sign") if isinstance(challenge, dict) else None
        if not isinstance(text, str) or NO_FUNDS_SENTENCE not in text or challenge.get("moves_funds") is not False:
            raise _refuse("unreadable_prompt",
                          f"The text to sign does not say \"{NO_FUNDS_SENTENCE}\"")
        self._client._log.info("AgentX asks the wallet %s to sign: %s", short_address(address),
                               "register it as this profile's buyer wallet. " + NO_FUNDS_SENTENCE)
        return self.register_buyer_wallet(address, _hex_signature(sign(text)))

    def remove_buyer_wallet(self) -> dict[str, Any]:
        """Remove the buyer wallet with the token alone (``DELETE /agents/me/buyer-wallet``)."""
        return self._client._delete(BUYER_WALLET)

    # ── Pay to open (§4.2, §4.7) ─────────────────────────────────────────────

    def _ask(self, job: "_Job", header: Optional[str] = None) -> httpx.Response:
        extra = {PAYMENT_SIGNATURE_HEADER: header} if header is not None else None
        return self._client._send("GET", job.path, extra_headers=extra, checked=False)

    def fingerprint(self, task_id: str) -> str:
        """The fingerprint (``result_hash``) of the task's newest delivery, read
        from ``GET /tasks/{id}/deliveries``: what the full result must match."""
        return self._fingerprint(_Job(task_id=str(task_id)))

    def submission_fingerprint(self, bounty_id: str, submission_id: str) -> str:
        """The fingerprint (``result_hash``) of a submission, read from
        ``GET /markets/bounties/{id}/submissions`` as the bounty's creator."""
        return self._fingerprint(_Job(bounty_id=str(bounty_id), submission_id=str(submission_id)))

    def _submission(self, job: "_Job") -> Optional[dict]:
        """The submission's line of the bounty's list, as this profile reads it."""
        listed = self._client._get(f"/markets/bounties/{_seg(job.bounty_id)}/submissions")
        for line in listed if isinstance(listed, list) else []:
            if isinstance(line, dict) and _same_id(line.get("submission_id"), job.submission_id):
                return line
        return None

    def _fingerprint(self, job: "_Job") -> str:
        if job.bounty_id is not None:
            newest = self._submission(job)
        else:
            deliveries = self._client._get(f"/tasks/{_seg(job.task_id)}/deliveries")
            newest = deliveries[0] if isinstance(deliveries, list) and deliveries else None
        shown = newest.get("result_hash") if isinstance(newest, dict) else None
        if not isinstance(shown, str) or not _HASH_RE.match(shown.lower()):
            raise _refuse("no_delivery", f"The {job.name} has no delivery with a fingerprint to pay for.")
        return shown.lower()

    def terms(self, task_id: str, *, wallet: Optional[str] = None,
              max_usd: Optional[str] = None) -> Terms:
        """Read the three records and check them (§4.7), signing nothing.
        Raises :class:`PaymentCheckError`, or :class:`PaymentNotAccepted` when
        AgentX is not asking for a payment for this task now."""
        job = _Job(task_id=str(task_id))
        return self._checked(job, self._ask(job), wallet, max_usd)

    def submission_terms(self, bounty_id: str, submission_id: str, *, wallet: Optional[str] = None,
                         max_usd: Optional[str] = None) -> Terms:
        """:meth:`terms` for a submission to a bounty you created."""
        job = _Job(bounty_id=str(bounty_id), submission_id=str(submission_id))
        return self._checked(job, self._ask(job), wallet, max_usd)

    def _checked(self, job: "_Job", asked: httpx.Response, wallet: Optional[str],
                 max_usd: Optional[str]) -> Terms:
        _only_payment_required(asked)
        required = decode_payment_required(asked.headers.get(PAYMENT_REQUIRED_HEADER))
        if job.bounty_id is not None:
            bounty = self._client._get(f"/markets/bounties/{_seg(job.bounty_id)}")
            record = self._submission(job)
        else:
            bounty, record = None, self._client._get(f"/tasks/{_seg(job.task_id)}")
        shown = record.get("payment_terms") if isinstance(record, dict) else None
        worker = shown.get("worker_did") if isinstance(shown, dict) else None
        if not isinstance(worker, str) or not worker:
            raise _refuse("no_public_terms", f"The {job.name}'s public record shows no payment terms.")
        wallet_record = self._client._get(f"/agents/{_seg(worker)}/payout-wallet")
        identity = getattr(self._client, "identity", None)
        mine = dict(buyer_did=getattr(identity, "agent_did", None), wallet=wallet, max_usd=max_usd)
        if job.bounty_id is not None:
            return check_submission_terms(job.bounty_id, job.submission_id, bounty, record, wallet_record,
                                          required, **mine)
        return check_terms(job.task_id, record, wallet_record, required, **mine)

    def pay_and_open(
        self,
        task_id: str,
        sign: TypedSigner,
        *,
        wallet: Optional[str] = None,
        max_usd: Optional[str] = None,
        show: Optional[Callable[[str], Any]] = None,
        wait_seconds: float = 90,
    ) -> dict[str, Any]:
        """Pay the worker of a delivered task and get its full result.

        Reads the task's public record, the worker's wallet history and
        ``PAYMENT-REQUIRED``; refuses to sign unless they agree
        (:class:`PaymentCheckError`: nothing was signed). Logs the plain
        sentence, and calls ``show`` with it: if ``show`` returns ``False``
        the payment is not signed. Then ``sign`` gets the typed data once.

        Returns AgentX's answer: ``result``, ``salt``, ``result_hash``,
        ``content_type``, ``task_status`` and ``payment`` (with the
        ``transaction``). The result is checked against the fingerprint read
        before paying (:class:`ResultMismatch`). A task already paid is
        answered with its result and nothing is signed.

        ``wallet`` is the address ``sign`` signs for and ``max_usd`` the most
        you mean to pay; both are checked before signing when given.
        ``wait_seconds`` is how long the same payment is sent again while it
        settles (:class:`PaymentPending` after that).
        """
        return self._pay(_Job(task_id=str(task_id)), sign, wallet, max_usd, show, wait_seconds)

    def pay_and_open_submission(
        self,
        bounty_id: str,
        submission_id: str,
        sign: TypedSigner,
        *,
        wallet: Optional[str] = None,
        max_usd: Optional[str] = None,
        show: Optional[Callable[[str], Any]] = None,
        wait_seconds: float = 90,
    ) -> dict[str, Any]:
        """Pick the winner of a bounty you created by paying for it, and get
        its full result (after the bounty's deadline; one winner per bounty).

        As :meth:`pay_and_open`, with the submission in the task's place: the
        records read are the bounty, this submission's line of the bounty's
        list (its ``payment_terms``: the submitter's profile and its payout
        wallet as it stood when the submission was made), the submitter's
        wallet history and ``PAYMENT-REQUIRED``. The sentence shown says
        "for bounty 'title'". The result is checked against the fingerprint
        shown with the submission's preview.
        """
        job = _Job(bounty_id=str(bounty_id), submission_id=str(submission_id))
        return self._pay(job, sign, wallet, max_usd, show, wait_seconds)

    def _ready(self, job: "_Job", wallet: Optional[str],
               max_usd: Optional[str]) -> Union[dict[str, Any], "Unsigned"]:
        """Everything up to the signature: the job's result if it is already
        paid, else the checked payment for the wallet to sign. Raises as
        :meth:`terms` does. Signs and sends nothing."""
        fingerprint = self._fingerprint(job)
        asked = self._ask(job)
        if asked.status_code == 200:
            return check_result(asked.json(), fingerprint)
        terms = self._checked(job, asked, wallet, max_usd)
        return Unsigned(terms=terms, typed=typed_data(terms, now=_server_time(asked)), fingerprint=fingerprint)

    def _pay(self, job: "_Job", sign: TypedSigner, wallet: Optional[str], max_usd: Optional[str],
             show: Optional[Callable[[str], Any]], wait_seconds: float) -> dict[str, Any]:
        ready = self._ready(job, wallet, max_usd)
        if not isinstance(ready, Unsigned):
            return ready

        sentence = ready.terms.sentence
        self._client._log.warning("AgentX payment: %s", sentence)
        if show is not None and show(sentence) is False:
            raise _refuse("declined_by_you", "show() returned False.")

        header = payment_header(ready.terms, ready.typed, sign(copy.deepcopy(ready.typed)))
        return self._send_until_done(job, header, ready.fingerprint, wait_seconds)

    def send_again(self, task_id: str, payment_header: str, *, wait_seconds: float = 90) -> dict[str, Any]:
        """Send a payment that was already signed once more (after
        :class:`PaymentPending`, or a lost answer). Signs nothing; the same
        payment can only ever pay once."""
        job = _Job(task_id=str(task_id))
        return self._send_until_done(job, payment_header, self._fingerprint(job), wait_seconds)

    def send_submission_again(self, bounty_id: str, submission_id: str, payment_header: str, *,
                              wait_seconds: float = 90) -> dict[str, Any]:
        """:meth:`send_again` for a payment signed for a bounty's submission."""
        job = _Job(bounty_id=str(bounty_id), submission_id=str(submission_id))
        return self._send_until_done(job, payment_header, self._fingerprint(job), wait_seconds)

    def _send_until_done(self, job: "_Job", header: str, fingerprint: str,
                         wait_seconds: float) -> dict[str, Any]:
        give_up = self._clock() + max(0.0, float(wait_seconds))
        last = "no answer"
        while True:
            wait = 3.0
            try:
                answer = self._ask(job, header)
            except httpx.TransportError as lost:
                last = f"no answer ({type(lost).__name__})"
            else:
                if answer.status_code == 200:
                    return check_result(answer.json(), fingerprint)
                error, message, detail = _error_of(answer)
                settling = answer.status_code == 402 and error == "settlement_pending"
                if not settling and answer.status_code != 503:
                    raise PaymentNotAccepted(answer.status_code, error, message, detail=detail,
                                             retry_after=_retry_after(answer, detail), payment_header=header)
                last = error
                wait = float(_retry_after(answer, detail) or wait)
            wait = min(max(wait, 1.0), self.MAX_WAIT_SECONDS)
            if self._clock() + wait > give_up:
                raise PaymentPending(402, "settlement_pending",
                                     f"The payment was sent and is not settled yet ({last}). Do not sign "
                                     f"another: call client.payments.{job.again}, error.payment_header).",
                                     payment_header=header)
            self._sleep(wait)


@dataclass(frozen=True)
class Unsigned:
    """A payment that passed every check and is not signed yet: the terms,
    the typed data for the wallet, and the fingerprint the result must match."""
    terms: Terms
    typed: dict
    fingerprint: str


@dataclass(frozen=True)
class _Job:
    """Which job a payment is for: a task, or a submission to a bounty."""
    task_id: Optional[str] = None
    bounty_id: Optional[str] = None
    submission_id: Optional[str] = None

    def __post_init__(self) -> None:
        self.path           # an id that cannot be one path segment: ValueError, before any request

    @property
    def name(self) -> str:
        return "task" if self.bounty_id is None else "submission"

    @property
    def path(self) -> str:
        if self.bounty_id is None:
            return f"/tasks/{_seg(self.task_id)}/result/full"
        return f"/markets/bounties/{_seg(self.bounty_id)}/submissions/{_seg(self.submission_id)}/full"

    @property
    def again(self) -> str:
        if self.bounty_id is None:
            return "send_again(task_id"
        return "send_submission_again(bounty_id, submission_id"


def _error_of(answer: httpx.Response) -> tuple[str, str, Any]:
    try:
        detail = answer.json().get("detail")
    except Exception:
        detail = None
    if isinstance(detail, dict):
        return str(detail.get("error") or "unknown"), str(detail.get("message") or ""), detail
    return "unknown", str(detail or answer.text or ""), detail


def _retry_after(answer: httpx.Response, detail: Any) -> Optional[int]:
    for value in (answer.headers.get("Retry-After"), detail.get("retry_after") if isinstance(detail, dict) else None):
        try:
            seconds = int(value)
        except (TypeError, ValueError):
            continue
        if seconds > 0:
            return seconds
    return None


def _only_payment_required(asked: httpx.Response) -> None:
    """An answer leads to a signature only when it is the plain request to
    pay: 402 ``payment_required``. Anything else (a payment settling, an
    earlier one that can still settle, a hold, an error) is raised: signing
    then could pay twice or pay for nothing."""
    error, message, detail = _error_of(asked)
    if asked.status_code != 402 or error != "payment_required":
        raise PaymentNotAccepted(asked.status_code, error, message, detail=detail,
                                 retry_after=_retry_after(asked, detail))


def _server_time(answer: httpx.Response) -> int:
    """AgentX's clock from the answer's ``Date`` header when it is close to
    this machine's; else this machine's. The payment's five minutes are
    counted by AgentX's clock."""
    here = int(time.time())
    try:
        there = int(parsedate_to_datetime(answer.headers["Date"]).timestamp())
    except (KeyError, TypeError, ValueError):
        return here
    return there if abs(there - here) <= MAX_CLOCK_GAP else here


__all__ = [
    "PaymentsNamespace", "PaymentCheckError", "PaymentNotAccepted", "PaymentPending", "ResultMismatch",
    "Terms", "Token", "TOKENS", "Unsigned", "check_terms", "check_submission_terms", "typed_data", "payment_header", "check_result",
    "decode_payment_required", "short_address", "format_units",
]
