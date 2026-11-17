"""W1-8c: the payout wallet, sign out everywhere and the login reset as MCP tools.

The server holds no wallet key and signs nothing: the challenge tool returns the
text, the host's wallet signs it, the submit tool sends the signature. The reset
goes out with no token and the new pair is kept by the server, never shown. An
answer carrying ``AgentX-Notice: payout-wallet-pending`` reaches the host as
``agentx_notice``.
"""
from __future__ import annotations

import json

import pytest
import respx
from mcp import Client

from agentx_mcp.api import PENDING_WARNING, AgentXAPI
from agentx_mcp.config import load_credentials, save_credentials
from agentx_mcp.server import build_server

BASE = "http://agentx.test"
DID = "did:agentx:payee-001"
TOKEN = "old.access.SECRET-ACCESS-1"
REFRESH = "old-SECRET-REFRESH-1"
NEW_TOKEN = "new.access.SECRET-ACCESS-2"
NEW_REFRESH = "new-SECRET-REFRESH-2"
SECRETS = (TOKEN, REFRESH, NEW_TOKEN, NEW_REFRESH, "SECRET")
WALLET = "0x1111111111111111111111111111111111111111"
NEW = "0x2222222222222222222222222222222222222222"
PENDING = {"AgentX-Notice": "payout-wallet-pending"}
RESET_ANSWER = {"access_token": NEW_TOKEN, "refresh_token": NEW_REFRESH, "token_type": "bearer",
                "expires_in": 900, "agent_did": DID}


@pytest.fixture
def server():
    return build_server(AgentXAPI(BASE))


def _joined(token: str = TOKEN) -> None:
    save_credentials(BASE, DID, token, REFRESH)


async def _call(server, tool: str, args: dict):
    async with Client(server) as client:
        result = await client.call_tool(tool, args)
    dumped = result.model_dump_json()
    for secret in SECRETS:
        assert secret not in dumped, f"{secret!r} leaked into a tool result"
    return result


def _payload(result):
    assert not result.is_error, result.content
    return result.structured_content


def _text(result) -> str:
    return " ".join(getattr(c, "text", "") for c in result.content)


def _sent(route) -> dict:
    return json.loads(route.calls.last.request.content)


# ── record and challenges ────────────────────────────────────────────────────

async def test_record_defaults_to_own_profile(server):
    _joined()
    with respx.mock(base_url=BASE) as mock:
        route = mock.get(f"/agents/{DID}/payout-wallet").respond(json={"in_force": None, "history": []})
        out = _payload(await _call(server, "agentx_payout_wallet", {}))
    assert out["history"] == [] and route.called


async def test_record_of_another_profile_needs_no_login(server):
    with respx.mock(base_url=BASE) as mock:
        route = mock.get("/agents/did:agentx:other/payout-wallet").respond(json={})
        _payload(await _call(server, "agentx_payout_wallet", {"agent_did": "did:agentx:other"}))
    assert "authorization" not in route.calls.last.request.headers


@pytest.mark.parametrize(("action", "args", "path", "body"), [
    ("set", {"address": WALLET}, "/agents/me/payout-wallet/challenge", {"address": WALLET}),
    ("change", {"address": NEW}, "/agents/me/payout-wallet/change/challenge", {"new_address": NEW}),
    ("cancel_change", {}, "/agents/me/payout-wallet/change/cancel/challenge", {}),
    ("remove", {}, "/agents/me/payout-wallet/remove/challenge", {}),
])
async def test_challenge_routes(server, action, args, path, body):
    _joined()
    with respx.mock(base_url=BASE) as mock:
        route = mock.post(path).respond(json={"text_to_sign": "sign me", "moves_funds": False})
        out = _payload(await _call(server, "agentx_payout_wallet_challenge", {"action": action, **args}))
    assert out["text_to_sign"] == "sign me"
    assert _sent(route) == body
    assert route.calls.last.request.headers["authorization"] == f"Bearer {TOKEN}"


async def test_challenge_without_address_sends_nothing(server):
    _joined()
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        route = mock.route().respond(json={})
        result = await _call(server, "agentx_payout_wallet_challenge", {"action": "set"})
    assert result.is_error and "needs address" in _text(result)
    assert not route.called


async def test_a_malformed_address_is_refused_before_sending(server):
    _joined()
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        route = mock.route().respond(json={})
        result = await _call(server, "agentx_payout_wallet_challenge",
                             {"action": "set", "address": "0x123"})
    assert result.is_error and not route.called


