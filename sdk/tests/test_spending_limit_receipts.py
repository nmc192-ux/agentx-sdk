"""The owner's spending limits and the public receipts in the Python SDK
(payments v1 §8.2, §8.3 item 2; plan step W2-12a2b).

Each call sends exactly the route and body the server documents. The raise
helper calls the wallet only for a text that is a spending-limit raise to the
amounts asked for and says it moves no funds; anything else is refused before
the wallet is asked and nothing is sent.
"""

import json

import httpx
import pytest
import respx

from agentx_sdk import AgentXClient, PaymentCheckError
from agentx_sdk.auth import AgentIdentity
from agentx_sdk.spending_limit import check_raise_text

BASE = "http://test.agentx.local"
ME = f"{BASE}/agents/me/spending-limit"
DID = "did:agentx:buyer-002"
WALLET = "0x16FF3c2a9b1E0d4F5a6B7c8D9e0F1a2B3c4D036c"


def make_client() -> AgentXClient:
    client = AgentXClient(api_key="test-key", base_url=BASE, max_retries=0)
    client.identity = AgentIdentity(agent_did=DID, api_key="test-key")
    return client


def sent(route: respx.Route) -> dict:
    return json.loads(route.calls.last.request.content or b"{}")


def challenge(day: str = "50", job: str = "10", **over) -> dict:
    text = (f"agentx.example wants you to sign in with your Ethereum account:\n{WALLET}\n\n"
            f"Raise the spending limit of {DID} on AgentX to {day} USD a day and {job} USD a job. "
            "This signature moves no funds.\n\nURI: https://agentx.example/agents/me/spending-limit\n")
    body = {"prompt": "raise_spending_limit", "signed_by": "the payout wallet", "address": WALLET,
            "text_to_sign": text, "how_to_sign": "personal_sign (EIP-191) over text_to_sign, exactly as given",
            "expires_at": "2026-11-17T03:00:00+00:00", "moves_funds": False}
    body.update(over)
    return body


class Wallet:
    def __init__(self):
        self.asked: list[str] = []

    def __call__(self, text: str) -> str:
        self.asked.append(text)
        return "0x" + "11" * 65


class TestSee:
    @respx.mock
    def test_get(self):
        respx.get(ME).mock(return_value=httpx.Response(200, json={"limit": None}))
        assert make_client().spending_limit.get() == {"limit": None}


class TestLower:
    @respx.mock
    def test_sends_only_what_is_given(self):
        route = respx.put(ME).mock(return_value=httpx.Response(200, json={"status": "lowered"}))
        make_client().spending_limit.lower(usd_a_job="5")
        assert sent(route) == {"usd_a_job": "5"}
        make_client().spending_limit.lower(usd_a_day="20", usd_a_job="2.50")
        assert sent(route) == {"usd_a_day": "20", "usd_a_job": "2.50"}

    @pytest.mark.parametrize("kwargs", [{}, {"usd_a_day": 20}, {"usd_a_job": 2.5}, {"usd_a_day": None}])
    def test_refused_before_sending(self, kwargs):
        with respx.mock(assert_all_called=False) as mock:
            anything = mock.route().mock(return_value=httpx.Response(200, json={}))
            with pytest.raises((TypeError, ValueError)):
                make_client().spending_limit.lower(**kwargs)
            assert not anything.called


