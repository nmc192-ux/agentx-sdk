"""W2-12b3: paid work as MCP tools (payments v1 §3.3, §4.7, §6.2, §6.4).

A language model sits between the owner and the wallet here, so what is
proven is what the server will NOT do: no tool takes or holds a key;
``agentx_pay_check`` signs and sends nothing and hands on a payment only when
the job's record, the worker's wallet history and AgentX's payment request
agree; ``agentx_pay_send`` sends a signature only for a payment that is still
exactly the one AgentX asks for, and says plainly on which side of the
signature every failure fell. The checks themselves are the Python SDK's
(``agentx_sdk.payments``, ``agentx_sdk.pay_cli``) and are tested there.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
import time
from email.utils import formatdate
from pathlib import Path

import httpx
import pytest
import respx
from mcp import Client

from agentx_mcp.api import AgentXAPI
from agentx_mcp.config import load_credentials, save_credentials
from agentx_mcp.server import NO_FUNDS_SENTENCE, build_server

BASE = "http://agentx.test"
BUYER = "did:agentx:buyer-001"
WORKER = "did:agentx:worker-002"
TOKEN = "old.access.SECRET-ACCESS-1"
REFRESH = "old-SECRET-REFRESH-1"
NEW_TOKEN = "new.access.SECRET-ACCESS-2"
NEW_REFRESH = "new-SECRET-REFRESH-2"
SECRETS = (TOKEN, REFRESH, NEW_TOKEN, NEW_REFRESH, "SECRET")
TASK = "9d2c1f6e-0000-4000-8000-00000000abcd"
BOUNTY = "5b1e7a10-0000-4000-8000-00000000b0b0"
SUB = "7c3d9e20-0000-4000-8000-0000000051b1"
PAYER = "0x1111111111111111111111111111111111111111"
PAY_TO = "0x12aB34cD56eF7890aBcDeF1234567890abCD89Ef"
THIEF = "0x9999999999999999999999999999999999999999"
USDC = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
SALT = "ab" * 32
RESULT = "the full result"
FINGERPRINT = hashlib.sha256(bytes.fromhex(SALT) + RESULT.encode()).hexdigest()
SIGNATURE = "0x" + "5a" * 65
FULL = f"/tasks/{TASK}/result/full"
SUB_FULL = f"/markets/bounties/{BOUNTY}/submissions/{SUB}/full"
PAY_TOOLS = {"agentx_pay_check", "agentx_pay_send"}
NEW_TOOLS = PAY_TOOLS | {
    "agentx_deliver_task", "agentx_task_deliveries", "agentx_revise_delivery", "agentx_decline_delivery",
    "agentx_buyer_wallet", "agentx_buyer_wallet_challenge", "agentx_buyer_wallet_register",
    "agentx_remove_buyer_wallet",
}


@pytest.fixture
def server():
    save_credentials(BASE, BUYER, TOKEN, REFRESH)
    return build_server(AgentXAPI(BASE))


async def _call(server, tool: str, args: dict):
    async with Client(server) as client:
        result = await client.call_tool(tool, args)
    dumped = result.model_dump_json()
    for secret in SECRETS:
        assert secret not in dumped, f"{secret!r} leaked into a tool result"
    return result


def _payload(result):
    assert not result.is_error, result.content
    data = result.structured_content
    return data.get("result", data) if isinstance(data, dict) and set(data) == {"result"} else data


def _text(result) -> str:
    assert result.is_error, result.structured_content
    return " ".join(getattr(c, "text", "") for c in result.content)


# ── AgentX's records, as the SDK's own tests build them ──────────────────────

def _terms(amount: str = "12500000", **changes) -> dict:
    return {"buyer_did": BUYER, "worker_did": WORKER, "worker_handle": "worker_002", "pay_to": PAY_TO,
            "payer": PAYER, "network": "eip155:84532", "asset": USDC, "amount": amount,
            "assigned_at": "2026-11-16T10:00:00+00:00", **changes}


def task(agreed: str = "12.50", **terms) -> dict:
    return {"task_id": TASK, "requester_agent_did": BUYER, "executor_agent_did": WORKER,
            "task_type": "summarise", "payload": {"title": "Summarise the Q3 report"}, "status": "delivered",
            "price_usd": "15.00", "agreed_price_usd": agreed, "payment_terms": _terms(**terms)}


def wallet_record(address: str = PAY_TO) -> dict:
    entry = {"address": address, "wallet_kind": "plain", "status": "in_force",
             "set_at": "2026-11-06T10:00:00+00:00", "effective_at": "2026-11-09T10:00:00+00:00",
             "ended_at": None}
    return {"agent_did": WORKER, "payout_wallet": entry, "pending": None, "history": [entry]}


def required(path: str = FULL, amount: str = "12500000", pay_to: str = PAY_TO) -> str:
    message = {
        "x402Version": 2, "error": "PAYMENT-SIGNATURE header is required",
        "resource": {"url": f"https://api.agentx.example{path}", "description": "Full result",
                     "mimeType": "application/json"},
        "accepts": [{"scheme": "exact", "network": "eip155:84532", "amount": amount, "asset": USDC,
                     "payTo": pay_to, "maxTimeoutSeconds": 300, "extra": {"name": "USDC", "version": "2"}}],
    }
    return base64.b64encode(json.dumps(message).encode()).decode()


def no(status: int, error: str, *, terms: str | None = None, retry_after: int | None = None,
       message: str | None = None, date: float | None = None) -> httpx.Response:
    headers = {}
    if terms:
        headers["PAYMENT-REQUIRED"] = terms
    if retry_after:
        headers["Retry-After"] = str(retry_after)
    if date is not None:
        headers["Date"] = formatdate(date, usegmt=True)
    return httpx.Response(status, json={"detail": {"error": error, "message": message or error}}, headers=headers)


def ask(**kw) -> httpx.Response:
    """The plain request to pay for the task."""
    return no(402, "payment_required", terms=required(), **kw)


def paid(result: str = RESULT) -> httpx.Response:
    return httpx.Response(200, json={
        "task_id": TASK, "task_status": "paid", "result": result, "salt": SALT,
        "result_hash": hashlib.sha256(bytes.fromhex(SALT) + result.encode()).hexdigest(),
        "content_type": "text/plain", "result_bytes": len(result),
        "payment": {"transaction": "0x" + "77" * 32, "amount": "12500000"}})


def the_job(mock, *answers, a_task=None, record=None, fingerprint: str = FINGERPRINT):
    """Mock the routes a payment reads; ``answers`` come from result/full in order."""
    mock.get(f"/tasks/{TASK}/deliveries").respond(json=[
        {"delivery_id": "d2", "result_hash": fingerprint}, {"delivery_id": "d1", "result_hash": "00" * 32}])
    mock.get(f"/tasks/{TASK}").respond(json=a_task or task())
    mock.get(url__regex=r".*/agents/[^/]+/payout-wallet$").respond(json=record or wallet_record())
    return mock.get(FULL).mock(side_effect=list(answers))


def signed(route) -> list:
    return [call.request.headers.get("PAYMENT-SIGNATURE") for call in route.calls]


def nothing_was_sent_with_a_signature(mock) -> bool:
    return all("PAYMENT-SIGNATURE" not in call.request.headers and call.request.method == "GET"
               for call in mock.calls)


async def a_payment(server, **more) -> dict:
    """A payment as ``agentx_pay_check`` returns it for the task."""
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        the_job(mock, ask())
        return _payload(await _call(server, "agentx_pay_check", {"task_id": TASK, **more}))["payment"]


# ── No tool takes or holds a key ─────────────────────────────────────────────

async def test_the_tools_are_listed_and_none_takes_a_key(server):
    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert NEW_TOOLS <= set(tools)
    key_words = re.compile(r"private|secret|seed|mnemonic|passphrase|password|(^|_)key($|_)", re.I)
    for name, tool in tools.items():
        for argument in tool.input_schema.get("properties", {}):
            assert not key_words.search(argument), f"{name} takes {argument}"
    assert set(tools["agentx_pay_send"].input_schema["required"]) == {"payment", "signature"}
    assert tools["agentx_pay_send"].annotations.destructive_hint is True
    assert tools["agentx_pay_check"].annotations.read_only_hint is True
    assert "moves funds" in tools["agentx_pay_send"].description.lower()
    assert "signs nothing" in tools["agentx_pay_check"].description.lower()


def test_the_pay_module_makes_no_signature_and_adds_no_check_of_its_own():
    source = (Path(__file__).parents[1] / "src" / "agentx_mcp" / "pay.py").read_text()
    for word in ("eth_account", "sign_typed_data", "private_key", "Account.", "secp256k1", "keccak"):
        assert word not in source
    # The checks, the payment and the re-check at send are the SDK's.
    assert "bridge.payments._ready(job, wallet, max_usd)" in source and "send_checked(bridge, filed, signature" in source


def test_the_package_asks_for_the_sdk_that_has_these_checks():
    project = (Path(__file__).parents[1] / "pyproject.toml").read_text()
    assert '"agentx-social>=0.7.0,<0.8"' in project


# ── agentx_pay_check ─────────────────────────────────────────────────────────

async def test_check_gives_the_sentence_and_the_typed_data_and_sends_no_payment(server):
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        full = the_job(mock, ask())
        got = _payload(await _call(server, "agentx_pay_check",
                                   {"task_id": TASK, "wallet": PAYER, "max_usd": "12.50"}))
        assert nothing_was_sent_with_a_signature(mock) and signed(full) == [None]
        assert all(call.request.headers["Authorization"] == f"Bearer {TOKEN}" for call in mock.calls)
    assert got["status"] == "ready_to_sign" and got["nothing_signed_or_sent"] is True
    assert got["from_wallet"] == PAYER
    payment = got["payment"]
    assert payment["sentence"] == (
        "Pay 12.50 USDC to @worker_002 (0x12aB…89Ef) for task 'Summarise the Q3 report'. "
        "This moves funds. It can be used once, in the next few minutes, and for nothing else.")
    typed = payment["typed_data"]
    assert typed["primaryType"] == "TransferWithAuthorization"
    assert (typed["message"]["from"], typed["message"]["to"], typed["message"]["value"]) == (PAYER, PAY_TO, "12500000")
    assert typed["domain"] == {"name": "USDC", "version": "2", "chainId": 84532, "verifyingContract": USDC}
    assert payment["job"] == {"task_id": TASK} and payment["fingerprint"] == FINGERPRINT
    assert "exactly as written" in got["next"] and "agentx_pay_send" in got["next"]


@pytest.mark.parametrize(("a_task", "record", "more", "reason"), [
    (task(pay_to=THIEF), wallet_record(THIEF), {}, "pay_to_differs"),
    (None, wallet_record(THIEF), {}, "wallet_not_in_history"),
    (task(worker_did="did:agentx:thief-009"), None, {}, "worker_differs"),
    (task(buyer_did="did:agentx:someone-else"), None, {}, "not_your_job"),
    (task(asset=THIEF), None, {}, "unknown_token"),
    (task(network="eip155:1"), None, {}, "unknown_network"),
    (task(agreed="1.00"), None, {}, "amount_differs"),
    (None, None, {"wallet": THIEF}, "wrong_wallet"),
    (None, None, {"max_usd": "1.00"}, "over_your_limit"),
])
async def test_a57_check_hands_on_no_payment_when_the_records_disagree(server, a_task, record, more, reason):
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        the_job(mock, ask(), a_task=a_task, record=record)
        result = await _call(server, "agentx_pay_check", {"task_id": TASK, **more})
        assert nothing_was_sent_with_a_signature(mock)
    said = _text(result)
    assert f"({reason})" in said and "Nothing was signed or sent." in said
    assert result.structured_content is None and "typed_data" not in said


@pytest.mark.parametrize("answer", [
    no(402, "settlement_pending"), no(402, "payment_failed", retry_after=120), no(409, "nothing_to_pay_for"),
    no(404, "payments_off"), no(503, "chain_unavailable"), httpx.Response(302, headers={"Location": "http://x"}),
    no(402, "payment_required"),          # asks to pay and says no terms
])
async def test_only_the_plain_request_to_pay_leads_to_a_payment_to_sign(server, answer):
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        the_job(mock, answer)
        result = await _call(server, "agentx_pay_check", {"task_id": TASK})
        assert nothing_was_sent_with_a_signature(mock)
    assert "Nothing was signed or sent." in _text(result) and result.structured_content is None


async def test_a_pending_payout_wallet_is_said_with_the_payment(server):
    # While it waits, every answer to the token carries the notice (§7.3).
    notice = {"AgentX-Notice": "payout-wallet-pending"}
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.get(f"/tasks/{TASK}/deliveries").respond(json=[{"result_hash": FINGERPRINT}], headers=notice)
        mock.get(f"/tasks/{TASK}").respond(json=task(), headers=notice)
        mock.get(f"/agents/{WORKER}/payout-wallet").respond(json=wallet_record(), headers=notice)
        mock.get(FULL).respond(402, json={"detail": {"error": "payment_required", "message": "pay"}},
                               headers={"PAYMENT-REQUIRED": required(), **notice})
        got = _payload(await _call(server, "agentx_pay_check", {"task_id": TASK}))
    assert "A payout wallet is pending" in got["agentx_notice"] and got["status"] == "ready_to_sign"
    assert "agentx_notice" not in await a_payment(server)


async def test_check_on_a_paid_job_gives_the_result_and_nothing_to_sign(server):
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        the_job(mock, paid())
        got = _payload(await _call(server, "agentx_pay_check", {"task_id": TASK}))
    assert got["status"] == "already_paid" and got["opened"]["result"] == RESULT and "payment" not in got


async def test_check_with_no_delivery_asks_agentx_for_no_payment(server):
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        full = the_job(mock, ask())
        mock.get(f"/tasks/{TASK}/deliveries").respond(json=[])
        result = await _call(server, "agentx_pay_check", {"task_id": TASK})
        assert not full.called
    assert "(no_delivery)" in _text(result)


async def test_a_record_agentx_will_not_show_is_agentx_refusal_in_its_own_words(server):
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        full = the_job(mock, ask())
        mock.get(f"/tasks/{TASK}/deliveries").respond(403, json={"detail": {
            "error": "not_your_job", "message": "Only the job's buyer and worker read its deliveries."}})
        result = await _call(server, "agentx_pay_check", {"task_id": TASK})
        assert not full.called
    assert "AgentX returned 403: Only the job's buyer and worker" in _text(result)
    assert "(error: not_your_job)" in _text(result)


@pytest.mark.parametrize("args", [
    {}, {"bounty_id": BOUNTY}, {"submission_id": SUB},
    {"task_id": TASK, "bounty_id": BOUNTY, "submission_id": SUB},
    {"task_id": "../../agents/me"}, {"bounty_id": BOUNTY, "submission_id": "a/b"},
    {"task_id": TASK, "wallet": "0x12"}, {"task_id": TASK, "max_usd": 12.5},
])
async def test_check_needs_one_job_and_well_formed_arguments(server, args):
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        anything = mock.route().respond(json={})
        result = await _call(server, "agentx_pay_check", args)
        assert result.is_error and not anything.called


async def test_check_for_a_bounty_says_bounty(server):
    shown = {"submission_id": SUB, "bounty_id": BOUNTY, "submitter_did": WORKER, "status": "pending",
             "preview": "Twelve sources.", "result_hash": FINGERPRINT, "result": None,
             "payment_terms": _terms(amount="5000000")}
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.get(f"/markets/bounties/{BOUNTY}/submissions").respond(json=[shown])
        mock.get(f"/markets/bounties/{BOUNTY}").respond(json={
            "bounty_id": BOUNTY, "creator_did": BUYER, "title": "Find the sources", "price_usd": "5.00",
            "pay_network": "eip155:84532", "status": "open"})
        mock.get(f"/agents/{WORKER}/payout-wallet").respond(json=wallet_record())
        mock.get(SUB_FULL).mock(return_value=no(402, "payment_required",
                                                 terms=required(SUB_FULL, amount="5000000")))
        got = _payload(await _call(server, "agentx_pay_check", {"bounty_id": BOUNTY, "submission_id": SUB}))
        assert nothing_was_sent_with_a_signature(mock)
    assert got["payment"]["sentence"].startswith("Pay 5.00 USDC to @worker_002 (0x12aB…89Ef) for bounty "
                                                 "'Find the sources'. This moves funds.")
    assert got["payment"]["job"] == {"bounty_id": BOUNTY, "submission_id": SUB}


async def test_check_without_a_login_asks_agentx_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTX_CREDENTIALS_FILE", str(tmp_path / "none.json"))
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        anything = mock.route().respond(json={})
        result = await _call(build_server(AgentXAPI(BASE)), "agentx_pay_check", {"task_id": TASK})
        assert "agentx_join" in _text(result) and not anything.called


# ── agentx_pay_send ──────────────────────────────────────────────────────────

async def test_send_checks_again_then_sends_the_payment_with_the_signature(server):
    payment = await a_payment(server)
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        full = the_job(mock, ask(), paid())
        got = _payload(await _call(server, "agentx_pay_send", {"payment": payment, "signature": SIGNATURE}))
        first, second = signed(full)
        # The three records were read again before the signature left.
        assert [str(call.request.url.path) for call in mock.calls][-1] == FULL and len(mock.calls) == 5
    assert got["status"] == "paid" and got["opened"]["result"] == RESULT
    assert got["opened"]["payment"]["transaction"] == "0x" + "77" * 32
    assert first is None
    sent = json.loads(base64.b64decode(second))
    assert sent["payload"] == {"signature": SIGNATURE, "authorization": payment["typed_data"]["message"]}
    assert sent["accepted"] == payment["terms"]["accepted"]


async def test_send_reads_a_payment_passed_as_json_text(server):
    payment = await a_payment(server)
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        the_job(mock, ask(), paid())
        got = _payload(await _call(server, "agentx_pay_send",
                                   {"payment": json.dumps(payment), "signature": SIGNATURE}))
    assert got["status"] == "paid"


def _to(address):
    def change(payment):
        payment["typed_data"]["message"]["to"] = address
    return change


def _both(address):
    def change(payment):
        payment["typed_data"]["message"]["to"] = address
        payment["terms"]["pay_to"] = address
    return change


def _set(*path_and_value):
    *path, value = path_and_value

    def change(payment):
        at = payment
        for step in path[:-1]:
            at = at[step]
        at[path[-1]] = value
    return change


@pytest.mark.parametrize("change", [
    _to(THIEF),
    _set("typed_data", "message", "value", "99000000"),
    _set("typed_data", "message", "validBefore", "9999999999"),
    _set("typed_data", "domain", "verifyingContract", THIEF),
    _set("typed_data", "domain", "chainId", 8453),
    _set("typed_data", "primaryType", "Permit"),
    _set("terms", "network", "eip155:1"),
    _set("terms", "amount", "1"),
    _set("job", {"task_id": "../x"}),
    _set("fingerprint", "zz"),
    _set("kind", "something-else"),
    _set("version", 2),
    lambda payment: payment.pop("typed_data"),
    lambda payment: payment.clear(),
])
async def test_a_payment_that_was_altered_is_refused_before_agentx_is_asked(server, change):
    payment = await a_payment(server)
    change(payment)
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        anything = mock.route().respond(json={})
        result = await _call(server, "agentx_pay_send", {"payment": payment, "signature": SIGNATURE})
        assert not anything.called
    said = _text(result)
    assert "(unreadable_file)" in said and "the signature did not leave this server" in said


@pytest.mark.parametrize("payment", ["not json", "[1, 2]", "{}", "x" * 70_000])
async def test_text_that_is_no_payment_is_refused_before_agentx_is_asked(server, payment):
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        anything = mock.route().respond(json={})
        result = await _call(server, "agentx_pay_send", {"payment": payment, "signature": SIGNATURE})
        assert result.is_error and not anything.called


async def test_a_payment_rewritten_to_pay_someone_else_is_refused_at_send(server):
    """The model (or what talked it round) rewrites the whole payment so it is
    consistent in itself. AgentX's records of this moment still say otherwise."""
    payment = await a_payment(server)
    _both(THIEF)(payment)
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        full = the_job(mock, ask())
        result = await _call(server, "agentx_pay_send", {"payment": payment, "signature": SIGNATURE})
        assert signed(full) == [None] and nothing_was_sent_with_a_signature(mock)
    assert "(terms_changed)" in _text(result) and "the signature did not leave this server" in _text(result)


