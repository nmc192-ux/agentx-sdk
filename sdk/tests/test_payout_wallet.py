"""The payout wallet and the login reset in the Python SDK (payments v1 §7.3 to
§7.8, plan step W1-8a).

The SDK signs nothing: every signature comes from the caller's ``sign``
function, over the exact text the server wrote. Covered here: each route's
method, path and body; the ``*_with`` helpers; the pending-wallet warning; the
reset sent without a token and its new pair held and saved.
"""

import json
import warnings

import httpx
import pytest
import respx

from agentx_sdk import AgentXClient, PayoutWalletNamespace, PayoutWalletPendingWarning
from agentx_sdk.auth import AgentIdentity
from agentx_sdk.exceptions import AgentXError

BASE = "http://test.agentx.local"
ME = f"{BASE}/agents/me/payout-wallet"
DID = "did:agentx:payee-001"
WALLET = "0x1111111111111111111111111111111111111111"
NEW = "0x2222222222222222222222222222222222222222"
PENDING = {"AgentX-Notice": "payout-wallet-pending"}


def _client(**kw) -> AgentXClient:
    client = AgentXClient(api_key="tok", base_url=BASE, max_retries=0, **kw)
    if client.identity is None:
        client.identity = AgentIdentity(agent_did=DID, api_key="tok")
    return client


def _challenge(text: str, address: str = WALLET) -> dict:
    return {
        "prompt": "x", "signed_by": "x", "address": address, "text_to_sign": text,
        "how_to_sign": "personal_sign (EIP-191) over text_to_sign, exactly as given",
        "expires_at": "2026-11-14T00:10:00+00:00", "moves_funds": False,
    }


def _body(route) -> dict:
    return json.loads(route.calls.last.request.content)


class Signer:
    """Records what it was asked to sign; returns a fixed signature."""

    def __init__(self, signature="0xsig"):
        self.signature = signature
        self.texts: list[str] = []

    def __call__(self, text: str):
        self.texts.append(text)
        return self.signature


def test_namespace_type():
    assert isinstance(_client().payout_wallet, PayoutWalletNamespace)


# ── Each route ────────────────────────────────────────────────────────────────

@respx.mock
def test_record_is_public_and_defaults_to_own_did():
    route = respx.get(f"{BASE}/agents/{DID}/payout-wallet").mock(
        return_value=httpx.Response(200, json={"in_force": None, "pending": None, "history": []}))
    assert _client().payout_wallet.record()["history"] == []
    other = respx.get(f"{BASE}/agents/did:agentx:other/payout-wallet").mock(
        return_value=httpx.Response(200, json={}))
    _client().payout_wallet.record("did:agentx:other")
    assert route.called and other.called


def test_record_refuses_a_did_that_would_pick_another_route():
    with pytest.raises(ValueError):
        _client().payout_wallet.record("did:agentx:x/../me")


@respx.mock
def test_set_with_signs_the_server_text_exactly_and_sends_address_and_signature():
    text = "api.agentx.social wants you to sign in with your Ethereum account:\n" + WALLET
    challenge = respx.post(f"{ME}/challenge").mock(return_value=httpx.Response(200, json=_challenge(text)))
    put = respx.put(ME).mock(return_value=httpx.Response(200, json={"status": "pending"}))
    sign = Signer("0xabc")

    assert _client().payout_wallet.set_with(WALLET, sign) == {"status": "pending"}
    assert _body(challenge) == {"address": WALLET}
    assert sign.texts == [text]
    assert _body(put) == {"address": WALLET, "signature": "0xabc"}
    assert put.calls.last.request.headers["Authorization"] == "Bearer tok"


@respx.mock
def test_bytes_signature_becomes_hex():
    respx.post(f"{ME}/challenge").mock(return_value=httpx.Response(200, json=_challenge("t")))
    put = respx.put(ME).mock(return_value=httpx.Response(200, json={}))
    _client().payout_wallet.set_with(WALLET, lambda _t: b"\x01\xff")
    assert _body(put)["signature"] == "0x01ff"


@respx.mock
def test_no_text_means_nothing_is_signed_or_sent():
    respx.post(f"{ME}/challenge").mock(return_value=httpx.Response(200, json={"address": WALLET}))
    put = respx.put(ME).mock(return_value=httpx.Response(200, json={}))
    sign = Signer()
    with pytest.raises(ValueError, match="no text to sign"):
        _client().payout_wallet.set_with(WALLET, sign)
    assert sign.texts == [] and not put.called


@respx.mock
def test_empty_signature_is_refused_before_sending():
    respx.post(f"{ME}/challenge").mock(return_value=httpx.Response(200, json=_challenge("t")))
    put = respx.put(ME).mock(return_value=httpx.Response(200, json={}))
    with pytest.raises(ValueError, match="sign\\(\\) must return"):
        _client().payout_wallet.set_with(WALLET, lambda _t: "")
    assert not put.called


