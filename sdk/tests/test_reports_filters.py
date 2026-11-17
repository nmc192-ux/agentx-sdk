"""Reports "not as previewed" and the buyer-record filters in the Python SDK
(payments v1 §3.5, §8.2; plan step W2-12a2b2).

Each report call sends exactly the route and body the server documents and
refuses non-text before sending; the server's error code reaches the caller.
The filters drop only jobs whose buyer has a rate above the limit: a buyer
with no rate yet, or a job without a record, is kept.
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
ME = "did:agentx:worker-002"
NOW = "2026-11-17T00:00:00Z"


def make_client() -> AgentXClient:
    client = AgentXClient(api_key="test-key", base_url=BASE, max_retries=0)
    client.identity = AgentIdentity(agent_did=ME, api_key="test-key")
    return client


def sent(route: respx.Route) -> dict:
    return json.loads(route.calls.last.request.content or b"{}")


class TestReportCalls:
    @respx.mock
    def test_report_job(self):
        route = respx.post(f"{BASE}/tasks/{TASK}/report").mock(
            return_value=httpx.Response(201, json={"report_id": "r1", "reason": "short"}))
        assert make_client().report_job(TASK, "short")["report_id"] == "r1"
        assert sent(route) == {"reason": "short"}

    @respx.mock
    def test_reply_to_report(self):
        route = respx.post(f"{BASE}/tasks/{TASK}/report/reply").mock(
            return_value=httpx.Response(200, json={"reply": "it is"}))
        make_client().reply_to_report(TASK, "it is")
        assert sent(route) == {"reply": "it is"}

    @respx.mock
    def test_worker_reports_defaults_to_own_did(self):
        route = respx.get(f"{BASE}/agents/{ME}/reports").mock(
            return_value=httpx.Response(200, json={"agent_did": ME, "paid_jobs": 0, "reported": 0,
                                                   "reports": [], "payment_flags": []}))
        assert make_client().worker_reports()["paid_jobs"] == 0
        assert route.called

    @respx.mock
    def test_worker_reports_of_another(self):
        route = respx.get(f"{BASE}/agents/did:agentx:other-1/reports").mock(
            return_value=httpx.Response(200, json={}))
        make_client().worker_reports("did:agentx:other-1")
        assert route.called

    def test_worker_reports_without_identity(self):
        client = AgentXClient(api_key="k", base_url=BASE, max_retries=0)
        with pytest.raises(AgentXError):
            client.worker_reports()

    @respx.mock
    def test_review_reported_worker(self):
        route = respx.post(f"{BASE}/agents/did:agentx:other-1/reports/review").mock(
            return_value=httpx.Response(201, json={"review_id": "v1"}))
        make_client().review_reported_worker("did:agentx:other-1", "looked at all three")
        assert sent(route) == {"note": "looked at all three"}

    @pytest.mark.parametrize("call", [
        lambda c: c.report_job(TASK, None),
        lambda c: c.report_job(TASK, {"reason": "x"}),
        lambda c: c.reply_to_report(TASK, b"bytes"),
        lambda c: c.review_reported_worker("did:agentx:other-1", 3),
    ])
    def test_non_text_refused_before_sending(self, call):
        with respx.mock(assert_all_called=False) as mock:
            anything = mock.route().mock(return_value=httpx.Response(201, json={}))
            with pytest.raises(TypeError):
                call(make_client())
            assert not anything.called

    @pytest.mark.parametrize("status,code", [
        (409, "not_paid"), (409, "already_reported"), (409, "report_window_over"),
        (403, "not_your_job"), (404, "no_such_task"),
    ])
    @respx.mock
    def test_server_code_reaches_caller(self, status, code):
        respx.post(f"{BASE}/tasks/{TASK}/report").mock(
            return_value=httpx.Response(status, json={"detail": {"error": code, "message": "m"}}))
        with pytest.raises(AgentXError) as caught:
            make_client().report_job(TASK, "short")
        assert code in str(caught.value.detail)

    @respx.mock
    def test_founders_only_reaches_caller(self):
        respx.post(f"{BASE}/agents/did:agentx:other-1/reports/review").mock(
            return_value=httpx.Response(403, json={"detail": {"error": "founders_only"}}))
        with pytest.raises(AgentXError) as caught:
            make_client().review_reported_worker("did:agentx:other-1", "n")
        assert "founders_only" in str(caught.value.detail)


def _task(n: int, rate, record: bool = True) -> dict:
    task = {"task_id": f"t{n}", "status": "open", "price_usd": "5.00" if record else None}
    if record:
        task["buyer_record"] = {"all_time": {"decline_rate_percent": rate},
                                "last_90_days": {"decline_rate_percent": 0}}
    return task


def _bounty(n: int, rate, record: bool = True) -> dict:
    bounty = {"bounty_id": f"0b7a4c3e-2f1d-4e5a-8b6c-9d0e1f2a3b4{n}", "creator_did": "did:agentx:b",
              "title": "t", "description": "d", "capability_required": "sql", "status": "open",
              "created_at": NOW}
    if record:
        bounty["buyer_record"] = {"all_time": {"no_winner_rate_percent": rate}}
    return bounty


class TestDeclineRateFilter:
    @respx.mock
    def test_drops_only_above_the_limit(self):
        respx.get(f"{BASE}/tasks").mock(return_value=httpx.Response(200, json=[
            _task(1, 10), _task(2, 30), _task(3, 31), _task(4, None), _task(5, None, record=False),
        ]))
        kept = make_client().list_tasks(max_buyer_decline_rate=30)
        assert [t["task_id"] for t in kept] == ["t1", "t2", "t4", "t5"]

    @respx.mock
    def test_reads_all_time_not_last_90_days(self):
        task = _task(1, 0)
        task["buyer_record"]["last_90_days"]["decline_rate_percent"] = 90
        respx.get(f"{BASE}/tasks").mock(return_value=httpx.Response(200, json=[task, _task(2, 90)]))
        assert [t["task_id"] for t in make_client().list_tasks(max_buyer_decline_rate=50)] == ["t1"]

    @respx.mock
    def test_no_filter_returns_everything_and_sends_no_extra_query(self):
        route = respx.get(f"{BASE}/tasks").mock(return_value=httpx.Response(200, json=[_task(1, 99)]))
        assert len(make_client().list_tasks()) == 1
        assert dict(route.calls.last.request.url.params) == {"status": "open", "limit": "50"}

    @respx.mock
    def test_filter_not_sent_to_server(self):
        route = respx.get(f"{BASE}/tasks").mock(return_value=httpx.Response(200, json=[]))
        make_client().list_tasks(max_buyer_decline_rate=0)
        assert "max_buyer_decline_rate" not in route.calls.last.request.url.params

    @respx.mock
    def test_zero_keeps_only_never_declined_and_unrated(self):
        respx.get(f"{BASE}/tasks").mock(return_value=httpx.Response(200, json=[
            _task(1, 0), _task(2, 1), _task(3, None)]))
        assert [t["task_id"] for t in make_client().list_tasks(max_buyer_decline_rate=0)] == ["t1", "t3"]

    @pytest.mark.parametrize("bad", [-1, 101, True, "30", float("nan")])
    def test_bad_limit_refused_before_sending(self, bad):
        with respx.mock(assert_all_called=False) as mock:
            anything = mock.route().mock(return_value=httpx.Response(200, json=[]))
            with pytest.raises(ValueError):
                make_client().list_tasks(max_buyer_decline_rate=bad)
            assert not anything.called


class TestNoWinnerRateFilter:
    @respx.mock
    def test_drops_only_above_the_limit(self):
        respx.get(f"{BASE}/markets/bounties").mock(return_value=httpx.Response(200, json=[
            _bounty(1, 20), _bounty(2, 60), _bounty(3, None), _bounty(4, None, record=False),
        ]))
        kept = make_client().list_bounties(max_buyer_no_winner_rate=50)
        assert [str(b.bounty_id)[-1] for b in kept] == ["1", "3", "4"]

    @respx.mock
    def test_record_kept_on_the_model(self):
        respx.get(f"{BASE}/markets/bounties").mock(return_value=httpx.Response(200, json=[_bounty(1, 20)]))
        [bounty] = make_client().list_bounties()
        assert bounty.buyer_record == {"all_time": {"no_winner_rate_percent": 20}}

    @pytest.mark.parametrize("bad", [-0.5, 100.5, False, "50"])
    def test_bad_limit_refused(self, bad):
        with pytest.raises(ValueError):
            make_client().list_bounties(max_buyer_no_winner_rate=bad)
