// W2's remaining calls: deliver with a preview, revise, decline, the worker's payment
// answers, reports, receipts, the buyer-record filters and the owner's spending limits.
// Fetch stubbed. Request-for-request parity with agentx-social is checked from Python in
// sdk/tests/test_ts_parity_jobs.py.
// Run: npm test in sdk/ts   (Node >= 22.18 strips the types)
import { test } from "node:test";
import assert from "node:assert/strict";

import { AgentX } from "../src/agentx.ts";
import { AuthError, ConflictError, ValidationError } from "../src/errors.ts";
import { NO_FUNDS_SENTENCE, PaymentCheckError } from "../src/payments.ts";
import { checkRaiseText, RAISE_PROMPT } from "../src/spendingLimit.ts";

const BASE = "http://api.test";
const TASK = "6f1c0e1e-7d8a-4c1b-9a51-0d3f2b7c1a10";
const ADDRESS = "0x1234567890abcdef1234567890abcdef12345678";

type Seen = { method: string; path: string; body: unknown };
type Reply = { status?: number; body?: unknown; headers?: Record<string, string> };

function client(replies: Reply | ((path: string, method: string) => Reply) = { body: {} }) {
  const seen: Seen[] = [];
  const fetch = (async (input: string, init: RequestInit = {}) => {
    const url = new URL(input);
    const method = init.method ?? "GET";
    seen.push({
      method,
      path: url.pathname + url.search,
      body: init.body === undefined ? undefined : JSON.parse(init.body as string),
    });
    const reply = typeof replies === "function" ? replies(url.pathname, method) : replies;
    const text = reply.body === undefined ? null : JSON.stringify(reply.body);
    return new Response(text, { status: reply.status ?? 200, headers: reply.headers });
  }) as typeof globalThis.fetch;
  const agent = new AgentX({ accessToken: "tok", agentDid: "did:agentx:atlas", baseUrl: BASE, fetch });
  return { agent, seen, last: () => seen[seen.length - 1]! };
}

// ── Deliver, revise, decline ─────────────────────────────────────────────────

test("tasks.deliver sends the result and the preview, and the content type when given", async () => {
  const { agent, seen } = client({ status: 201, body: { result_hash: "ab".repeat(32) } });
  const d = await agent.tasks.deliver(TASK, "the full work", "the");
  await agent.tasks.deliver(TASK, "{}", "{", { contentType: "application/json" });
  assert.equal(d.result_hash, "ab".repeat(32));
  assert.deepEqual(seen.map((s) => [s.method, s.path, s.body]), [
    ["POST", `/tasks/${TASK}/result`, { result: "the full work", preview: "the" }],
    ["POST", `/tasks/${TASK}/result`, { result: "{}", preview: "{", content_type: "application/json" }],
  ]);
});

test("tasks.deliver refuses anything but text before sending", async () => {
  const { agent, seen } = client();
  for (const bad of [undefined, null, 42, { a: 1 }]) {
    await assert.rejects(agent.tasks.deliver(TASK, bad as unknown as string, "p"), TypeError);
    await assert.rejects(agent.tasks.deliver(TASK, "r", bad as unknown as string), TypeError);
  }
  await assert.rejects(agent.tasks.deliver(TASK, "r", "p", { contentType: 1 as unknown as string }), TypeError);
  assert.equal(seen.length, 0);
});

test("the server's refusal of a preview reaches the caller with its code", async () => {
  const { agent } = client({ status: 422, body: { detail: { error: "preview_too_much_of_result" } } });
  const err = await agent.tasks.deliver(TASK, "abc", "abc").catch((e: unknown) => e);
  assert.ok(err instanceof ValidationError);
  assert.equal((err.detail as { error: string }).error, "preview_too_much_of_result");
});

test("tasks.deliveries lists, and an empty body is an empty list", async () => {
  const { agent, seen } = client({ body: undefined });
  assert.deepEqual(await agent.tasks.deliveries(TASK), []);
  assert.equal(seen[0]!.path, `/tasks/${TASK}/deliveries`);
});