@pytest.mark.parametrize(("now", "reason"), [
    # AgentX's records now pay another wallet, consistently: the terms changed.
    (dict(a_task=task(pay_to=THIEF), record=wallet_record(THIEF),
          answer=no(402, "payment_required", terms=required(pay_to=THIEF))), "terms_changed"),
    # The agreed amount changed, consistently.
    (dict(a_task=task(agreed="13.00", amount="13000000"),
          answer=no(402, "payment_required", terms=required(amount="13000000"))), "terms_changed"),
    # One route alone now names another wallet: the records disagree.
    (dict(answer=no(402, "payment_required", terms=required(pay_to=THIEF))), "pay_to_differs"),
    (dict(record=wallet_record(THIEF)), "wallet_not_in_history"),
    # The worker delivered again: another result than the one checked.
    (dict(fingerprint="cd" * 32), "delivery_changed"),
])
async def test_send_refuses_a_payment_that_no_longer_matches_agentx(server, now, reason):
    payment = await a_payment(server)
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        full = the_job(mock, now.pop("answer", None) or ask(), **now)
        result = await _call(server, "agentx_pay_send", {"payment": payment, "signature": SIGNATURE})
        assert signed(full) == [None] and nothing_was_sent_with_a_signature(mock)
    said = _text(result)
    assert f"({reason})" in said and "the signature did not leave this server" in said
    assert SIGNATURE not in said