# ── submit ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(("args", "method", "path", "body"), [
    ({"action": "set", "address": WALLET, "signature": "0xaa"}, "PUT", "/agents/me/payout-wallet",
     {"address": WALLET, "signature": "0xaa"}),
    ({"action": "change", "address": NEW, "current_signature": "0xbb", "new_signature": "0xcc"},
     "POST", "/agents/me/payout-wallet/change",
     {"new_address": NEW, "current_signature": "0xbb", "new_signature": "0xcc"}),
    ({"action": "cancel_change", "signature": "0xdd"}, "POST",
     "/agents/me/payout-wallet/change/cancel", {"signature": "0xdd"}),
    ({"action": "remove", "signature": "0xee"}, "POST", "/agents/me/payout-wallet/remove",
     {"signature": "0xee"}),
])
async def test_submit_routes(server, args, method, path, body):
    _joined()
    with respx.mock(base_url=BASE) as mock:
        route = mock.request(method, path).respond(json={"status": "ok"})
        _payload(await _call(server, "agentx_payout_wallet_submit", args))
    assert _sent(route) == body


@pytest.mark.parametrize("args", [
    {"action": "set", "address": WALLET},
    {"action": "set", "signature": "0xaa"},
    {"action": "change", "address": NEW, "current_signature": "0xbb"},
    {"action": "remove"},
    {"action": "cancel_change", "signature": "not-hex"},
])
async def test_submit_with_missing_or_bad_input_sends_nothing(server, args):
    _joined()
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        route = mock.route().respond(json={})
        result = await _call(server, "agentx_payout_wallet_submit", args)
    assert result.is_error and not route.called


async def test_a_refusal_reaches_the_host(server):
    _joined()
    with respx.mock(base_url=BASE) as mock:
        mock.put("/agents/me/payout-wallet").respond(
            403, json={"detail": {"reason": "bad_signature", "message": "Signature not accepted."}})
        result = await _call(server, "agentx_payout_wallet_submit",
                             {"action": "set", "address": WALLET, "signature": "0xaa"})
    assert result.is_error and "403" in _text(result) and "bad_signature" in _text(result)


async def test_cancel_pending_is_a_delete_with_the_token(server):
    _joined()
    with respx.mock(base_url=BASE) as mock:
        route = mock.delete("/agents/me/payout-wallet/pending").respond(json={"cancelled": True})
        _payload(await _call(server, "agentx_cancel_pending_payout_wallet", {}))
    assert route.calls.last.request.headers["authorization"] == f"Bearer {TOKEN}"


# ── the pending notice ───────────────────────────────────────────────────────

async def test_pending_notice_reaches_the_host_on_any_tool(server):
    _joined()
    with respx.mock(base_url=BASE) as mock:
        mock.post("/heartbeat").respond(json={"next_action": "post"}, headers=PENDING)
        out = _payload(await _call(server, "agentx_heartbeat", {}))
    assert out["agentx_notice"] == PENDING_WARNING
    assert "agentx_cancel_pending_payout_wallet" in out["agentx_notice"]


async def test_pending_notice_on_a_refusal_is_in_the_error(server):
    _joined()
    with respx.mock(base_url=BASE) as mock:
        mock.post("/auth/logout-all").respond(409, json={"detail": "no"}, headers=PENDING)
        result = await _call(server, "agentx_logout_all", {})
    assert result.is_error and "payout wallet is pending" in _text(result)


async def test_no_notice_no_extra_field(server):
    _joined()
    with respx.mock(base_url=BASE) as mock:
        mock.post("/heartbeat").respond(json={"next_action": "post"})
        out = _payload(await _call(server, "agentx_heartbeat", {}))
    assert "agentx_notice" not in out


# ── sign out everywhere and the login reset ──────────────────────────────────

async def test_logout_all_posts_with_the_token(server):
    _joined()
    with respx.mock(base_url=BASE) as mock:
        route = mock.post("/auth/logout-all").respond(json={"signed_out": True, "agent_did": DID})
        assert _payload(await _call(server, "agentx_logout_all", {}))["signed_out"] is True
    assert route.calls.last.request.headers["authorization"] == f"Bearer {TOKEN}"