class TestRaise:
    @respx.mock
    def test_raise_with_signs_the_text_and_sends_only_the_signature(self):
        asked = respx.post(f"{ME}/raise/challenge").mock(return_value=httpx.Response(200, json=challenge()))
        done = respx.post(f"{ME}/raise").mock(return_value=httpx.Response(200, json={"status": "pending"}))
        wallet = Wallet()
        assert make_client().spending_limit.raise_with("50", "10", wallet) == {"status": "pending"}
        assert sent(asked) == {"usd_a_day": "50", "usd_a_job": "10"}
        assert wallet.asked == [challenge()["text_to_sign"]]
        assert sent(done) == {"signature": "0x" + "11" * 65}

    @respx.mock
    def test_amounts_written_differently_still_match(self):
        respx.post(f"{ME}/raise/challenge").mock(
            return_value=httpx.Response(200, json=challenge("2.5", "1")))
        respx.post(f"{ME}/raise").mock(return_value=httpx.Response(200, json={"status": "pending"}))
        make_client().spending_limit.raise_with("2.50", "1.00", Wallet())

    @pytest.mark.parametrize("bad,reason", [
        (challenge(day="500"), "amount_mismatch"),                 # not what was asked
        (challenge(job="100"), "amount_mismatch"),
        (challenge(day="NaN"), "amount_mismatch"),
        (challenge(moves_funds=True), "unreadable_prompt"),
        (challenge(moves_funds=None), "unreadable_prompt"),
        (challenge(prompt="pay_for_job"), "unreadable_prompt"),
        (challenge(text_to_sign="Pay 50 USDC to @x. This moves funds."), "unreadable_prompt"),
        (challenge(text_to_sign=None), "unreadable_prompt"),
        (challenge(text_to_sign=challenge()["text_to_sign"].replace(" This signature moves no funds.", "")),
         "unreadable_prompt"),
        (challenge(text_to_sign=challenge()["text_to_sign"].replace(" a day and ", " a week and ")),
         "amount_mismatch"),
    ])
    def test_wallet_never_asked_for_a_wrong_text(self, bad, reason):
        with respx.mock(assert_all_called=False) as mock:
            mock.post(f"{ME}/raise/challenge").mock(return_value=httpx.Response(200, json=bad))
            final = mock.post(f"{ME}/raise").mock(return_value=httpx.Response(200, json={}))
            wallet = Wallet()
            with pytest.raises(PaymentCheckError) as err:
                make_client().spending_limit.raise_with("50", "10", wallet)
            assert err.value.reason == reason
            assert wallet.asked == [] and not final.called

    def test_check_refuses_a_non_dict(self):
        with pytest.raises(PaymentCheckError):
            check_raise_text(["text"], "50", "10")

    def test_amount_as_number_refused_before_sending(self):
        with respx.mock(assert_all_called=False) as mock:
            anything = mock.route().mock(return_value=httpx.Response(200, json={}))
            with pytest.raises(TypeError):
                make_client().spending_limit.raise_with(50, "10", Wallet())
            assert not anything.called

    @respx.mock
    def test_plain_calls_and_cancel(self):
        done = respx.post(f"{ME}/raise").mock(return_value=httpx.Response(200, json={"status": "pending"}))
        gone = respx.delete(f"{ME}/pending").mock(return_value=httpx.Response(200, json={"status": "cancelled"}))
        c = make_client()
        c.spending_limit.raise_limit("0xabc")
        assert sent(done) == {"signature": "0xabc"}
        assert c.spending_limit.cancel_pending() == {"status": "cancelled"} and gone.called


class TestReceipts:
    @respx.mock
    def test_receipt(self):
        rid = "11111111-2222-4333-8444-555555555555"
        respx.get(f"{BASE}/receipts/{rid}").mock(return_value=httpx.Response(200, json={"state": "paid"}))
        assert make_client().receipt(rid) == {"state": "paid"}

    @respx.mock
    def test_paid_work_defaults_to_own_and_passes_side(self):
        route = respx.get(f"{BASE}/agents/{DID}/receipts").mock(
            return_value=httpx.Response(200, json={"receipts": []}))
        make_client().paid_work(side="buyer", limit=5)
        params = route.calls.last.request.url.params
        assert (params["side"], params["limit"], params["offset"]) == ("buyer", "5", "0")
        make_client().paid_work()
        assert "side" not in route.calls.last.request.url.params

    def test_paid_work_bad_side_refused(self):
        with pytest.raises(ValueError):
            make_client().paid_work(side="seller")

    @respx.mock
    def test_home_receipts_page(self):
        route = respx.get(f"{BASE}/feed/receipts").mock(return_value=httpx.Response(
            200, json=[{"kind": "receipt"}], headers={"X-Next-Cursor": "abc"}))
        page = make_client().home_receipts(limit=1)
        assert page == {"receipts": [{"kind": "receipt"}], "next_cursor": "abc"}
        make_client().home_receipts(cursor="abc")
        assert route.calls.last.request.url.params["cursor"] == "abc"
