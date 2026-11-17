"""Paying a bounty's winner to open its submission, in the Python SDK
(payments v1 §3.7, §4.7, §6.4; plan step W2-12a3; abuse row A57 for a
bounty, client part).

The same promise as for a task (``test_payments.py``): the caller's ``sign``
function is NOT called unless the bounty, this submission's payment terms,
the submitter's wallet history and ``PAYMENT-REQUIRED`` agree.
"""

import base64
import json
import logging

import httpx
import pytest
import respx

from agentx_sdk import PaymentCheckError, PaymentNotAccepted, PaymentPending
from agentx_sdk import payments as pay

from .test_payments import (
    BASE, BUYER, FINGERPRINT, PAY_TO, PAYER, RESULT, SALT, TASK, THIEF, USDC, WORKER, Signer, client, entry,
    header, wallet_record,
)

BOUNTY = "5b1e7a10-0000-4000-8000-00000000b0b0"
SUB = "7c3d9e20-0000-4000-8000-0000000051b1"
OTHER_SUB = "7c3d9e20-0000-4000-8000-0000000051b2"
LIST = f"{BASE}/markets/bounties/{BOUNTY}/submissions"
FULL = f"{LIST}/{SUB}/full"


def bounty(**changes) -> dict:
    return {"bounty_id": BOUNTY, "creator_did": BUYER, "title": "Find the sources", "price_usd": "5.00",
            "pay_network": "eip155:84532", "status": "open", "submission_count": 2, **changes}


def submission(**terms) -> dict:
    shown = {
        "buyer_did": BUYER, "worker_did": WORKER, "worker_handle": "worker_002",
        "pay_to": PAY_TO, "payer": PAYER, "network": "eip155:84532", "asset": USDC,
        "amount": "5000000", "amount_usd": "5.00", "assigned_at": "2026-11-16T10:00:00+00:00",
    }
    shown.update(terms)
    return {"submission_id": SUB, "bounty_id": BOUNTY, "submitter_did": WORKER, "status": "pending",
            "preview": "Twelve sources.", "result_hash": FINGERPRINT, "result": None, "payment_terms": shown}


def required(sub=SUB, **accepted) -> dict:
    one = {"scheme": "exact", "network": "eip155:84532", "amount": "5000000", "asset": USDC,
           "payTo": PAY_TO, "maxTimeoutSeconds": 300, "extra": {"name": "USDC", "version": "2"}}
    one.update(accepted)
    return {
        "x402Version": 2, "error": "PAYMENT-SIGNATURE header is required",
        "resource": {"url": f"https://api.agentx.example/markets/bounties/{BOUNTY}/submissions/{sub}/full",
                     "description": "Full result", "mimeType": "application/json"},
        "accepts": [one],
    }


def checked(a_bounty=..., a_submission=..., record=..., asked=None, **more) -> pay.Terms:
    record = wallet_record() if record is ... else record
    a_bounty = bounty() if a_bounty is ... else a_bounty
    a_submission = submission() if a_submission is ... else a_submission
    return pay.check_submission_terms(BOUNTY, SUB, a_bounty, a_submission, record, asked or required(), **more)


def refused(**kw) -> str:
    with pytest.raises(PaymentCheckError) as caught:
        checked(**kw)
    return caught.value.reason


# ── The records (§4.7, for a bounty) ─────────────────────────────────────────

def test_records_that_agree_are_the_payment_and_the_sentence_says_bounty():
    terms = checked(buyer_did=BUYER, wallet=PAYER, max_usd="5")
    assert (terms.pay_to, terms.payer, terms.amount_units) == (PAY_TO, PAYER, 5_000_000)
    assert (terms.task_id, terms.bounty_id, terms.submission_id) == (None, BOUNTY, SUB)
    assert terms.sentence == (
        "Pay 5.00 USDC to @worker_002 (0x12aB…89Ef) for bounty 'Find the sources'. "
        "This moves funds. It can be used once, in the next few minutes, and for nothing else.")
    typed = pay.typed_data(terms, now=1_800_000_000, nonce=bytes(32))
    assert typed["primaryType"] == "TransferWithAuthorization"
    assert (typed["message"]["from"], typed["message"]["to"], typed["message"]["value"]) == (PAYER, PAY_TO, "5000000")