async def test_reset_challenge_sends_no_token(server):
    _joined()
    with respx.mock(base_url=BASE) as mock:
        route = mock.post("/auth/wallet-reset/challenge").respond(json={"text_to_sign": "reset me"})
        out = _payload(await _call(server, "agentx_login_reset_challenge", {}))
    assert out["text_to_sign"] == "reset me"
    assert _sent(route) == {"agent": DID}
    assert "authorization" not in route.calls.last.request.headers


async def test_reset_challenge_with_nothing_configured_needs_a_did(server):
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        route = mock.route().respond(json={})
        result = await _call(server, "agentx_login_reset_challenge", {})
    assert result.is_error and "agent_did" in _text(result) and not route.called


async def test_reset_sends_no_token_and_saves_the_new_pair_unshown(server):
    _joined()
    with respx.mock(base_url=BASE) as mock:
        reset = mock.post("/auth/wallet-reset").respond(json=RESET_ANSWER)
        out = _payload(await _call(server, "agentx_login_reset", {"signature": "0xabcd"}))
        assert out["signed_in"] is True and out["agent_did"] == DID
        assert "saved to" in out["credentials"]
        assert _sent(reset) == {"agent": DID, "signature": "0xabcd"}
        assert "authorization" not in reset.calls.last.request.headers
        creds = load_credentials(BASE)
        assert (creds.token, creds.refresh_token) == (NEW_TOKEN, NEW_REFRESH)
        whoami = mock.get("/agents").respond(json={"agents": [{"agent_did": DID}]})
        _payload(await _call(server, "agentx_whoami", {}))
    assert whoami.called


async def test_reset_with_no_credentials_saves_a_new_entry(server):
    with respx.mock(base_url=BASE) as mock:
        mock.post("/auth/wallet-reset").respond(json=RESET_ANSWER)
        _payload(await _call(server, "agentx_login_reset", {"signature": "0xabcd", "agent_did": DID}))
    assert load_credentials(BASE).token == NEW_TOKEN


async def test_reset_of_another_profile_is_refused_and_keeps_the_pair(server):
    _joined()
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        route = mock.route().respond(json=RESET_ANSWER)
        result = await _call(server, "agentx_login_reset",
                             {"signature": "0xabcd", "agent_did": "did:agentx:other"})
    assert result.is_error and not route.called
    assert load_credentials(BASE).token == TOKEN


async def test_refused_reset_keeps_the_old_pair_and_tries_no_refresh(server):
    _joined()
    with respx.mock(base_url=BASE) as mock:
        reset = mock.post("/auth/wallet-reset").respond(401, json={"detail": "no"})
        result = await _call(server, "agentx_login_reset", {"signature": "0xabcd"})
    assert result.is_error and reset.call_count == 1
    assert load_credentials(BASE).token == TOKEN


async def test_reset_answer_without_tokens_is_an_error_and_keeps_the_pair(server):
    _joined()
    with respx.mock(base_url=BASE) as mock:
        mock.post("/auth/wallet-reset").respond(json={"agent_did": DID})
        result = await _call(server, "agentx_login_reset", {"signature": "0xabcd"})
    assert result.is_error and "unexpected body" in _text(result)
    assert load_credentials(BASE).token == TOKEN


async def test_reset_with_an_env_token_holds_the_pair_in_memory(server, monkeypatch):
    monkeypatch.setenv("AGENTX_TOKEN", TOKEN)
    monkeypatch.setenv("AGENTX_AGENT_DID", DID)
    with respx.mock(base_url=BASE) as mock:
        mock.post("/auth/wallet-reset").respond(json=RESET_ANSWER)
        out = _payload(await _call(server, "agentx_login_reset", {"signature": "0xabcd"}))
        assert "held by this server" in out["credentials"]
        route = mock.get(f"/agents/{DID}/payout-wallet").respond(json={})
        _payload(await _call(server, "agentx_payout_wallet", {}))
    assert route.calls.last.request.headers["authorization"] == f"Bearer {NEW_TOKEN}"


async def test_reset_answer_with_the_pending_notice_passes_it_on(server):
    _joined()
    with respx.mock(base_url=BASE) as mock:
        mock.post("/auth/wallet-reset").respond(json=RESET_ANSWER, headers=PENDING)
        out = _payload(await _call(server, "agentx_login_reset", {"signature": "0xabcd"}))
    assert out["agentx_notice"] == PENDING_WARNING
