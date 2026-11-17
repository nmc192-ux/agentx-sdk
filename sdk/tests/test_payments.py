"""Paying to open a task, and the buyer wallet, in the Python SDK (payments v1
§4.2, §4.7, §6.2, §6.4; plan step W2-12a; abuse rows A57 and A61, client part).

The SDK signs nothing: the payment's signature comes from the caller's
``sign`` function. What is proven here is when that function is NOT called:
whenever the task's public record, the worker's wallet history and
``PAYMENT-REQUIRED`` do not agree, and whenever AgentX answers anything but
the plain request to pay. And that a signed payment is sent again as it is,
never signed twice.
"""

import base64
import copy
import hashlib
import json
import logging

import httpx
import pytest
import respx

from agentx_sdk import (
    AgentXClient, PaymentCheckError, PaymentNotAccepted, PaymentPending, PaymentsNamespace, ResultMismatch,
)
from agentx_sdk import payments as pay
from agentx_sdk.auth import AgentIdentity

BASE = "http://test.agentx.local"
BUYER = "did:agentx:buyer-001"
WORKER = "did:agentx:worker-002"
TASK = "9d2c1f6e-0000-4000-8000-00000000abcd"
PAYER = "0x1111111111111111111111111111111111111111"
PAY_TO = "0x12aB34cD56eF7890aBcDeF1234567890abCD89Ef"
THIEF = "0x9999999999999999999999999999999999999999"
USDC = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
FULL = f"{BASE}/tasks/{TASK}/result/full"
SALT = "ab" * 32
RESULT = "the full result"
FINGERPRINT = hashlib.sha256(bytes.fromhex(SALT) + RESULT.encode()).hexdigest()
NOW = 1_800_000_000


def task(**terms) -> dict:
    shown = {
        "buyer_did": BUYER, "worker_did": WORKER, "worker_handle": "worker_002",
        "pay_to": PAY_TO, "payer": PAYER, "network": "eip155:84532", "asset": USDC,
        "amount": "12500000", "amount_usd": "12.50", "assigned_at": "2026-11-16T10:00:00+00:00",
    }
    shown.update(terms)
    return {
        "task_id": TASK, "requester_agent_did": BUYER, "executor_agent_did": WORKER,
        "task_type": "summarise", "payload": {"title": "Summarise the Q3 report"}, "status": "delivered",
        "price_usd": "15.00", "agreed_price_usd": "12.50", "payment_terms": shown,
    }


def wallet_record(*entries) -> dict:
    history = list(entries) or [entry()]
    return {"agent_did": WORKER, "payout_wallet": history[0], "pending": None, "history": history}


def entry(address=PAY_TO, status="in_force", effective_at="2026-11-09T10:00:00+00:00", ended_at=None) -> dict:
    return {"address": address, "wallet_kind": "plain", "status": status,
            "set_at": "2026-11-06T10:00:00+00:00", "effective_at": effective_at, "ended_at": ended_at}


def required(**accepted) -> dict:
    one = {"scheme": "exact", "network": "eip155:84532", "amount": "12500000", "asset": USDC,
           "payTo": PAY_TO, "maxTimeoutSeconds": 300, "extra": {"name": "USDC", "version": "2"}}
    one.update(accepted)
    return {
        "x402Version": 2, "error": "PAYMENT-SIGNATURE header is required",
        "resource": {"url": f"https://api.agentx.example/tasks/{TASK}/result/full",
                     "description": "Full result", "mimeType": "application/json"},
        "accepts": [one],
    }


def header(message: dict) -> str:
    return base64.b64encode(json.dumps(message).encode()).decode()


def checked(a_task=None, record=..., asked=None, **more) -> pay.Terms:
    record = wallet_record() if record is ... else record
    return pay.check_terms(TASK, a_task or task(), record, asked or required(), **more)


def refused(**kw) -> str:
    with pytest.raises(PaymentCheckError) as caught:
        checked(**kw)
    return caught.value.reason


# ── The three records (§4.7) ─────────────────────────────────────────────────