async def test_send_refuses_a_payment_past_its_few_minutes(server):
    payment = await a_payment(server)
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        full = the_job(mock, ask(date=time.time() + 290))     # under 30 seconds left by AgentX's clock
        result = await _call(server, "agentx_pay_send", {"payment": payment, "signature": SIGNATURE})
        assert signed(full) == [None]
    assert "(payment_expired)" in _text(result)


async def test_send_refuses_a_payment_whose_few_minutes_were_moved_into_the_future(server):
    """W2-16: the whole window is moved ten years on, so the payment is still
    consistent in itself. `agentx_pay_check` never writes it: its signature
    is not passed on."""
    payment = await a_payment(server)
    message = payment["typed_data"]["message"]
    for bound in ("validAfter", "validBefore"):
        message[bound] = str(int(message[bound]) + 10 * 365 * 86400)
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        full = the_job(mock, ask())
        result = await _call(server, "agentx_pay_send", {"payment": payment, "signature": SIGNATURE})
        assert signed(full) == [None] and nothing_was_sent_with_a_signature(mock)
    said = _text(result)
    assert "(payment_window_wrong)" in said and "the signature did not leave this server" in said


@pytest.mark.parametrize("signature", ["0xabc", "0x", "5a5a", "0xzz", ""])
async def test_a_signature_that_is_not_hex_is_not_sent(server, signature):
    payment = await a_payment(server)
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        anything = mock.route().respond(json={})
        result = await _call(server, "agentx_pay_send", {"payment": payment, "signature": signature})
        assert result.is_error and not anything.called


