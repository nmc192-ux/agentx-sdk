"""AgentX SDK — ``agentx-pay``: pay to open from the command line, with the
wallet somewhere else (payments v1 §4.7, §6.4).

**This command holds no key and takes none.** It has two steps, and the
signature is made between them by whatever holds the buyer wallet (a
hardware wallet, a signing service, another machine)::

    agentx-pay check --task TASK_ID --out payment.json
    agentx-pay check --bounty BOUNTY_ID --submission SUBMISSION_ID --out payment.json

reads the job's public record, the worker's wallet history and AgentX's
payment request, refuses unless they agree (exactly the checks of
``client.payments.pay_and_open``), prints the plain sentence ("Pay 12.50 USDC
to @worker (0x12ab…89ef) for task 'title'. This moves funds. …") and writes
``payment.json``. Its ``typed_data`` is what the wallet signs
(``eth_signTypedData_v4``). Nothing is signed and nothing is sent. ::

    agentx-pay send payment.json --signature 0x…

checks the file against AgentX again, then sends the payment with the
signature and prints the full result as JSON. A file that was changed, a job
whose terms or delivery changed, or a payment past its few minutes is refused
before the signature leaves this machine.

Signing in: ``--identity FILE`` (an identity file saved by the SDK), or the
environment variables ``AGENTX_TOKEN`` and, optionally,
``AGENTX_REFRESH_TOKEN``. ``--base-url`` or ``AGENTX_BASE_URL`` for another
deployment.

Exit status: 0 done; 2 refused before anything was sent (the message says
why); 3 AgentX did not accept the payment; 4 the payment was sent and is not
settled yet (run the same ``send`` again; never sign another); 5 paid, and
the result does not match its fingerprint.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Optional, Sequence, TextIO

from .config import DEFAULT_BASE_URL
from .exceptions import AgentXError
from .payments import (
    MAX_CLOCK_GAP, TOKENS, VALID_SECONDS, PaymentCheckError, PaymentNotAccepted, PaymentPending, ResultMismatch, Terms,
    Unsigned, _address, _error_of, _hex_signature, _HASH_RE, _Job, _retry_after, _same, _server_time,
    _UNITS_RE, check_result, payment_header, short_address, typed_data,
)

FILE_KIND = "agentx-payment"
FILE_VERSION = 1
# AgentX takes a payment that still has 30 seconds to run (§5).
MIN_SECONDS_LEFT = 30
_MAX_FILE_BYTES = 64 * 1024

DONE, REFUSED, NOT_ACCEPTED, PENDING, MISMATCH = 0, 2, 3, 4, 5


def _refuse(reason: str, message: str) -> PaymentCheckError:
    return PaymentCheckError(reason, message)


# ── The payment file ─────────────────────────────────────────────────────────

def to_file(ready: Unsigned) -> dict[str, Any]:
    """What ``check`` writes: the job, the sentence, the checked terms, the
    typed data for the wallet and the fingerprint. No secret is in it."""
    terms = ready.terms
    job = ({"task_id": terms.task_id} if terms.bounty_id is None
           else {"bounty_id": terms.bounty_id, "submission_id": terms.submission_id})
    return {
        "kind": FILE_KIND,
        "version": FILE_VERSION,
        "job": job,
        "sentence": terms.sentence,
        "sign_before": _iso(int(ready.typed["message"]["validBefore"]) - MIN_SECONDS_LEFT),
        "terms": {
            "title": terms.title, "worker_did": terms.worker_did, "worker_handle": terms.worker_handle,
            "network": terms.network, "amount": str(terms.amount_units), "payer": terms.payer,
            "pay_to": terms.pay_to, "accepted": terms.accepted, "resource": terms.resource,
        },
        "fingerprint": ready.fingerprint,
        "typed_data": ready.typed,
    }


def from_file(filed: object) -> tuple[_Job, Unsigned]:
    """Read a payment file back, or refuse (``unreadable_file``). The token
    is the SDK's own for the network named, never the file's, and the typed
    data must be exactly the one transfer this SDK builds for the terms."""
    def bad(why: str) -> PaymentCheckError:
        return _refuse("unreadable_file", f"The payment file {why}. Run `agentx-pay check` again.")

    if not isinstance(filed, dict) or filed.get("kind") != FILE_KIND or filed.get("version") != FILE_VERSION:
        raise bad("is not an agentx-pay file of this version")
    ids, shown, typed = filed.get("job"), filed.get("terms"), filed.get("typed_data")
    if not isinstance(ids, dict) or not isinstance(shown, dict) or not isinstance(typed, dict):
        raise bad("is incomplete")
    if not all(isinstance(value, str) for value in ids.values()):
        raise bad("names no job")
    try:
        if set(ids) == {"task_id"}:
            job = _Job(task_id=ids["task_id"])
        elif set(ids) == {"bounty_id", "submission_id"}:
            job = _Job(bounty_id=ids["bounty_id"], submission_id=ids["submission_id"])
        else:
            raise ValueError
    except ValueError:
        raise bad("names no job") from None
    texts = ("title", "worker_did", "worker_handle", "network", "amount", "payer", "pay_to")
    if not all(isinstance(shown.get(name), str) for name in texts) or not _UNITS_RE.match(shown["amount"]):
        raise bad("holds terms that cannot be read")
    token = TOKENS.get(shown["network"])
    accepted, resource = shown.get("accepted"), shown.get("resource")
    if token is None or not isinstance(accepted, dict) or not (resource is None or isinstance(resource, dict)):
        raise bad("holds terms that cannot be read")
    fingerprint = filed.get("fingerprint")
    if not isinstance(fingerprint, str) or not _HASH_RE.match(fingerprint):
        raise bad("holds no fingerprint")
    try:
        terms = Terms(
            task_id=job.task_id, title=shown["title"], worker_did=shown["worker_did"],
            worker_handle=shown["worker_handle"], network=shown["network"], token=token,
            amount_units=int(shown["amount"]), payer=_address(shown["payer"], "unreadable_file", "payer"),
            pay_to=_address(shown["pay_to"], "unreadable_file", "pay_to"), accepted=accepted,
            resource=resource, bounty_id=job.bounty_id, submission_id=job.submission_id)
        message = typed["message"]
        ends, nonce = int(message["validBefore"]), bytes.fromhex(message["nonce"][2:])
        rebuilt = typed_data(terms, now=ends - VALID_SECONDS, nonce=nonce)
    except (PaymentCheckError, KeyError, TypeError, ValueError):
        raise bad("holds typed data that cannot be read") from None
    if rebuilt != typed:
        raise bad("holds typed data that is not the payment of its terms")
    return job, Unsigned(terms=terms, typed=typed, fingerprint=fingerprint)


def _iso(moment: int) -> str:
    return datetime.fromtimestamp(moment, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _unchanged(filed: Terms, now: Terms) -> bool:
    """The job's terms as AgentX gives them now are the ones in the file."""
    return (filed.worker_did == now.worker_did and filed.network == now.network
            and filed.amount_units == now.amount_units and _same(filed.payer, now.payer)
            and _same(filed.pay_to, now.pay_to) and filed.accepted == now.accepted
            and filed.resource == now.resource)