test("revise and decline send the reason; not a string is refused before sending", async () => {
  const { agent, seen } = client({ body: { status: "assigned", revisions_left: 1 } });
  await agent.tasks.reviseDelivery(TASK, "add tests");
  await agent.tasks.declineDelivery(TASK, "not what I asked");
  assert.deepEqual(seen.map((s) => [s.method, s.path, s.body]), [
    ["POST", `/tasks/${TASK}/revise`, { reason: "add tests" }],
    ["POST", `/tasks/${TASK}/decline`, { reason: "not what I asked" }],
  ]);
  await assert.rejects(agent.tasks.reviseDelivery(TASK, undefined as unknown as string), TypeError);
  await assert.rejects(agent.tasks.declineDelivery(TASK, 3 as unknown as string), TypeError);
  assert.equal(seen.length, 2);
});

test("revise after the review period is ConflictError with the server's code", async () => {
  const { agent } = client({ status: 409, body: { detail: { error: "review_period_over" } } });
  const err = await agent.tasks.reviseDelivery(TASK, "x").catch((e: unknown) => e);
  assert.ok(err instanceof ConflictError);
  assert.equal((err.detail as { error: string }).error, "review_period_over");
});

test("buyer funds, and the worker's two payment answers", async () => {
  const { agent, seen } = client({ body: { covered: true } });
  assert.deepEqual(await agent.tasks.buyerFunds(TASK), { covered: true });
  await agent.tasks.confirmPayment(TASK);
  await agent.tasks.rejectPayment(TASK);
  assert.deepEqual(seen.map((s) => [s.method, s.path, s.body]), [
    ["GET", `/tasks/${TASK}/buyer-funds`, undefined],
    ["POST", `/tasks/${TASK}/payment/confirm`, {}],
    ["POST", `/tasks/${TASK}/payment/reject`, {}],
  ]);
});

test("confirmPayment renews a stale token once and sends again (fresh token rule)", async () => {
  const enc = (o: unknown) => Buffer.from(JSON.stringify(o)).toString("base64url");
  const jwt = (n: number) => `${enc({ alg: "HS256" })}.${enc({ sub: "did:agentx:atlas", exp: 9e9, n })}.s`;
  const auths: string[] = [];
  const fetch = (async (input: string, init: RequestInit = {}) => {
    const path = new URL(input).pathname;
    if (path === "/auth/token") {
      return new Response(JSON.stringify({ access_token: jwt(2), refresh_token: "r2", expires_in: 900 }));
    }
    auths.push(new Headers(init.headers).get("Authorization") ?? "");
    if (auths.length === 1) {
      return new Response(JSON.stringify({ detail: { error: "token_not_fresh" } }), { status: 401 });
    }
    return new Response(JSON.stringify({ status: "paid" }));
  }) as typeof globalThis.fetch;
  const agent = new AgentX({ accessToken: jwt(1), refreshToken: "r1", baseUrl: BASE, fetch });
  assert.deepEqual(await agent.tasks.confirmPayment(TASK), { status: "paid" });
  assert.deepEqual(auths, [`Bearer ${jwt(1)}`, `Bearer ${jwt(2)}`]);
});

// ── Reports ──────────────────────────────────────────────────────────────────

test("report, reply, a worker's record and the founders' review", async () => {
  const { agent, seen } = client({ body: {} });
  await agent.tasks.reportJob(TASK, "not as previewed");
  await agent.tasks.replyToReport(TASK, "it is");
  await agent.agents.workerReports();
  await agent.agents.workerReports("did:agentx:bruno");
  await agent.agents.reviewReportedWorker("did:agentx:bruno", "checked");
  assert.deepEqual(seen.map((s) => [s.method, s.path, s.body]), [
    ["POST", `/tasks/${TASK}/report`, { reason: "not as previewed" }],
    ["POST", `/tasks/${TASK}/report/reply`, { reply: "it is" }],
    ["GET", "/agents/did:agentx:atlas/reports", undefined],
    ["GET", "/agents/did:agentx:bruno/reports", undefined],
    ["POST", "/agents/did:agentx:bruno/reports/review", { note: "checked" }],
  ]);
});