@pytest.mark.parametrize("answer", [
    no(409, "nothing_to_pay_for"), no(402, "payment_failed", retry_after=120), no(404, "payments_off"),
    no(503, "chain_unavailable"), httpx.Response(302, headers={"Location": "http://elsewhere.test/"}),
])
async def test_send_sends_no_signature_when_the_job_cannot_be_paid_now(server, answer):
    payment = await a_payment(server)
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        full = the_job(mock, answer)
        result = await _call(server, "agentx_pay_send", {"payment": payment, "signature": SIGNATURE})
        assert signed(full) == [None]
    assert "Nothing was signed or sent." in _text(result)


async def test_send_on_a_job_already_paid_gives_the_result_and_keeps_the_signature(server):
    payment = await a_payment(server)
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        full = the_job(mock, paid())
        got = _payload(await _call(server, "agentx_pay_send", {"payment": payment, "signature": SIGNATURE}))
        assert signed(full) == [None]
    assert got["status"] == "paid" and got["opened"]["result"] == RESULT


async def test_a_settling_payment_says_send_the_same_again_and_that_works(server):
    payment = await a_payment(server)
    args = {"payment": payment, "signature": SIGNATURE, "wait_seconds": 0}
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        full = the_job(mock, ask(), no(402, "settlement_pending", retry_after=5))
        first = _payload(await _call(server, "agentx_pay_send", args))
        header = signed(full)[1]
    assert first["status"] == "settling" and "Do NOT sign another" in first["message"]
    assert "same payment and the same signature" in first["message"]
    # Again, exactly as before: AgentX says it is settling, the same payment goes out, and opens.
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        full = the_job(mock, no(402, "settlement_pending"), paid())
        again = _payload(await _call(server, "agentx_pay_send", args))
        assert signed(full) == [None, header]
    assert again["status"] == "paid" and again["opened"]["result"] == RESULT