# ── The two steps ────────────────────────────────────────────────────────────

def check(client: Any, job: _Job, out_path: str, *, wallet: Optional[str], max_usd: Optional[str],
          out: TextIO) -> int:
    ready = client.payments._ready(job, wallet, max_usd)
    if not isinstance(ready, Unsigned):
        print("This job is already paid. Nothing to sign. Its result:", file=sys.stderr)
        json.dump(ready, out, indent=2)
        out.write("\n")
        return DONE
    filed = to_file(ready)
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(filed, handle, indent=2)
        handle.write("\n")
    message = ready.typed["message"]
    print(ready.terms.sentence, file=out)
    print(f"\nFrom your buyer wallet {short_address(message['from'])}, on {ready.terms.network}.", file=out)
    print(f"Wrote {out_path}. Nothing was signed or sent.", file=out)
    print(f"Have the buyer wallet sign its `typed_data` (eth_signTypedData_v4) and, before "
          f"{filed['sign_before']}, run:\n  agentx-pay send {out_path} --signature 0x…", file=out)
    return DONE


def send_checked(client: Any, filed: object, signature: object, *, wait_seconds: float) -> dict[str, Any]:
    """``send`` without the printing: the payment file is read and checked
    against AgentX again, and only then sent with the signature. Returns the
    full result. Raises as ``client.payments.pay_and_open`` does; a
    :class:`PaymentCheckError` means the signature never left this machine.

    One case is sent without the check of the job's terms and delivery:
    while a payment for the job is settling (an earlier ``send`` that got no
    answer), the file's time window is still checked and the signature is
    then passed on as it is. The same signed payment can only ever pay once,
    and AgentX checks it against the job's terms itself."""
    job, ready = from_file(filed)
    try:
        signature = _hex_signature(signature.strip() if isinstance(signature, str) else signature)
    except ValueError:
        raise _refuse("unreadable_signature", "The signature is not a 0x hex string.") from None
    payments = client.payments
    asked = payments._ask(job)
    if asked.status_code == 200:
        # Already paid: the result, and the signature stays here.
        opened = check_result(asked.json(), ready.fingerprint)
    else:
        error, message, detail = _error_of(asked)
        # `check` writes a payment that ends five minutes on. One that ends
        # later was not written by `check` (the file was altered before the
        # wallet signed): its signature is not passed on, settling or not.
        if int(ready.typed["message"]["validBefore"]) - _server_time(asked) > VALID_SECONDS + MAX_CLOCK_GAP:
            raise _refuse("payment_window_wrong", "The payment in the file ends later than a payment "
                                                  "`agentx-pay check` writes. It was not sent. Run "
                                                  "`agentx-pay check` again and sign the new one.")
        if asked.status_code == 402 and error == "payment_required":
            # Not paid and no payment under way: the file is checked against
            # AgentX's records of this moment before the signature is sent.
            if not _unchanged(ready.terms, payments._checked(job, asked, None, None)):
                raise _refuse("terms_changed", "The job's payment terms are no longer the ones in the "
                                               "file. Run `agentx-pay check` again.")
            if payments._fingerprint(job) != ready.fingerprint:
                raise _refuse("delivery_changed", "The job's delivery is no longer the one the file was "
                                                  "made for. Run `agentx-pay check` again.")
            if int(ready.typed["message"]["validBefore"]) - _server_time(asked) < MIN_SECONDS_LEFT:
                raise _refuse("payment_expired", "The payment in the file has run out of time. Run "
                                                 "`agentx-pay check` again and sign the new one.")
        elif not (asked.status_code == 402 and error == "settlement_pending"):
            # The job cannot be paid for now: AgentX's answer, and nothing sent.
            raise PaymentNotAccepted(asked.status_code, error, message, detail=detail,
                                     retry_after=_retry_after(asked, detail))
        # (Settling: a payment for this job is under way, which is this one
        # if an earlier `send` got no answer. The same payment can only ever
        # pay once, and AgentX tells its own payment from any other.)
        header = payment_header(ready.terms, ready.typed, signature)
        opened = payments._send_until_done(job, header, ready.fingerprint, wait_seconds)
    return opened