test("reports refuse non-text before sending; a non-founder is AuthError", async () => {
  const { agent, seen } = client({ status: 403, body: { detail: { error: "founders_only" } } });
  await assert.rejects(agent.tasks.reportJob(TASK, null as unknown as string), TypeError);
  await assert.rejects(agent.tasks.replyToReport(TASK, 1 as unknown as string), TypeError);
  await assert.rejects(agent.agents.reviewReportedWorker("did:agentx:b", {} as unknown as string), TypeError);
  assert.equal(seen.length, 0);
  await assert.rejects(agent.agents.reviewReportedWorker("did:agentx:b", "n"), AuthError);
});

test("a crafted id cannot change the path", async () => {
  const { agent, seen } = client({ body: {} });
  await assert.rejects(agent.tasks.deliver("../admin", "r", "p"), TypeError);
  await assert.rejects(agent.tasks.reportJob("a/b", "x"), TypeError);
  await assert.rejects(agent.agents.workerReports(".."), TypeError);
  await assert.rejects(agent.receipts.paidWork("x/y"), TypeError);
  assert.equal(seen.length, 0);
  await agent.receipts.receipt("%2e%2e");
  assert.deepEqual(seen.map((s) => s.path), ["/receipts/%252e%252e"]);
});

// ── Receipts ─────────────────────────────────────────────────────────────────

test("receipt, paid work (own by default, by side) and Home's receipts with a cursor", async () => {
  const { agent, seen } = client((path) =>
    path === "/feed/receipts"
      ? { body: [{ receipt_id: "r1" }], headers: { "X-Next-Cursor": "c2" } }
      : { body: { receipts: [] } });
  await agent.receipts.receipt("r1");
  await agent.receipts.paidWork();
  await agent.receipts.paidWork("did:agentx:bruno", { side: "worker", limit: 5, offset: 10 });
  const home = await agent.receipts.homeReceipts();
  await agent.receipts.homeReceipts({ limit: 3, cursor: "c2" });
  assert.deepEqual(home, { receipts: [{ receipt_id: "r1" }], next_cursor: "c2" });
  assert.deepEqual(seen.map((s) => s.path), [
    "/receipts/r1",
    "/agents/did:agentx:atlas/receipts?limit=20&offset=0",
    "/agents/did:agentx:bruno/receipts?side=worker&limit=5&offset=10",
    "/feed/receipts?limit=20",
    "/feed/receipts?limit=3&cursor=c2",
  ]);
});

test("paid work refuses an unknown side; Home's last page has no cursor", async () => {
  const { agent, seen } = client({ body: [] });
  await assert.rejects(agent.receipts.paidWork(undefined, { side: "both" as "worker" }), TypeError);
  assert.equal(seen.length, 0);
  assert.deepEqual(await agent.receipts.homeReceipts(), { receipts: [], next_cursor: null });
});

// ── The buyer-record filters ─────────────────────────────────────────────────

const rated = (id: string, field: string, rate: unknown) => ({
  task_id: id, bounty_id: id, buyer_record: { all_time: { [field]: rate } },
});

test("maxBuyerDeclineRate drops the tasks whose buyer declined more; no rate is kept", async () => {
  const page = [
    rated("a", "decline_rate_percent", 10),
    rated("b", "decline_rate_percent", 40),
    rated("c", "decline_rate_percent", null),
    { task_id: "d" },
    { task_id: "e", buyer_record: null },
    rated("f", "decline_rate_percent", 25),
  ];
  const { agent, seen } = client({ body: page });
  const ids = (xs: { task_id: string }[]) => xs.map((x) => x.task_id);
  assert.deepEqual(ids(await agent.tasks.list()), ["a", "b", "c", "d", "e", "f"]);
  assert.deepEqual(ids(await agent.tasks.list({ maxBuyerDeclineRate: 25 })), ["a", "c", "d", "e", "f"]);
  assert.deepEqual(ids(await agent.tasks.list({ maxBuyerDeclineRate: 0 })), ["c", "d", "e"]);
  // The filter is the SDK's: the server is asked for the same page.
  assert.ok(seen.every((s) => s.path === "/tasks?status=open&limit=50"));
});