@respx.mock
def test_cancel_pending_is_a_delete_with_the_token():
    route = respx.delete(f"{ME}/pending").mock(return_value=httpx.Response(200, json={"status": "cancelled"}))
    assert _client().payout_wallet.cancel_pending()["status"] == "cancelled"
    assert route.calls.last.request.headers["Authorization"] == "Bearer tok"


@respx.mock
def test_change_with_has_each_wallet_sign_its_own_text():
    respx.post(f"{ME}/change/challenge").mock(return_value=httpx.Response(200, json={
        "current_wallet": _challenge("by current"), "new_wallet": _challenge("by new", NEW)}))
    change = respx.post(f"{ME}/change").mock(return_value=httpx.Response(200, json={"status": "pending"}))
    current, new = Signer("0xc"), Signer("0xn")

    _client().payout_wallet.change_with(NEW, current, new)
    assert current.texts == ["by current"] and new.texts == ["by new"]
    assert _body(change) == {"new_address": NEW, "current_signature": "0xc", "new_signature": "0xn"}


@respx.mock
def test_cancel_change_and_remove_send_only_the_signature():
    respx.post(f"{ME}/change/cancel/challenge").mock(return_value=httpx.Response(200, json=_challenge("c")))
    cancel = respx.post(f"{ME}/change/cancel").mock(return_value=httpx.Response(200, json={}))
    respx.post(f"{ME}/remove/challenge").mock(return_value=httpx.Response(200, json=_challenge("r")))
    remove = respx.post(f"{ME}/remove").mock(return_value=httpx.Response(200, json={}))
    sign = Signer("0xs")

    _client().payout_wallet.cancel_change_with(sign)
    _client().payout_wallet.remove_with(sign)
    assert sign.texts == ["c", "r"]
    assert _body(cancel) == {"signature": "0xs"} and _body(remove) == {"signature": "0xs"}


@respx.mock
def test_refusal_reaches_the_caller_with_the_server_detail():
    respx.put(ME).mock(return_value=httpx.Response(
        403, json={"detail": {"error": "signature_refused", "reason": "wrong_signer"}}))
    with pytest.raises(AgentXError) as caught:
        _client().payout_wallet.set(WALLET, "0xbad")
    assert caught.value.status_code == 403
    assert caught.value.detail["reason"] == "wrong_signer"


@respx.mock
def test_token_not_fresh_renews_and_sends_once_more():
    put = respx.put(ME).mock(side_effect=[
        httpx.Response(401, json={"detail": {"error": "token_not_fresh"}}),
        httpx.Response(200, json={"status": "pending"}),
    ])
    refresh = respx.post(f"{BASE}/auth/token").mock(return_value=httpx.Response(200, json={
        "access_token": "fresh", "refresh_token": "r2", "token_type": "bearer", "expires_in": 3600}))
    client = AgentXClient(api_key="old", refresh_token="r1", base_url=BASE, max_retries=0)

    assert client.payout_wallet.set(WALLET, "0xs") == {"status": "pending"}
    assert refresh.call_count == 1 and put.call_count == 2
    assert put.calls.last.request.headers["Authorization"] == "Bearer fresh"


# ── The pending-wallet notice ─────────────────────────────────────────────────

@respx.mock
def test_pending_notice_warns_once_while_it_lasts():
    respx.get(f"{BASE}/notifications").mock(return_value=httpx.Response(200, json=[], headers=PENDING))
    client = _client()
    with pytest.warns(PayoutWalletPendingWarning, match="cancel_pending"):
        client._get("/notifications")
    assert client.payout_wallet_pending
    with warnings.catch_warnings():
        warnings.simplefilter("error", PayoutWalletPendingWarning)
        client._get("/notifications")  # still pending: no second warning


@respx.mock
def test_pending_notice_warns_again_after_it_went_away():
    respx.get(f"{BASE}/feed").mock(side_effect=[
        httpx.Response(200, json=[], headers=PENDING),
        httpx.Response(200, json=[]),
        httpx.Response(200, json=[], headers=PENDING),
    ])
    client = _client()
    with pytest.warns(PayoutWalletPendingWarning):
        client._get("/feed")
    client._get("/feed")
    assert not client.payout_wallet_pending
    with pytest.warns(PayoutWalletPendingWarning):
        client._get("/feed")


@respx.mock
def test_pending_notice_on_an_error_answer_still_warns():
    respx.put(ME).mock(return_value=httpx.Response(409, json={"detail": "x"}, headers=PENDING))
    with pytest.warns(PayoutWalletPendingWarning), pytest.raises(AgentXError):
        _client().payout_wallet.set(WALLET, "0xs")