def send(client: Any, filed: object, signature: object, *, wait_seconds: float, out: TextIO) -> int:
    opened = send_checked(client, filed, signature, wait_seconds=wait_seconds)
    json.dump(opened, out, indent=2)
    out.write("\n")
    return DONE


# ── The command ──────────────────────────────────────────────────────────────

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentx-pay",
        description="Pay to open a delivered AgentX task or a bounty's winning submission. "
                    "Holds no key: `check` writes the payment for your wallet to sign, "
                    "`send` takes the signature made elsewhere.")
    steps = parser.add_subparsers(dest="step", required=True)

    def signing_in(step: argparse.ArgumentParser) -> None:
        step.add_argument("--identity", help="identity file saved by the SDK (else AGENTX_TOKEN)")
        step.add_argument("--base-url", help=f"AgentX API (else AGENTX_BASE_URL, else {DEFAULT_BASE_URL})")

    first = steps.add_parser("check", help="check the job's records and write the payment to sign")
    first.add_argument("--task", help="the task to pay for")
    first.add_argument("--bounty", help="the bounty (with --submission)")
    first.add_argument("--submission", help="the submission to pay for: the winner")
    first.add_argument("--out", required=True, help="where to write the payment file")
    first.add_argument("--wallet", help="the address that will sign; refused if the job is paid from another")
    first.add_argument("--max-usd", help="the most you mean to pay, e.g. 15.00")
    signing_in(first)

    second = steps.add_parser("send", help="send the payment with the signature made elsewhere")
    second.add_argument("file", help="the payment file `check` wrote")
    how = second.add_mutually_exclusive_group(required=True)
    how.add_argument("--signature", help="the wallet's signature over the file's typed_data (0x hex)")
    how.add_argument("--signature-file", help="a file holding that signature")
    second.add_argument("--wait", type=float, default=90, help="seconds to wait while it settles (90)")
    signing_in(second)
    return parser


