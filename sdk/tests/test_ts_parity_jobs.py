"""The TypeScript SDK's W2 calls send what agentx-social sends (W2-12b2).

Deliver with a preview, revise, decline, buyer funds, the worker's payment answers,
reports, receipts, the buyer-record filters and the owner's spending limits: the same calls
through ``AgentXClient`` (httpx mock transport) and ``@agentx-social/sdk`` (Node, ``fetch``
stubbed) must make the same requests (method, path, query, JSON body), and the two
client-side filters must keep the same jobs of the same page. Skipped when no Node >= 22.18
is on PATH.
"""

import json
import subprocess

import httpx
import pytest

from agentx_sdk.auth import AgentIdentity
from agentx_sdk.client import AgentXClient

from .test_ts_parity_tasks import TS_DIR, _node, _norm

TASK = "6f1c0e1e-7d8a-4c1b-9a51-0d3f2b7c1a10"
ME = "did:agentx:atlas"
OTHER = "did:agentx:bruno"
ADDRESS = "0x1234567890abcdef1234567890abcdef12345678"
NO_FUNDS = "This signature moves no funds."
RAISE = {
    "prompt": "raise_spending_limit", "address": ADDRESS, "moves_funds": False,
    "text_to_sign": f"Raise the spending limit of @atlas to 40.00 USD a day and 5 USD a job. {NO_FUNDS}",
}
BOUNTY_FIELDS = {
    "creator_did": ME, "title": "t", "description": "d", "capability_required": "sql",
    "reward_pool": 0, "status": "open", "created_at": "2026-11-01T00:00:00Z",
}


def _rated(i: int, field: str, rate):
    return {"task_id": f"t{i}", "bounty_id": f"0b7a4c3e-2f1d-4e5a-8b6c-9d0e1f2a3b{i:02d}",
            **BOUNTY_FIELDS, "buyer_record": {"all_time": {field: rate}}}


RATES = [0, 10, 24, 25, 26, 99, 100, None]
TASK_PAGE = [_rated(i, "decline_rate_percent", r) for i, r in enumerate(RATES)] + [{"task_id": "plain"}]
BOUNTY_PAGE = ([_rated(i, "no_winner_rate_percent", r) for i, r in enumerate(RATES)]
               + [{"bounty_id": "0b7a4c3e-2f1d-4e5a-8b6c-9d0e1f2a3bff", **BOUNTY_FIELDS}])
LIMITS = [None, 0, 25, 25.5, 100]


