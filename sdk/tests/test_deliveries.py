"""Jobs with payment terms in the Python SDK, the worker's and the buyer's
calls around a delivery (payments v1 §3.3, §3.4, §6.3.5 b; plan step W2-12a2).

Each call sends exactly the route and body the server documents; text fields
that are not strings are refused before a request is sent; the server's error
code reaches the caller in ``exc.detail``.
"""

import json

import httpx
import pytest
import respx

from agentx_sdk import AgentXClient
from agentx_sdk.auth import AgentIdentity
from agentx_sdk.exceptions import AgentXError

BASE = "http://test.agentx.local"
TASK = "9d2c1f6e-0000-4000-8000-00000000abcd"


def make_client() -> AgentXClient:
    client = AgentXClient(api_key="test-key", base_url=BASE, max_retries=0)
    client.identity = AgentIdentity(agent_did="did:agentx:worker-002", api_key="test-key")
    return client


def sent(route: respx.Route) -> dict:
    return json.loads(route.calls.last.request.content or b"{}")


class TestDeliver:
    @respx.mock
    def test_sends_result_and_preview(self):
        route = respx.post(f"{BASE}/tasks/{TASK}/result").mock(
            return_value=httpx.Response(201, json={"result_hash": "ab" * 32}))
        answer = make_client().deliver(TASK, "the full work", "the")
        assert answer == {"result_hash": "ab" * 32}
        assert sent(route) == {"result": "the full work", "preview": "the"}

    @respx.mock
    def test_content_type_only_when_given(self):
        route = respx.post(f"{BASE}/tasks/{TASK}/result").mock(return_value=httpx.Response(201, json={}))
        make_client().deliver(TASK, "# work", "#", content_type="text/markdown")
        assert sent(route) == {"result": "# work", "preview": "#", "content_type": "text/markdown"}

    @respx.mock
    def test_never_sends_result_payload(self):
        # A body with `result_payload` as well is refused by the server (one_kind_of_result).
        route = respx.post(f"{BASE}/tasks/{TASK}/result").mock(return_value=httpx.Response(201, json={}))
        make_client().deliver(TASK, "work", "w")
        assert "result_payload" not in sent(route)

    @pytest.mark.parametrize("result,preview", [
        ({"text": "x"}, "p"), ("work", None), ("work", 3), (b"work", "w"), (None, "p"),
    ])
    def test_non_text_refused_before_sending(self, result, preview):
        with respx.mock(assert_all_called=False) as mock:
            anything = mock.route().mock(return_value=httpx.Response(201, json={}))
            with pytest.raises(TypeError):
                make_client().deliver(TASK, result, preview)
            assert not anything.called

    def test_non_text_content_type_refused(self):
        with respx.mock(assert_all_called=False) as mock:
            anything = mock.route().mock(return_value=httpx.Response(201, json={}))
            with pytest.raises(TypeError):
                make_client().deliver(TASK, "work", "w", content_type=1)
            assert not anything.called

    @respx.mock
    def test_server_code_reaches_caller(self):
        respx.post(f"{BASE}/tasks/{TASK}/result").mock(return_value=httpx.Response(
            422, json={"detail": {"error": "preview_too_much_of_result", "message": "m"}}))
        with pytest.raises(AgentXError) as err:
            make_client().deliver(TASK, "work", "work")
        assert err.value.status_code == 422
        assert err.value.detail["error"] == "preview_too_much_of_result"


class TestDeliveries:
    @respx.mock
    def test_lists(self):
        listed = [{"result_hash": "cd" * 32, "preview": "p"}]
        respx.get(f"{BASE}/tasks/{TASK}/deliveries").mock(return_value=httpx.Response(200, json=listed))
        assert make_client().deliveries(TASK) == listed

    @respx.mock
    def test_empty(self):
        respx.get(f"{BASE}/tasks/{TASK}/deliveries").mock(return_value=httpx.Response(200, json=[]))
        assert make_client().deliveries(TASK) == []


class TestReviewAnswers:
    @pytest.mark.parametrize("method,path", [("revise_delivery", "revise"), ("decline_delivery", "decline")])
    @respx.mock
    def test_sends_reason(self, method, path):
        route = respx.post(f"{BASE}/tasks/{TASK}/{path}").mock(
            return_value=httpx.Response(200, json={"revisions_left": 1}))
        assert getattr(make_client(), method)(TASK, "too short") == {"revisions_left": 1}
        assert sent(route) == {"reason": "too short"}

    @pytest.mark.parametrize("method", ["revise_delivery", "decline_delivery"])
    @pytest.mark.parametrize("reason", [None, 1, ["x"]])
    def test_reason_must_be_text(self, method, reason):
        with respx.mock(assert_all_called=False) as mock:
            anything = mock.route().mock(return_value=httpx.Response(200, json={}))
            with pytest.raises(TypeError):
                getattr(make_client(), method)(TASK, reason)
            assert not anything.called

    @respx.mock
    def test_conflict_code(self):
        respx.post(f"{BASE}/tasks/{TASK}/revise").mock(return_value=httpx.Response(
            409, json={"detail": {"error": "no_revisions_left", "message": "m"}}))
        with pytest.raises(AgentXError) as err:
            make_client().revise_delivery(TASK, "again")
        assert err.value.status_code == 409
        assert err.value.detail["error"] == "no_revisions_left"


class TestBuyerFunds:
    @respx.mock
    def test_reads(self):
        reading = {"covered": True, "price_usd": "12.50", "checked_at": "t", "next_check_at": "u"}
        route = respx.get(f"{BASE}/tasks/{TASK}/buyer-funds").mock(
            return_value=httpx.Response(200, json=reading))
        assert make_client().buyer_funds(TASK) == reading
        assert route.calls.last.request.method == "GET"


class TestPaymentAnswers:
    @pytest.mark.parametrize("method,path", [("confirm_payment", "confirm"), ("reject_payment", "reject")])
    @respx.mock
    def test_posts_with_no_body_fields(self, method, path):
        route = respx.post(f"{BASE}/tasks/{TASK}/payment/{path}").mock(
            return_value=httpx.Response(200, json={"decision": path}))
        assert getattr(make_client(), method)(TASK) == {"decision": path}
        assert sent(route) == {}

    @respx.mock
    def test_not_fresh_token_is_renewed_and_sent_again(self):
        client = AgentXClient(api_key="old", refresh_token="refresh", base_url=BASE, max_retries=0)
        client.identity = AgentIdentity(agent_did="did:agentx:worker-002", api_key="old")
        respx.post(f"{BASE}/auth/token").mock(return_value=httpx.Response(
            200, json={"access_token": "new", "refresh_token": "r2", "expires_in": 3600}))
        route = respx.post(f"{BASE}/tasks/{TASK}/payment/confirm").mock(side_effect=[
            httpx.Response(401, json={"detail": {"error": "token_not_fresh"}}),
            httpx.Response(200, json={"decision": "confirm"}),
        ])
        assert client.confirm_payment(TASK) == {"decision": "confirm"}
        assert route.call_count == 2
        assert route.calls.last.request.headers["Authorization"] == "Bearer new"

    @respx.mock
    def test_nothing_to_decide(self):
        respx.post(f"{BASE}/tasks/{TASK}/payment/reject").mock(return_value=httpx.Response(
            409, json={"detail": {"error": "nothing_to_decide", "message": "m"}}))
        with pytest.raises(AgentXError) as err:
            make_client().reject_payment(TASK)
        assert err.value.detail["error"] == "nothing_to_decide"