async def test_a_lost_answer_after_the_signature_went_out_is_not_called_unsent(server):
    payment = await a_payment(server)
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        the_job(mock, ask(), httpx.ConnectError("gone"))
        got = _payload(await _call(server, "agentx_pay_send",
                                   {"payment": payment, "signature": SIGNATURE, "wait_seconds": 0}))
    assert got["status"] == "settling" and "Do NOT sign another" in got["message"]


async def test_no_answer_before_the_signature_went_out_is_said_so(server):
    payment = await a_payment(server)
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        full = the_job(mock, httpx.ConnectError("gone"))
        result = await _call(server, "agentx_pay_send", {"payment": payment, "signature": SIGNATURE})
        assert signed(full) == [None]
    assert "Could not reach AgentX" in _text(result) and "Nothing was sent." in _text(result)


async def test_a_login_that_ends_after_the_signature_went_out_is_not_called_unsent(server):
    payment = await a_payment(server)
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        the_job(mock, ask(), httpx.Response(401, json={"detail": "expired"}))
        mock.post("/auth/token").respond(400, json={"detail": f"refresh token {REFRESH} is spent"})
        result = await _call(server, "agentx_pay_send", {"payment": payment, "signature": SIGNATURE})
    said = _text(result)
    assert "its outcome is not known" in said and "Do NOT sign another" in said
    assert "Nothing was sent" not in said