def _reply(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/tasks":
        return httpx.Response(200, json=TASK_PAGE)
    if path == "/markets/bounties":
        return httpx.Response(200, json=BOUNTY_PAGE)
    if path.endswith("/raise/challenge"):
        return httpx.Response(200, json=RAISE)
    if path == "/feed/receipts":
        return httpx.Response(200, json=[], headers={"X-Next-Cursor": "c2"})
    if path.endswith("/deliveries"):
        return httpx.Response(200, json=[])
    return httpx.Response(200, json={})


def _python() -> tuple[list[dict], list]:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        seen.append(_norm(request.method, str(request.url), body))
        return _reply(request)

    c = AgentXClient(api_key="k", base_url="http://api.test")
    c._http = httpx.Client(base_url="http://api.test", transport=httpx.MockTransport(handler))
    c.identity = AgentIdentity(agent_did=ME, api_key="k")

    c.deliver(TASK, "the full work", "the")
    c.deliver(TASK, "{}", "{", content_type="application/json")
    c.deliveries(TASK)
    c.revise_delivery(TASK, "add tests")
    c.decline_delivery(TASK, "not what I asked")
    c.buyer_funds(TASK)
    c.confirm_payment(TASK)
    c.reject_payment(TASK)
    c.report_job(TASK, "not as previewed")
    c.reply_to_report(TASK, "it is")
    c.worker_reports()
    c.worker_reports(OTHER)
    c.review_reported_worker(OTHER, "checked")
    c.receipt("r1")
    c.paid_work()
    c.paid_work(OTHER, side="worker", limit=5, offset=10)
    c.paid_work(side="buyer")
    c.home_receipts()
    c.home_receipts(limit=3, cursor="c2")
    s = c.spending_limit
    s.get()
    s.lower(usd_a_day="20")
    s.lower(usd_a_job="2.50")
    s.lower(usd_a_day="20", usd_a_job="2.50")
    s.raise_challenge("40", "5")
    s.raise_limit("0xsig")
    s.raise_with("40", "5.0", lambda text: "0xabc")
    s.cancel_pending()

    kept = []
    for limit in LIMITS:
        kept.append([t["task_id"] for t in c.list_tasks(max_buyer_decline_rate=limit)])
        kept.append([str(b.bounty_id) for b in c.list_bounties(status="open", max_buyer_no_winner_rate=limit)])
    return seen, kept


TS_SCRIPT = """
import { AgentX } from "./src/index.ts";
const [TASK, ME, OTHER, RAISE, TASK_PAGE, BOUNTY_PAGE, LIMITS] = %s;
const seen = [];
const fetch = async (input, init = {}) => {
  seen.push({ method: init.method ?? "GET", url: input,
              body: init.body === undefined ? null : JSON.parse(init.body) });
  const path = new URL(input).pathname;
  const json = (b, h) => new Response(JSON.stringify(b), { status: 200, headers: h });
  if (path === "/tasks") return json(TASK_PAGE);
  if (path === "/markets/bounties") return json(BOUNTY_PAGE);
  if (path.endsWith("/raise/challenge")) return json(RAISE);
  if (path === "/feed/receipts") return json([], { "X-Next-Cursor": "c2" });
  if (path.endsWith("/deliveries")) return json([]);
  return json({});
};
const a = new AgentX({ accessToken: "k", agentDid: ME, baseUrl: "http://api.test", fetch });
const t = a.tasks;
await t.deliver(TASK, "the full work", "the");
await t.deliver(TASK, "{}", "{", { contentType: "application/json" });
await t.deliveries(TASK);
await t.reviseDelivery(TASK, "add tests");
await t.declineDelivery(TASK, "not what I asked");
await t.buyerFunds(TASK);
await t.confirmPayment(TASK);
await t.rejectPayment(TASK);
await t.reportJob(TASK, "not as previewed");
await t.replyToReport(TASK, "it is");
await a.agents.workerReports();
await a.agents.workerReports(OTHER);
await a.agents.reviewReportedWorker(OTHER, "checked");
await a.receipts.receipt("r1");
await a.receipts.paidWork();
await a.receipts.paidWork(OTHER, { side: "worker", limit: 5, offset: 10 });
await a.receipts.paidWork(undefined, { side: "buyer" });
await a.receipts.homeReceipts();
await a.receipts.homeReceipts({ limit: 3, cursor: "c2" });
const s = a.spendingLimit;
await s.get();
await s.lower({ usdADay: "20" });
await s.lower({ usdAJob: "2.50" });
await s.lower({ usdADay: "20", usdAJob: "2.50" });
await s.raiseChallenge("40", "5");
await s.raiseLimit("0xsig");
console.warn = () => {};
await s.raiseWith("40", "5.0", () => "0xabc");
await s.cancelPending();
const kept = [];
for (const limit of LIMITS) {
  const o = limit === null ? {} : { maxBuyerDeclineRate: limit };
  kept.push((await t.list(o)).map((x) => x.task_id));
  const b = limit === null ? { status: "open" } : { status: "open", maxBuyerNoWinnerRate: limit };
  kept.push((await a.bounties.list(b)).map((x) => x.bounty_id));
}
console.log(JSON.stringify({ seen, kept }));
"""


def _ts(node: str) -> tuple[list[dict], list]:
    script = TS_SCRIPT % json.dumps([TASK, ME, OTHER, RAISE, TASK_PAGE, BOUNTY_PAGE, LIMITS])
    proc = subprocess.run([node, "--input-type=module", "-e", script],
                          capture_output=True, text=True, timeout=60, cwd=TS_DIR)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    return [_norm(r["method"], r["url"], r["body"]) for r in out["seen"]], out["kept"]


@pytest.fixture(scope="module")
def both():
    node = _node()
    if node is None:
        pytest.skip("Node >= 22.18 not available")
    return _python(), _ts(node)


def test_ts_w2_calls_send_what_agentx_social_sends(both):
    (py, _), (ts, _) = both
    assert len(py) == len(ts) == 28 + 2 * len(LIMITS)
    for i, (p, t) in enumerate(zip(py, ts)):
        assert p == t, f"call {i}: agentx-social {p} != @agentx-social/sdk {t}"


def test_ts_buyer_record_filters_keep_what_agentx_social_keeps(both):
    (_, py_kept), (_, ts_kept) = both
    assert py_kept == ts_kept
    # The filter does something: at 25 the 26, 99 and 100 rates are gone, the rest kept.
    assert py_kept[4] == ["t0", "t1", "t2", "t3", "t7", "plain"]


AMOUNT_PAIRS = [
    ("2.50", "2.5"), ("20", "20.00"), ("020", "20"), ("2e1", "20"), ("0.0", "0"), (".5", "0.5"),
    ("-0", "0"), ("2.51", "2.5"), ("20", "2"), ("-20", "20"), ("20", ""), ("20", "twenty"),
    ("20", "."), ("20", "NaN"), ("NaN", "NaN"), ("twenty", "twenty"), ("1E+2", "100"), ("5.", "5"), ("25", "25.000001"),
]

AMOUNTS_SCRIPT = """
import { checkRaiseText } from "./src/spendingLimit.ts";
const [PAIRS, NO_FUNDS] = %s;
const out = PAIRS.map(([inText, asked]) => {
  const c = { prompt: "raise_spending_limit", moves_funds: false,
              text_to_sign: `Raise it to ${inText} USD a day and 1 USD a job. ${NO_FUNDS}` };
  try { checkRaiseText(c, asked, "1"); return "ok"; } catch (e) { return e.reason; }
});
console.log(JSON.stringify(out));
"""


def test_ts_raise_amounts_are_read_as_agentx_social_reads_them():
    """The one check before the payout wallet signs: equal amounts in both SDKs."""
    from agentx_sdk.payments import PaymentCheckError
    from agentx_sdk.spending_limit import check_raise_text

    node = _node()
    if node is None:
        pytest.skip("Node >= 22.18 not available")
    py = []
    for in_text, asked in AMOUNT_PAIRS:
        challenge = {"prompt": "raise_spending_limit", "moves_funds": False,
                     "text_to_sign": f"Raise it to {in_text} USD a day and 1 USD a job. {NO_FUNDS}"}
        try:
            check_raise_text(challenge, asked, "1")
            py.append("ok")
        except PaymentCheckError as exc:
            py.append(exc.reason)
    proc = subprocess.run(
        [node, "--input-type=module", "-e", AMOUNTS_SCRIPT % json.dumps([AMOUNT_PAIRS, NO_FUNDS])],
        capture_output=True, text=True, timeout=60, cwd=TS_DIR)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip().splitlines()[-1]) == py
    assert py.count("ok") == 9