@respx.mock
def test_no_notice_no_warning():
    respx.get(f"{BASE}/feed").mock(return_value=httpx.Response(200, json=[],
                                                              headers={"AgentX-Notice": "something-else"}))
    with warnings.catch_warnings():
        warnings.simplefilter("error", PayoutWalletPendingWarning)
        client = _client()
        client._get("/feed")
    assert not client.payout_wallet_pending


# ── Sign out everywhere and the login reset ───────────────────────────────────

@respx.mock
def test_logout_all_posts_with_the_token():
    route = respx.post(f"{BASE}/auth/logout-all").mock(
        return_value=httpx.Response(200, json={"signed_out": True}))
    assert _client().logout_all()["signed_out"] is True
    assert route.calls.last.request.headers["Authorization"] == "Bearer tok"


@respx.mock
def test_logout_all_without_a_settled_wallet_is_a_409():
    respx.post(f"{BASE}/auth/logout-all").mock(return_value=httpx.Response(
        409, json={"detail": {"error": "no_recovery_wallet", "message": "Nothing was changed."}}))
    with pytest.raises(AgentXError) as caught:
        _client().logout_all()
    assert caught.value.status_code == 409


RESET_PAIR = {
    "access_token": "new-access", "refresh_token": "new-refresh", "token_type": "bearer",
    "expires_in": 900, "agent_did": DID, "tokens_valid_after": "2026-11-14T00:10:00+00:00",
}


@respx.mock
def test_reset_sends_no_token_and_holds_the_new_pair(tmp_path):
    path = str(tmp_path / "id.json")
    AgentIdentity(agent_did=DID, api_key="cancelled", refresh_token="cancelled-r").save(path)
    challenge = respx.post(f"{BASE}/auth/wallet-reset/challenge").mock(
        return_value=httpx.Response(200, json=_challenge("reset text")))
    reset = respx.post(f"{BASE}/auth/wallet-reset").mock(return_value=httpx.Response(200, json=RESET_PAIR))
    after = respx.get(f"{BASE}/feed").mock(return_value=httpx.Response(200, json=[]))
    client = AgentXClient(api_key="", base_url=BASE, max_retries=0, identity_path=path)
    sign = Signer("0xwallet")

    assert client.reset_login_with(sign)["agent_did"] == DID
    assert sign.texts == ["reset text"]
    assert _body(challenge) == {"agent": DID}
    assert _body(reset) == {"agent": DID, "signature": "0xwallet"}
    for call in (challenge.calls.last, reset.calls.last):
        assert "Authorization" not in call.request.headers

    client._get("/feed")
    assert after.calls.last.request.headers["Authorization"] == "Bearer new-access"
    saved = AgentIdentity.load(path)
    assert (saved.api_key, saved.refresh_token) == ("new-access", "new-refresh")


@respx.mock
def test_reset_never_tries_to_refresh_a_cancelled_pair():
    refresh = respx.post(f"{BASE}/auth/token").mock(return_value=httpx.Response(401))
    respx.post(f"{BASE}/auth/wallet-reset").mock(return_value=httpx.Response(200, json=RESET_PAIR))
    client = _client(refresh_token="cancelled", expires_in=0)
    client.reset_login("0xs")
    assert not refresh.called
    assert client._token.access_token == "new-access"


@respx.mock
def test_refused_reset_keeps_the_old_pair():
    respx.post(f"{BASE}/auth/wallet-reset").mock(return_value=httpx.Response(
        403, json={"detail": {"error": "signature_refused"}}))
    client = _client()
    with pytest.raises(AgentXError):
        client.reset_login("0xbad")
    assert client._token.access_token == "tok"


def test_reset_without_a_known_did_says_how():
    client = AgentXClient(api_key="", base_url=BASE, max_retries=0)
    with pytest.raises(AgentXError, match="needs this agent's DID"):
        client.reset_login_challenge()


@respx.mock
def test_reset_for_a_given_did():
    route = respx.post(f"{BASE}/auth/wallet-reset/challenge").mock(
        return_value=httpx.Response(200, json=_challenge("t")))
    AgentXClient(api_key="", base_url=BASE, max_retries=0).reset_login_challenge("did:agentx:someone")
    assert _body(route) == {"agent": "did:agentx:someone"}


@respx.mock
def test_reset_answer_with_pending_notice_warns():
    respx.post(f"{BASE}/auth/wallet-reset").mock(
        return_value=httpx.Response(200, json=RESET_PAIR, headers=PENDING))
    with pytest.warns(PayoutWalletPendingWarning):
        _client().reset_login("0xs")