def test_three_records_that_agree_are_the_payment():
    terms = checked(buyer_did=BUYER, wallet=PAYER, max_usd="12.50")
    assert (terms.pay_to, terms.payer, terms.amount_units) == (PAY_TO, PAYER, 12_500_000)
    assert terms.token is pay.TOKENS["eip155:84532"] and terms.accepted == required()["accepts"][0]


def test_the_sentence_is_the_one_of_section_6_4():
    assert checked().sentence == (
        "Pay 12.50 USDC to @worker_002 (0x12aB…89Ef) for task 'Summarise the Q3 report'. "
        "This moves funds. It can be used once, in the next few minutes, and for nothing else.")


def test_a57_pay_to_that_differs_from_the_public_snapshot_is_refused():
    assert refused(asked=required(payTo=THIEF)) == "pay_to_differs"
    # One character is enough.
    assert refused(asked=required(payTo=PAY_TO[:-1] + "0")) == "pay_to_differs"


def test_a57_snapshot_that_the_wallet_history_does_not_show_is_refused():
    # The record and PAYMENT-REQUIRED agree on an address the worker never had.
    assert refused(a_task=task(pay_to=THIEF), asked=required(payTo=THIEF)) == "wallet_not_in_history"


@pytest.mark.parametrize("history", [
    [],                                                                       # no wallet at all
    [entry(effective_at="2026-11-16T10:00:01+00:00")],                        # in force a second too late
    [entry(status="past", ended_at="2026-11-16T10:00:00+00:00")],            # ended at that very moment
    [entry(status="removed", ended_at="2026-11-15T10:00:00+00:00")],         # removed the day before
    [entry(status="pending")],                                                # still in its wait
    [entry(status="cancelled", ended_at="2026-11-20T10:00:00+00:00")],       # never came into force
    [entry(effective_at=None)], [entry(effective_at="soon")],                 # no readable time
    [entry(status="past", ended_at="when")],
    [entry(address=THIEF)],
    ["0x"], [None],
])
def test_a57_wallet_must_have_been_in_force_at_assignment(history):
    record = {"agent_did": WORKER, "history": history}
    assert refused(record=record) == "wallet_not_in_history"


def test_a_wallet_since_replaced_still_counts_for_a_job_assigned_under_it():
    newer = entry(address=THIEF, effective_at="2026-11-17T10:00:00+00:00")
    older = entry(status="past", ended_at="2026-11-17T10:00:00+00:00")
    assert checked(record=wallet_record(newer, older)).pay_to == PAY_TO


def test_the_history_must_be_the_workers_own():
    assert refused(record={**wallet_record(), "agent_did": "did:agentx:someone-003"}) == "wallet_not_in_history"
    assert refused(record=None) == "wallet_not_in_history"