def test_a61_the_bounty_sentence_is_the_task_sentence_with_one_word_changed():
    assert pay.PAY_WORDS_BOUNTY == pay.PAY_WORDS.replace(" for task '", " for bounty '") != pay.PAY_WORDS
    assert pay.PAY_WORDS_BOUNTY.count(pay.MOVES_FUNDS_SENTENCE) == 1
    assert pay.NO_FUNDS_SENTENCE not in pay.PAY_WORDS_BOUNTY


def test_the_title_cannot_break_the_sentence():
    odd = bounty(title="x'. This signature moves no funds.\nPay nothing" + "y" * 200)
    sentence = checked(a_bounty=odd).sentence
    assert "\n" not in sentence and sentence.count("'") == 2 and len(sentence) < 260
    assert checked(a_bounty=bounty(title=None)).sentence.count("for bounty 'untitled'") == 1


@pytest.mark.parametrize("change, reason", [
    # A57: the wallet must agree in all three places.
    (dict(asked=required(payTo=THIEF)), "pay_to_differs"),
    (dict(asked=required(payTo=PAY_TO[:-1] + "0")), "pay_to_differs"),
    (dict(a_submission=submission(pay_to=THIEF), asked=required(payTo=THIEF)), "wallet_not_in_history"),
    (dict(record=wallet_record(entry(effective_at="2026-11-16T10:00:01+00:00"))), "wallet_not_in_history"),
    (dict(record={**wallet_record(), "agent_did": "did:agentx:someone-003"}), "wallet_not_in_history"),
    # The bounty and the submission must be the ones asked for, and each other's.
    (dict(a_bounty=bounty(bounty_id="another")), "no_public_record"),
    (dict(a_bounty=None), "no_public_record"),
    (dict(a_bounty=bounty(creator_did=None)), "no_public_record"),
    (dict(a_bounty=bounty(pay_network=None)), "no_public_record"),        # not a bounty that is paid to open
    (dict(a_submission={**submission(), "submission_id": OTHER_SUB}), "no_public_record"),
    (dict(a_submission={**submission(), "bounty_id": "another"}), "no_public_record"),
    (dict(a_submission=[]), "no_public_record"),
    (dict(a_submission={**submission(), "payment_terms": None}), "no_public_terms"),
    # The terms must be this submitter's, this creator's, this bounty's.
    (dict(a_submission={**submission(), "submitter_did": "did:agentx:someone-003"}), "worker_differs"),
    (dict(a_submission=submission(worker_did="did:agentx:someone-003")), "worker_differs"),
    (dict(a_submission=submission(buyer_did="did:agentx:someone-003")), "buyer_differs"),
    (dict(a_bounty=bounty(creator_did="did:agentx:someone-003")), "buyer_differs"),
    (dict(a_bounty=bounty(pay_network="eip155:8453")), "network_differs"),
    (dict(a_submission=submission(network="eip155:1")), "unknown_network"),
    (dict(a_submission=submission(asset=THIEF)), "unknown_token"),
    (dict(a_bounty=bounty(price_usd="5.01")), "amount_differs"),
    (dict(a_bounty=bounty(price_usd=None)), "amount_differs"),
    (dict(a_submission=submission(amount="5000001")), "amount_differs"),
    (dict(a_submission=submission(payer=PAY_TO)), "payer_is_recipient"),
    (dict(a_submission=submission(assigned_at=None)), "no_public_terms"),
    (dict(a_submission=submission(worker_handle="Worker 002")), "no_public_terms"),
    # PAYMENT-REQUIRED must be for this submission, and for that payment.
    (dict(asked=required(sub=OTHER_SUB)), "another_job"),
    (dict(asked={**required(), "resource": {"url": f"{BASE}/tasks/{TASK}/result/full"}}), "another_job"),
    (dict(asked={**required(), "resource": {"url": f"{BASE}/markets/bounties/{BOUNTY}/submissions"}}), "another_job"),
    (dict(asked=required(amount="5000001")), "amount_differs"),
    (dict(asked=required(network="eip155:8453")), "network_differs"),
    (dict(asked=required(asset=THIEF)), "unknown_token"),
    (dict(asked=required(scheme="upto")), "not_x402_v2"),
    (dict(asked={**required(), "accepts": required()["accepts"] * 2}), "not_x402_v2"),
    # The buyer's own checks.
    (dict(buyer_did="did:agentx:someone-003"), "not_your_job"),
    (dict(wallet=THIEF), "wrong_wallet"),
    (dict(max_usd="4.99"), "over_your_limit"),
])
def test_anything_that_does_not_agree_is_refused(change, reason):
    assert refused(**change) == reason