async def test_a_payment_agentx_refuses_says_it_was_sent_and_how_long_to_wait(server):
    payment = await a_payment(server)
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        the_job(mock, ask(), no(402, "payment_failed", retry_after=240,
                                message=f"could not settle for Bearer {TOKEN}"))
        result = await _call(server, "agentx_pay_send", {"payment": payment, "signature": SIGNATURE})
    said = _text(result)
    assert "was sent and AgentX did not accept it" in said and "payment_failed" in said
    assert "Wait 240 seconds before signing another." in said and "[redacted]" in said
    assert "Nothing was" not in said and SIGNATURE not in said


async def test_a_result_that_is_not_the_one_previewed_is_said_so(server):
    payment = await a_payment(server)
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        the_job(mock, ask(), paid("something else"))
        got = _payload(await _call(server, "agentx_pay_send", {"payment": payment, "signature": SIGNATURE}))
    assert got["status"] == "paid_result_mismatch" and got["opened"]["result"] == "something else"
    assert "The payment went through, but" in got["warning"]


async def test_a_stale_token_is_renewed_and_the_payment_still_checked(server):
    """Pay-to-open takes a token at most 15 minutes old: a 401 renews the pair
    once, and the signed request carries the new token, never the old."""
    payment = await a_payment(server)
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        full = the_job(mock, no(401, "token_not_fresh"), ask(), paid())
        mock.post("/auth/token").respond(json={"access_token": NEW_TOKEN, "refresh_token": NEW_REFRESH,
                                               "agent_did": BUYER})
        got = _payload(await _call(server, "agentx_pay_send", {"payment": payment, "signature": SIGNATURE}))
        with_signature = [call.request for call in full.calls if "PAYMENT-SIGNATURE" in call.request.headers]
    assert got["status"] == "paid" and len(with_signature) == 1
    assert with_signature[0].headers["Authorization"] == f"Bearer {NEW_TOKEN}"
    assert load_credentials(BASE).token == NEW_TOKEN