@pytest.mark.parametrize("change, reason", [
    (dict(asked=required(network="eip155:8453")), "network_differs"),
    (dict(asked=required(asset=THIEF)), "unknown_token"),                     # a look-alike token
    (dict(asked=required(extra={"name": "USD Coin", "version": "2"})), "unknown_token"),
    (dict(asked=required(extra={"name": "USDC", "version": "1"})), "unknown_token"),
    (dict(asked=required(extra={"name": "USDC", "version": "2", "assetTransferMethod": "permit2"})),
     "unknown_token"),
    (dict(asked=required(extra={"name": "USDC", "version": "2", "more": 1})), "unknown_token"),
    (dict(asked=required(extra=None)), "unknown_token"),
    (dict(asked=required(amount="12500001")), "amount_differs"),
    (dict(asked=required(amount=12500000)), "amount_differs"),
    (dict(asked=required(scheme="upto")), "not_x402_v2"),
    (dict(asked=required(approve=True)), "not_x402_v2"),
    (dict(asked={**required(), "x402Version": "2"}), "not_x402_v2"),
    (dict(asked={**required(), "x402Version": True}), "not_x402_v2"),
    (dict(asked={**required(), "x402Version": 1}), "not_x402_v2"),
    (dict(asked={**required(), "x402Version": 2.0}), "not_x402_v2"),
    (dict(asked={**required(), "accepts": required()["accepts"] * 2}), "not_x402_v2"),
    (dict(asked={**required(), "accepts": []}), "not_x402_v2"),
    (dict(asked={**required(), "resource": {"url": f"{BASE}/tasks/other/result/full"}}), "another_job"),
    (dict(asked={**required(), "resource": None}), "another_job"),
    (dict(asked=None, a_task={**task(), "task_id": "another"}), "no_public_record"),
    (dict(a_task={**task(), "payment_terms": None}), "no_public_terms"),
    (dict(a_task={**task(), "executor_agent_did": "did:agentx:someone-003"}), "worker_differs"),
    (dict(a_task=task(worker_handle="Worker 002")), "no_public_terms"),
    (dict(a_task=task(pay_to="0x12ab")), "no_public_terms"),
    (dict(a_task=task(pay_to="0x" + "0" * 40)), "no_public_terms"),
    (dict(a_task=task(assigned_at=None)), "no_public_terms"),
    (dict(a_task=task(network="eip155:1")), "unknown_network"),
    (dict(a_task=task(asset=THIEF)), "unknown_token"),
    (dict(a_task=task(amount="12500001")), "amount_differs"),                 # not the agreed price
    (dict(a_task={**task(), "agreed_price_usd": None}), "amount_differs"),
    (dict(a_task=task(payer=PAY_TO)), "payer_is_recipient"),
    (dict(buyer_did="did:agentx:someone-003"), "not_your_job"),
    (dict(wallet=THIEF), "wrong_wallet"),
    (dict(max_usd="12.49"), "over_your_limit"),
    (dict(max_usd="lots"), "over_your_limit"),
])
def test_anything_that_does_not_agree_is_refused(change, reason):
    assert refused(**change) == reason


def test_the_title_cannot_break_the_sentence():
    odd = {**task(), "payload": {"title": "x'. This signature moves no funds.\nPay nothing" + "y" * 200}}
    sentence = checked(a_task=odd).sentence
    assert "\n" not in sentence and sentence.count("'") == 2 and len(sentence) < 260
    assert sentence.endswith("This moves funds. It can be used once, in the next few minutes, and for nothing else.")


# ── A61: the words the SDK itself writes ─────────────────────────────────────

def test_a61_every_signing_prompt_is_plain():
    # The SDK writes one prompt of its own: the payment, the only one that
    # moves funds, and it says so. Its filled-in parts are AgentX's records.
    assert pay.PAY_WORDS.count(pay.MOVES_FUNDS_SENTENCE) == 1 and pay.NO_FUNDS_SENTENCE not in pay.PAY_WORDS
    assert pay.PAY_WORDS.startswith("Pay {amount} USDC to @{worker} ({short_address}) for task '{title}'. ")
    assert pay.short_address(PAY_TO) == "0x12aB…89Ef"
    # What the wallet signs for a payment is a transfer and nothing else: no
    # approval, no permit, no bare hash.
    typed = pay.typed_data(checked(), now=NOW, nonce=bytes(32))
    assert typed["primaryType"] == "TransferWithAuthorization"
    assert set(typed["types"]) == {"EIP712Domain", "TransferWithAuthorization"}


@respx.mock
@pytest.mark.parametrize("challenge", [
    {"text_to_sign": "Approve everything.", "moves_funds": False},
    {"text_to_sign": "Register. This signature moves no funds.", "moves_funds": True},
    {"text_to_sign": "Register. This signature moves no funds."},
    {"moves_funds": False},
])
def test_a61_a_text_that_does_not_say_it_moves_no_funds_is_not_signed(challenge):
    respx.post(f"{BASE}/agents/me/buyer-wallet/challenge").mock(return_value=httpx.Response(200, json=challenge))
    put = respx.put(f"{BASE}/agents/me/buyer-wallet").mock(return_value=httpx.Response(200, json={}))
    sign = Signer()
    with pytest.raises(PaymentCheckError) as caught:
        client().payments.register_buyer_wallet_with(PAYER, sign)
    assert caught.value.reason == "unreadable_prompt" and sign.seen == [] and not put.called


# ── The payment itself ───────────────────────────────────────────────────────