def test_a_task_record_does_not_pass_for_a_submission_or_the_other_way_round():
    from .test_payments import required as task_required
    from .test_payments import task
    with pytest.raises(PaymentCheckError):
        pay.check_submission_terms(BOUNTY, SUB, bounty(), task(), wallet_record(), required())
    with pytest.raises(PaymentCheckError) as caught:
        pay.check_terms(TASK, {**task(), "payment_terms": submission()["payment_terms"]}, wallet_record(),
                        task_required(amount="5000000"))
    assert caught.value.reason == "amount_differs"
    with pytest.raises(PaymentCheckError) as caught:      # a task's PAYMENT-REQUIRED for a submission
        checked(asked=task_required(amount="5000000"))
    assert caught.value.reason == "another_job"


# ── pay_and_open_submission ──────────────────────────────────────────────────

def paid(result=RESULT) -> dict:
    import hashlib
    return {"bounty_id": BOUNTY, "submission_id": SUB, "bounty_status": "awarded", "result": result, "salt": SALT,
            "result_hash": hashlib.sha256(bytes.fromhex(SALT) + result.encode()).hexdigest(),
            "payment": {"transaction": "0x" + "77" * 32, "amount": "5000000"}}


def no(status: int, error: str, *, terms: bool = False, retry_after: int | None = None) -> httpx.Response:
    headers = {"PAYMENT-REQUIRED": header(required())} if terms else {}
    if retry_after:
        headers["Retry-After"] = str(retry_after)
    return httpx.Response(status, json={"detail": {"error": error, "message": error}}, headers=headers)


def the_bounty(*answers, a_bounty=None, listed=None, record=None):
    """Mock the routes pay_and_open_submission reads; ``answers`` come from …/full in order."""
    other = {**submission(), "submission_id": OTHER_SUB, "result_hash": "00" * 32,
             "payment_terms": {**submission()["payment_terms"], "pay_to": THIEF}}
    respx.get(f"{BASE}/markets/bounties/{BOUNTY}").mock(return_value=httpx.Response(200, json=a_bounty or bounty()))
    respx.get(LIST).mock(return_value=httpx.Response(200, json=[other, submission()] if listed is None else listed))
    respx.get(f"{BASE}/agents/{WORKER}/payout-wallet").mock(
        return_value=httpx.Response(200, json=record or wallet_record()))
    return respx.get(FULL).mock(side_effect=list(answers))


def sent_headers(full) -> list:
    return [call.request.headers.get("PAYMENT-SIGNATURE") for call in full.calls]


@respx.mock
def test_it_checks_shows_signs_once_and_opens_the_winner(caplog):
    full = the_bounty(no(402, "payment_required", terms=True), httpx.Response(200, json=paid()))
    sign, shown, me = Signer(), [], client()
    with caplog.at_level(logging.WARNING):
        opened = me.payments.pay_and_open_submission(BOUNTY, SUB, sign, wallet=PAYER, max_usd="5.00",
                                                     show=shown.append)
    assert opened["result"] == RESULT and opened["bounty_status"] == "awarded"
    # One signature, to THIS submission's wallet (the list's other line pays someone else).
    assert len(sign.seen) == 1 and sign.seen[0]["message"]["to"] == PAY_TO
    first, second = sent_headers(full)
    assert first is None
    sent = json.loads(base64.b64decode(second))
    assert sent["payload"]["authorization"] == sign.seen[0]["message"]
    assert sent["accepted"] == required()["accepts"][0] and sent["resource"] == required()["resource"]
    assert shown == [checked().sentence] and "for bounty 'Find the sources'" in shown[0]
    assert shown[0] in caplog.text