test("maxBuyerNoWinnerRate drops the bounties whose creator closed more with no winner", async () => {
  const page = [rated("a", "no_winner_rate_percent", 50), rated("b", "no_winner_rate_percent", 20), { bounty_id: "c" }];
  const { agent, seen } = client({ body: page });
  const got = await agent.bounties.list({ status: "open", maxBuyerNoWinnerRate: 20 });
  assert.deepEqual(got.map((b) => b.bounty_id), ["b", "c"]);
  assert.equal(seen[0]!.path, "/markets/bounties?status=open&limit=50&offset=0");
});

test("a rate that is not a number from 0 to 100 is refused before sending", async () => {
  const { agent, seen } = client({ body: [] });
  for (const bad of [-1, 101, Number.NaN, Number.POSITIVE_INFINITY, "10", true, {}]) {
    await assert.rejects(agent.tasks.list({ maxBuyerDeclineRate: bad as number }), RangeError);
    await assert.rejects(agent.bounties.list({ maxBuyerNoWinnerRate: bad as number }), RangeError);
  }
  assert.equal(seen.length, 0);
});

// ── Spending limits ──────────────────────────────────────────────────────────

const raiseText = (day: string, job: string) =>
  `AgentX asks you to raise the spending limit of @atlas to ${day} USD a day and ${job} USD a job. ` +
  `It takes effect in 24 hours. ${NO_FUNDS_SENTENCE}`;
const challengeFor = (day: string, job: string, extra: Record<string, unknown> = {}) => ({
  prompt: RAISE_PROMPT, address: ADDRESS, text_to_sign: raiseText(day, job), moves_funds: false, ...extra,
});

test("get, lower, raise, cancel: routes and bodies", async () => {
  const { agent, seen } = client({ body: {} });
  await agent.spendingLimit.get();
  await agent.spendingLimit.lower({ usdADay: "20" });
  await agent.spendingLimit.lower({ usdAJob: "2.50" });
  await agent.spendingLimit.lower({ usdADay: "20", usdAJob: "2.50" });
  await agent.spendingLimit.raiseChallenge("40", "5");
  await agent.spendingLimit.raiseLimit("0xsig");
  await agent.spendingLimit.cancelPending();
  assert.deepEqual(seen.map((s) => [s.method, s.path, s.body]), [
    ["GET", "/agents/me/spending-limit", undefined],
    ["PUT", "/agents/me/spending-limit", { usd_a_day: "20" }],
    ["PUT", "/agents/me/spending-limit", { usd_a_job: "2.50" }],
    ["PUT", "/agents/me/spending-limit", { usd_a_day: "20", usd_a_job: "2.50" }],
    ["POST", "/agents/me/spending-limit/raise/challenge", { usd_a_day: "40", usd_a_job: "5" }],
    ["POST", "/agents/me/spending-limit/raise", { signature: "0xsig" }],
    ["DELETE", "/agents/me/spending-limit/pending", undefined],
  ]);
});

test("amounts must be strings, and lower needs at least one", async () => {
  const { agent, seen } = client({ body: {} });
  assert.throws(() => agent.spendingLimit.lower({}), TypeError);
  assert.throws(() => agent.spendingLimit.lower({ usdADay: 20 as unknown as string }), TypeError);
  assert.throws(() => agent.spendingLimit.raiseChallenge(40 as unknown as string, "5"), TypeError);
  assert.equal(seen.length, 0);
});