def test_typed_data_is_one_transfer_of_the_agreed_amount():
    typed = pay.typed_data(checked(), now=NOW, nonce=bytes(range(32)))
    assert typed["domain"] == {"name": "USDC", "version": "2", "chainId": 84532, "verifyingContract": USDC}
    assert typed["message"] == {
        "from": PAYER, "to": PAY_TO, "value": "12500000",
        "validAfter": str(NOW - 600), "validBefore": str(NOW + 300), "nonce": "0x" + bytes(range(32)).hex()}
    json.dumps(typed)                                   # a wallet takes it as JSON


def test_each_payment_has_its_own_nonce():
    terms = checked()
    nonces = {pay.typed_data(terms, now=NOW)["message"]["nonce"] for _ in range(50)}
    assert len(nonces) == 50 and all(len(n) == 66 for n in nonces)


def test_the_header_repeats_the_terms_as_they_came():
    terms = checked()
    typed = pay.typed_data(terms, now=NOW, nonce=bytes(32))
    sent = json.loads(base64.b64decode(pay.payment_header(terms, typed, b"\x01" * 65)))
    assert sent["x402Version"] == 2 and sent["accepted"] == required()["accepts"][0]
    assert sent["payload"] == {"signature": "0x" + "01" * 65, "authorization": typed["message"]}
    assert set(sent) == {"x402Version", "resource", "accepted", "payload"}


@pytest.mark.parametrize("signature", [None, "", "sig", "0x", "0xabc", 5])
def test_a_signature_that_is_not_hex_is_not_sent(signature):
    terms = checked()
    with pytest.raises(ValueError):
        pay.payment_header(terms, pay.typed_data(terms, now=NOW), signature)


# ── pay_and_open ─────────────────────────────────────────────────────────────

class Signer:
    def __init__(self, signature="0x" + "5a" * 65):
        self.signature, self.seen = signature, []

    def __call__(self, what):
        self.seen.append(copy.deepcopy(what))
        return self.signature


def client() -> AgentXClient:
    made = AgentXClient(api_key="tok", base_url=BASE, max_retries=0)
    made.identity = AgentIdentity(agent_did=BUYER, api_key="tok")
    made.slept = []  # type: ignore[attr-defined]
    made.payments._sleep = made.slept.append
    return made


def paid(result=RESULT, **more) -> dict:
    return {"task_id": TASK, "task_status": "paid", "result": result, "salt": SALT,
            "result_hash": hashlib.sha256(bytes.fromhex(SALT) + result.encode()).hexdigest(),
            "content_type": "text/plain", "result_bytes": len(result),
            "payment": {"transaction": "0x" + "77" * 32, "amount": "12500000"}, **more}


def no(status: int, error: str, *, terms: bool = False, retry_after: int | None = None) -> httpx.Response:
    headers = {}
    if terms:
        headers["PAYMENT-REQUIRED"] = header(required())
    if retry_after:
        headers["Retry-After"] = str(retry_after)
    return httpx.Response(status, json={"detail": {"error": error, "message": error}}, headers=headers)


def the_job(*answers, a_task=None, record=None):
    """Mock the routes pay_and_open reads; ``answers`` come from result/full in order."""
    respx.get(f"{BASE}/tasks/{TASK}/deliveries").mock(return_value=httpx.Response(200, json=[
        {"delivery_id": "d2", "result_hash": FINGERPRINT}, {"delivery_id": "d1", "result_hash": "00" * 32}]))
    respx.get(f"{BASE}/tasks/{TASK}").mock(return_value=httpx.Response(200, json=a_task or task()))
    respx.get(f"{BASE}/agents/{WORKER}/payout-wallet").mock(
        return_value=httpx.Response(200, json=record or wallet_record()))
    return respx.get(FULL).mock(side_effect=list(answers))


def sent_headers(full) -> list:
    return [call.request.headers.get("PAYMENT-SIGNATURE") for call in full.calls]