def _client(args: argparse.Namespace) -> Any:
    from .client import AgentXClient
    base_url = args.base_url or os.environ.get("AGENTX_BASE_URL") or DEFAULT_BASE_URL
    if args.identity:
        if not os.path.isfile(args.identity):
            raise _refuse("no_login", f"No identity file at {args.identity}.")
        return AgentXClient(api_key="", base_url=base_url, identity_path=args.identity, log_level="ERROR")
    token = os.environ.get("AGENTX_TOKEN")
    if not token:
        raise _refuse("no_login", "Give --identity FILE, or set AGENTX_TOKEN.")
    return AgentXClient(api_key=token, base_url=base_url, log_level="ERROR",
                        refresh_token=os.environ.get("AGENTX_REFRESH_TOKEN") or None)


def _job(args: argparse.Namespace) -> _Job:
    try:
        if args.task and not (args.bounty or args.submission):
            return _Job(task_id=args.task)
        if args.bounty and args.submission and not args.task:
            return _Job(bounty_id=args.bounty, submission_id=args.submission)
    except ValueError:
        pass
    raise _refuse("no_job", "Give --task ID, or --bounty ID with --submission ID.")


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read(_MAX_FILE_BYTES + 1)
    if len(text) > _MAX_FILE_BYTES:
        raise _refuse("unreadable_file", f"{path} is too large to be what is expected.")
    return text


def main(argv: Optional[Sequence[str]] = None, *, client: Any = None, out: Optional[TextIO] = None) -> int:
    """Run ``agentx-pay``. ``client`` and ``out`` are for tests."""
    args = _parser().parse_args(argv)
    out = out or sys.stdout
    made = None
    try:
        if args.step == "check":
            job = _job(args)
            client = client or (made := _client(args))
            return check(client, job, args.out, wallet=args.wallet, max_usd=args.max_usd, out=out)
        try:
            filed = json.loads(_read(args.file))
            signature = args.signature if args.signature is not None else _read(args.signature_file)
        except (OSError, ValueError) as wrong:
            raise _refuse("unreadable_file", f"Could not read it ({type(wrong).__name__}).") from None
        client = client or (made := _client(args))
        return send(client, filed, signature, wait_seconds=args.wait, out=out)
    except PaymentCheckError as refused:
        print(f"{refused}\nNothing was sent.", file=sys.stderr)
        return REFUSED
    except PaymentPending as pending:
        print(f"{pending.error}: the payment was sent and is not settled yet. Do NOT sign another. "
              f"Run the same `agentx-pay send` again in a minute.", file=sys.stderr)
        return PENDING
    except PaymentNotAccepted as no:
        wait = f" Wait {no.retry_after} seconds before signing another." if no.signed and no.retry_after else ""
        sent = "" if no.signed else " Nothing was sent."
        print(f"AgentX did not accept it: {no}.{wait}{sent}", file=sys.stderr)
        return NOT_ACCEPTED if no.signed else REFUSED
    except ResultMismatch as mismatch:
        print(f"The payment went through, but: {mismatch}", file=sys.stderr)
        json.dump(mismatch.opened, out, indent=2)
        out.write("\n")
        return MISMATCH
    except AgentXError as failed:
        print(f"AgentX could not be asked: {failed}", file=sys.stderr)
        return REFUSED
    finally:
        if made is not None:
            made.close()


if __name__ == "__main__":
    sys.exit(main())