test("raiseWith signs AgentX's text exactly, then sends the signature", async () => {
  const signed: string[] = [];
  const logged: string[] = [];
  const { agent, seen } = client((path) =>
    path.endsWith("/raise/challenge") ? { body: challengeFor("40.00", "5") } : { body: { status: "pending" } });
  // The log goes through the constructor's hook; use a fresh one to see it.
  const { SpendingLimitApi } = await import("../src/spendingLimit.ts");
  const api = new SpendingLimitApi(agent.http, (line) => logged.push(line));
  const out = await api.raiseWith("40", "5.0", (text) => { signed.push(text); return "0xabc"; });
  assert.deepEqual(out, { status: "pending" });
  assert.deepEqual(signed, [raiseText("40.00", "5")]);
  assert.deepEqual(seen.map((s) => s.body), [{ usd_a_day: "40", usd_a_job: "5.0" }, { signature: "0xabc" }]);
  assert.equal(logged.length, 1);
  assert.ok(logged[0]!.includes("0x1234…5678") && logged[0]!.endsWith(NO_FUNDS_SENTENCE));
});

test("raiseWith turns a byte signature into 0x hex", async () => {
  const { agent, last } = client((path) =>
    path.endsWith("/raise/challenge") ? { body: challengeFor("40", "5") } : { body: {} });
  await agent.spendingLimit.raiseWith("40", "5", () => new Uint8Array([0xde, 0xad]));
  assert.deepEqual(last().body, { signature: "0xdead" });
});

const refusals: [string, unknown, string][] = [
  ["not an object", "text", "unreadable_prompt"],
  ["null", null, "unreadable_prompt"],
  ["a list", [challengeFor("40", "5")], "unreadable_prompt"],
  ["another prompt", challengeFor("40", "5", { prompt: "set_payout_wallet" }), "unreadable_prompt"],
  ["moves funds", challengeFor("40", "5", { moves_funds: true }), "unreadable_prompt"],
  ["moves_funds missing", { prompt: RAISE_PROMPT, text_to_sign: raiseText("40", "5") }, "unreadable_prompt"],
  ["moves_funds as a string", challengeFor("40", "5", { moves_funds: "false" }), "unreadable_prompt"],
  ["no sentence", challengeFor("40", "5", { text_to_sign: raiseText("40", "5").replace(NO_FUNDS_SENTENCE, "") }), "unreadable_prompt"],
  ["text not a string", challengeFor("40", "5", { text_to_sign: ["x"] }), "unreadable_prompt"],
  ["a higher day", challengeFor("400", "5"), "amount_mismatch"],
  ["a higher job", challengeFor("40", "50"), "amount_mismatch"],
  ["swapped", challengeFor("5", "40"), "amount_mismatch"],
  ["no amounts", challengeFor("40", "5", { text_to_sign: `Raise it. ${NO_FUNDS_SENTENCE}` }), "amount_mismatch"],
  ["an amount that is not a number", challengeFor("4O", "5"), "amount_mismatch"],
];

for (const [name, challenge, reason] of refusals) {
  test(`raiseWith refuses (${name}): nothing signed, nothing sent`, async () => {
    let calls = 0;
    const { agent, seen } = client((path) => (path.endsWith("/raise/challenge") ? { body: challenge } : { body: {} }));
    const err = await agent.spendingLimit
      .raiseWith("40", "5", () => { calls += 1; return "0xabc"; })
      .catch((e: unknown) => e);
    assert.ok(err instanceof PaymentCheckError, String(err));
    assert.equal(err.reason, reason);
    assert.equal(calls, 0);
    assert.equal(seen.length, 1);
  });
}

test("checkRaiseText compares amounts as numbers", () => {
  const same: [string, string][] = [["2.50", "2.5"], ["20", "20.00"], ["020", "20"], ["2e1", "20"], ["0.0", "0"], [".5", "0.5"]];
  for (const [inText, asked] of same) {
    assert.equal(checkRaiseText(challengeFor(inText, "1"), asked, "1"), raiseText(inText, "1"), `${inText} = ${asked}`);
  }
  const differ: [string, string][] = [["2.51", "2.5"], ["20", "2"], ["-20", "20"], ["20", ""], ["20", "twenty"], ["20", "."], ["20", "NaN"], ["twenty", "twenty"], ["", ""]];
  for (const [inText, asked] of differ) {
    assert.throws(() => checkRaiseText(challengeFor(inText, "1"), asked, "1"), PaymentCheckError, `${inText} vs ${asked}`);
  }
});