@respx.mock
def test_pay_and_open_checks_shows_signs_once_and_opens(caplog):
    full = the_job(no(402, "payment_required", terms=True), httpx.Response(200, json=paid()))
    sign, shown, me = Signer(), [], client()
    with caplog.at_level(logging.WARNING):
        opened = me.payments.pay_and_open(TASK, sign, wallet=PAYER, max_usd="20", show=shown.append)
    assert opened["result"] == RESULT and opened["task_status"] == "paid"
    assert len(sign.seen) == 1 and sign.seen[0]["message"]["to"] == PAY_TO
    first, second = sent_headers(full)
    assert first is None
    sent = json.loads(base64.b64decode(second))
    assert sent["payload"]["authorization"] == sign.seen[0]["message"]
    assert sent["payload"]["signature"] == sign.signature
    # The plain sentence went to show() and to the log, before signing.
    assert shown == [pay.check_terms(TASK, task(), wallet_record(), required()).sentence]
    assert shown[0] in caplog.text


@respx.mock
@pytest.mark.parametrize("a_task, record, reason", [
    (task(pay_to=THIEF), wallet_record(entry(address=THIEF)), "pay_to_differs"),
    (None, wallet_record(entry(address=THIEF)), "wallet_not_in_history"),
    ({**task(), "payment_terms": None}, None, "no_public_terms"),
])
def test_a57_pay_and_open_does_not_sign_when_the_records_disagree(a_task, record, reason):
    full = the_job(no(402, "payment_required", terms=True), a_task=a_task, record=record)
    sign = Signer()
    with pytest.raises(PaymentCheckError) as caught:
        client().payments.pay_and_open(TASK, sign)
    assert caught.value.reason == reason and sign.seen == [] and sent_headers(full) == [None]


@respx.mock
def test_show_returning_false_stops_before_the_wallet():
    full = the_job(no(402, "payment_required", terms=True))
    sign = Signer()
    with pytest.raises(PaymentCheckError) as caught:
        client().payments.pay_and_open(TASK, sign, show=lambda sentence: False)
    assert caught.value.reason == "declined_by_you" and sign.seen == [] and len(full.calls) == 1


@respx.mock
@pytest.mark.parametrize("answer", [
    no(402, "settlement_pending", terms=True, retry_after=3),
    no(402, "earlier_payment_can_still_settle", terms=True, retry_after=200),
    no(402, "payment_not_accepted", terms=True, retry_after=200),
    no(409, "payment_on_hold"), no(409, "nothing_to_pay_for"), no(403, "not_your_job"),
    no(503, "facilitator_unavailable", retry_after=30), no(429, "too_many_refused_payments"),
    httpx.Response(402, json={"detail": "pay"}, headers={"PAYMENT-REQUIRED": header(required())}),
])
def test_only_the_plain_request_to_pay_leads_to_a_signature(answer):
    full = the_job(answer)
    sign = Signer()
    with pytest.raises(PaymentNotAccepted) as caught:
        client().payments.pay_and_open(TASK, sign)
    assert sign.seen == [] and len(full.calls) == 1 and caught.value.signed is False


@respx.mock
def test_a_paid_task_is_opened_again_without_signing():
    full = the_job(httpx.Response(200, json=paid()))
    sign = Signer()
    assert client().payments.pay_and_open(TASK, sign)["result"] == RESULT
    assert sign.seen == [] and len(full.calls) == 1


@respx.mock
def test_a_settling_payment_is_sent_again_as_it_is_and_never_signed_twice():
    full = the_job(no(402, "payment_required", terms=True),
                   no(402, "settlement_pending", terms=True, retry_after=4),
                   no(503, "chain_unavailable", retry_after=10),
                   httpx.ConnectError("gone"),
                   httpx.Response(200, json=paid()))
    sign, me = Signer(), client()
    assert me.payments.pay_and_open(TASK, sign)["result"] == RESULT
    headers = sent_headers(full)
    assert len(sign.seen) == 1 and headers[0] is None
    assert len(set(headers[1:])) == 1 and len(headers) == 5
    assert me.slept == [4.0, 10.0, 3.0]