async def test_what_send_would_send_is_only_what_check_returned(server):
    """Nothing in the payment is secret and nothing in it can redirect the
    money: sent twice through the model unchanged it is the same request."""
    payment = await a_payment(server)
    kept = copy.deepcopy(payment)
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        full = the_job(mock, ask(), paid())
        await _call(server, "agentx_pay_send", {"payment": payment, "signature": SIGNATURE})
        sent = json.loads(base64.b64decode(signed(full)[1]))
    assert payment == kept
    assert sent["payload"]["authorization"]["to"] == PAY_TO
    assert sent["payload"]["authorization"]["from"] == PAYER
    assert sent["payload"]["authorization"]["value"] == "12500000"
    assert "signature" not in json.dumps(kept).lower()


# ── The buyer wallet ─────────────────────────────────────────────────────────

CHALLENGE = {"prompt": "register_buyer_wallet", "signed_by": "the new buyer wallet", "address": PAYER,
             "text_to_sign": f"agentx.social wants you to sign in.\n\nRegister. {NO_FUNDS_SENTENCE}",
             "how_to_sign": "personal_sign", "expires_at": "2026-11-17T10:10:00+00:00", "moves_funds": False}


async def test_buyer_wallet_record_is_public_and_defaults_to_your_own(server):
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        own = mock.get(f"/agents/{BUYER}/buyer-wallet").respond(json={"agent_did": BUYER, "buyer_wallet": None})
        other = mock.get(f"/agents/{WORKER}/buyer-wallet").respond(json={"agent_did": WORKER})
        assert _payload(await _call(server, "agentx_buyer_wallet", {}))["agent_did"] == BUYER
        assert _payload(await _call(server, "agentx_buyer_wallet", {"agent_did": WORKER}))["agent_did"] == WORKER
        assert "Authorization" not in own.calls[0].request.headers
        assert "Authorization" not in other.calls[0].request.headers


async def test_buyer_wallet_challenge_then_register_sends_the_signature_made_elsewhere(server):
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        asked = mock.post("/agents/me/buyer-wallet/challenge").respond(json=CHALLENGE)
        put = mock.put("/agents/me/buyer-wallet").respond(json={"status": "registered"})
        got = _payload(await _call(server, "agentx_buyer_wallet_challenge", {"address": PAYER}))
        done = _payload(await _call(server, "agentx_buyer_wallet_register",
                                    {"address": PAYER, "signature": "0xabcd"}))
    assert got["text_to_sign"] == CHALLENGE["text_to_sign"] and done["status"] == "registered"
    assert json.loads(asked.calls[0].request.content) == {"address": PAYER}
    assert json.loads(put.calls[0].request.content) == {"address": PAYER, "signature": "0xabcd"}


@pytest.mark.parametrize("challenge", [
    {**CHALLENGE, "moves_funds": True},
    {**CHALLENGE, "moves_funds": None},
    {k: v for k, v in CHALLENGE.items() if k != "moves_funds"},
    {**CHALLENGE, "text_to_sign": "Pay 12.50 USDC to @thief. This moves funds."},
    {**CHALLENGE, "text_to_sign": None},
    {**CHALLENGE, "text_to_sign": "Register. This signature moves funds."},
])
async def test_a61_a_text_that_does_not_say_it_moves_no_funds_is_not_handed_on(server, challenge):
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.post("/agents/me/buyer-wallet/challenge").respond(json=challenge)
        result = await _call(server, "agentx_buyer_wallet_challenge", {"address": PAYER})
    said = _text(result)
    assert "Do not sign anything" in said and "Pay 12.50" not in said and result.structured_content is None