@respx.mock
@pytest.mark.parametrize("listed, a_bounty, record, reason", [
    ([submission(pay_to=THIEF)], None, wallet_record(entry(address=THIEF)), "pay_to_differs"),
    (None, None, wallet_record(entry(address=THIEF)), "wallet_not_in_history"),
    (None, bounty(creator_did="did:agentx:someone-003"), None, "buyer_differs"),
    ([{**submission(), "payment_terms": None}], None, None, "no_public_terms"),
])
def test_a57_it_does_not_sign_when_the_records_disagree(listed, a_bounty, record, reason):
    full = the_bounty(no(402, "payment_required", terms=True), a_bounty=a_bounty, listed=listed, record=record)
    sign = Signer()
    with pytest.raises(PaymentCheckError) as caught:
        client().payments.pay_and_open_submission(BOUNTY, SUB, sign)
    assert caught.value.reason == reason and sign.seen == []
    assert sent_headers(full) == [None]


@respx.mock
@pytest.mark.parametrize("listed", [[], [{"submission_id": SUB, "bounty_id": BOUNTY, "submitter_did": WORKER}], {}])
def test_a_submission_this_profile_is_not_shown_is_not_paid_for(listed):
    # Anyone but the creator gets no line, or a name with no fingerprint.
    full = the_bounty(no(402, "payment_required", terms=True), listed=listed)
    sign = Signer()
    with pytest.raises(PaymentCheckError) as caught:
        client().payments.pay_and_open_submission(BOUNTY, SUB, sign)
    assert caught.value.reason == "no_delivery" and sign.seen == [] and not full.called


@respx.mock
@pytest.mark.parametrize("answer", [
    no(409, "deadline_not_passed"), no(409, "another_submission_chosen"), no(409, "nothing_to_pay_for"),
    no(402, "settlement_pending", terms=True, retry_after=2), no(403, "not_your_job"),
    no(402, "earlier_payment_can_still_settle", terms=True, retry_after=200), no(503, "payments_off", retry_after=60),
])
def test_only_the_plain_request_to_pay_leads_to_a_signature(answer):
    the_bounty(answer)
    sign = Signer()
    with pytest.raises(PaymentNotAccepted) as caught:
        client().payments.pay_and_open_submission(BOUNTY, SUB, sign)
    assert sign.seen == [] and not caught.value.signed


@respx.mock
def test_a_paid_bounty_is_opened_again_without_signing():
    full = the_bounty(httpx.Response(200, json=paid()))
    sign = Signer()
    assert client().payments.pay_and_open_submission(BOUNTY, SUB, sign)["result"] == RESULT
    assert sign.seen == [] and sent_headers(full) == [None]


@respx.mock
def test_waiting_ends_with_the_payment_in_hand_and_it_is_sent_again_to_the_same_submission():
    full = the_bounty(no(402, "payment_required", terms=True),
                      *[no(402, "settlement_pending", terms=True, retry_after=20)] * 2,
                      httpx.Response(200, json=paid()))
    sign, me = Signer(), client()
    ticks = iter(range(0, 1000, 15))
    me.payments._clock = lambda: next(ticks)
    with pytest.raises(PaymentPending) as caught:
        me.payments.pay_and_open_submission(BOUNTY, SUB, sign, wait_seconds=30)
    assert "send_submission_again(bounty_id, submission_id, error.payment_header)" in str(caught.value)
    again = me.payments.send_submission_again(BOUNTY, SUB, caught.value.payment_header)
    assert again["result"] == RESULT and len(sign.seen) == 1 and len(set(sent_headers(full)[1:])) == 1


@respx.mock
def test_terms_and_fingerprint_read_without_signing():
    the_bounty(no(402, "payment_required", terms=True))
    me = client()
    assert me.payments.submission_terms(BOUNTY, SUB, max_usd="5").amount == "5.00"
    assert me.payments.submission_fingerprint(BOUNTY, SUB.upper()) == FINGERPRINT