@respx.mock
def test_waiting_ends_with_the_signed_payment_in_hand_and_it_can_be_sent_again():
    full = the_job(no(402, "payment_required", terms=True),
                   *[no(402, "settlement_pending", terms=True, retry_after=20)] * 2,
                   httpx.Response(200, json=paid()))
    sign, me = Signer(), client()
    ticks = iter(range(0, 1000, 15))
    me.payments._clock = lambda: next(ticks)
    with pytest.raises(PaymentPending) as caught:
        me.payments.pay_and_open(TASK, sign, wait_seconds=30)
    assert caught.value.signed and caught.value.payment_header == sent_headers(full)[1]
    assert me.payments.send_again(TASK, caught.value.payment_header)["result"] == RESULT
    assert len(sign.seen) == 1 and len(set(sent_headers(full)[1:])) == 1


@respx.mock
def test_a_refused_payment_is_raised_with_its_wait_and_not_signed_again():
    full = the_job(no(402, "payment_required", terms=True),
                   no(402, "payment_not_accepted", terms=True, retry_after=240))
    sign = Signer()
    with pytest.raises(PaymentNotAccepted) as caught:
        client().payments.pay_and_open(TASK, sign)
    error = caught.value
    assert (error.error, error.retry_after, error.signed, error.status_code) == (
        "payment_not_accepted", 240, True, 402)
    assert not isinstance(error, PaymentPending) and len(sign.seen) == 1 and len(full.calls) == 2


@respx.mock
def test_a_result_that_is_not_the_one_previewed_is_said_so():
    the_job(no(402, "payment_required", terms=True), httpx.Response(200, json=paid("something else")))
    with pytest.raises(ResultMismatch) as caught:
        client().payments.pay_and_open(TASK, Signer())
    assert caught.value.opened["result"] == "something else"


@pytest.mark.parametrize("opened", [
    None, {}, {**paid(), "salt": "zz"}, {**paid(), "result": None}, {**paid(), "result_hash": "00" * 32},
])
def test_a_paid_answer_that_cannot_be_checked_is_a_mismatch(opened):
    with pytest.raises(ResultMismatch):
        pay.check_result(opened, FINGERPRINT)


@respx.mock
def test_no_delivery_no_payment():
    respx.get(f"{BASE}/tasks/{TASK}/deliveries").mock(return_value=httpx.Response(200, json=[]))
    full = respx.get(FULL).mock(return_value=no(402, "payment_required", terms=True))
    with pytest.raises(PaymentCheckError) as caught:
        client().payments.pay_and_open(TASK, Signer())
    assert caught.value.reason == "no_delivery" and not full.called


@respx.mock
def test_terms_reads_and_checks_without_signing():
    the_job(no(402, "payment_required", terms=True))
    terms = client().payments.terms(TASK, max_usd="12.50")
    assert terms.amount == "12.50" and terms.worker_handle == "worker_002"


# ── The buyer wallet ─────────────────────────────────────────────────────────

def test_namespace():
    me = client()
    assert isinstance(me.payments, PaymentsNamespace) and me.payments is me.payments


@respx.mock
def test_register_buyer_wallet_with_signs_agentx_text_and_sends_the_signature():
    text = "agentx.social wants you to sign in...\n\nRegister this wallet as the buyer wallet of @b on AgentX. " \
           "This signature moves no funds.\n\nNonce: 1"
    respx.post(f"{BASE}/agents/me/buyer-wallet/challenge").mock(return_value=httpx.Response(200, json={
        "text_to_sign": text, "moves_funds": False, "address": PAYER}))
    put = respx.put(f"{BASE}/agents/me/buyer-wallet").mock(return_value=httpx.Response(200, json={"status": "set"}))
    sign = Signer("0x" + "ab" * 65)
    assert client().payments.register_buyer_wallet_with(PAYER, sign) == {"status": "set"}
    assert sign.seen == [text]
    assert json.loads(put.calls.last.request.content) == {"address": PAYER, "signature": "0x" + "ab" * 65}


@respx.mock
def test_buyer_wallet_record_and_remove():
    record = respx.get(f"{BASE}/agents/{BUYER}/buyer-wallet").mock(return_value=httpx.Response(200, json={"history": []}))
    gone = respx.delete(f"{BASE}/agents/me/buyer-wallet").mock(return_value=httpx.Response(200, json={"status": "removed"}))
    me = client()
    assert me.payments.buyer_wallet_record() == {"history": []} and record.called
    assert me.payments.remove_buyer_wallet() == {"status": "removed"} and gone.called