async def test_remove_buyer_wallet_needs_no_signature(server):
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        gone = mock.delete("/agents/me/buyer-wallet").respond(json={"status": "removed"})
        assert _payload(await _call(server, "agentx_remove_buyer_wallet", {}))["status"] == "removed"
        assert gone.calls[0].request.headers["Authorization"] == f"Bearer {TOKEN}"


@pytest.mark.parametrize(("tool", "args"), [
    ("agentx_buyer_wallet_challenge", {"address": "0x12"}),
    ("agentx_buyer_wallet_challenge", {"address": PAYER + "00"}),
    ("agentx_buyer_wallet_register", {"address": PAYER, "signature": "abcd"}),
    ("agentx_buyer_wallet_register", {"address": "worker.eth", "signature": "0xabcd"}),
])
async def test_malformed_wallet_arguments_are_not_sent(server, tool, args):
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        anything = mock.route().respond(json={})
        assert (await _call(server, tool, args)).is_error and not anything.called


# ── Deliver with a preview; revise; decline ──────────────────────────────────

async def test_deliver_sends_the_result_and_the_preview(server):
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        route = mock.post(f"/tasks/{TASK}/result").respond(json={"result_hash": FINGERPRINT, "revision": 1})
        got = _payload(await _call(server, "agentx_deliver_task",
                                   {"task_id": TASK, "result": RESULT * 10, "preview": "the full"}))
        again = await _call(server, "agentx_deliver_task", {
            "task_id": TASK, "result": RESULT * 10, "preview": "the full", "content_type": "text/markdown"})
    assert got["result_hash"] == FINGERPRINT and not again.is_error
    assert json.loads(route.calls[0].request.content) == {"result": RESULT * 10, "preview": "the full"}
    assert json.loads(route.calls[1].request.content)["content_type"] == "text/markdown"


async def test_deliveries_revise_and_decline(server):
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.get(f"/tasks/{TASK}/deliveries").respond(json=[{"preview": "the full", "result_hash": FINGERPRINT}])
        revise = mock.post(f"/tasks/{TASK}/revise").respond(json={"revisions_left": 1})
        decline = mock.post(f"/tasks/{TASK}/decline").respond(json={"answer": "declined"})
        listed = _payload(await _call(server, "agentx_task_deliveries", {"task_id": TASK}))
        assert listed[0]["result_hash"] == FINGERPRINT and "result" not in listed[0]
        assert _payload(await _call(server, "agentx_revise_delivery",
                                    {"task_id": TASK, "reason": "No sources."}))["revisions_left"] == 1
        assert _payload(await _call(server, "agentx_decline_delivery",
                                    {"task_id": TASK, "reason": "Not the job."}))["answer"] == "declined"
    assert json.loads(revise.calls[0].request.content) == {"reason": "No sources."}
    assert json.loads(decline.calls[0].request.content) == {"reason": "Not the job."}


@pytest.mark.parametrize(("tool", "args"), [
    ("agentx_deliver_task", {"task_id": TASK, "result": RESULT, "preview": ""}),
    ("agentx_deliver_task", {"task_id": TASK, "result": RESULT, "preview": "p" * 2001}),
    ("agentx_deliver_task", {"task_id": TASK, "result": {"a": 1}, "preview": "p"}),
    ("agentx_deliver_task", {"task_id": "../x", "result": RESULT, "preview": "p"}),
    ("agentx_revise_delivery", {"task_id": TASK}),
    ("agentx_revise_delivery", {"task_id": TASK, "reason": ""}),
    ("agentx_decline_delivery", {"task_id": TASK, "reason": "r" * 1001}),
    ("agentx_task_deliveries", {"task_id": "x/../y"}),
])
async def test_malformed_delivery_arguments_are_not_sent(server, tool, args):
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        anything = mock.route().respond(json={})
        assert (await _call(server, tool, args)).is_error and not anything.called


async def test_a_refusal_carries_agentx_code_and_a_switched_off_server_says_so(server):
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.post(f"/tasks/{TASK}/revise").respond(409, json={"detail": {
            "error": "no_revisions_left", "message": "This job was already sent back twice."}})
        mock.post(f"/tasks/{TASK}/decline").respond(404, json={"detail": "Not Found"})
        revise = await _call(server, "agentx_revise_delivery", {"task_id": TASK, "reason": "again"})
        decline = await _call(server, "agentx_decline_delivery", {"task_id": TASK, "reason": "no"})
    assert "already sent back twice. (error: no_revisions_left)" in _text(revise)
    assert "Tasks are not switched on" in _text(decline)
