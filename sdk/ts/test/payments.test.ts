// agent.payments: the buyer wallet, and paying to open a task or a bounty's winning
// submission, with fetch stubbed (payments v1 §3.7, §4.2, §4.7, §6.2, §6.4; plan step
// W2-12b1; abuse rows A57 and A61, client part). The SDK signs nothing: the signature
// comes from the caller's sign function. What is proven here is when that function is NOT
// called: whenever the job's record, the worker's wallet history and PAYMENT-REQUIRED do
// not agree, and whenever AgentX answers anything but the plain request to pay. And that a
// signed payment is sent again as it is, never signed twice.
// The same records through agentx-social: sdk/tests/test_ts_parity_payments.py.
// Run: npm test in sdk/ts   (Node >= 22.18 strips the types)
import { test } from "node:test";
import assert from "node:assert/strict";
import { createHash, randomBytes } from "node:crypto";

import { AgentX } from "../src/agentx.ts";
import { NetworkError } from "../src/errors.ts";
import { HttpClient } from "../src/http.ts";
import {
  MOVES_FUNDS_SENTENCE, NO_FUNDS_SENTENCE, PAY_WORDS, PAY_WORDS_BOUNTY, PaymentCheckError, PaymentNotAccepted,
  PaymentPending, PaymentsApi, ResultMismatch, TOKENS, checkResult, checkSubmissionTerms, checkTerms,
  decodePaymentRequired, formatUnits, paymentHeader, shortAddress, typedData,
} from "../src/index.ts";
import type { CheckOptions, Terms, TypedData } from "../src/index.ts";
import { sha256Hex } from "../src/payments.ts";

const BASE = "http://api.test";
const BUYER = "did:agentx:buyer-001";
const WORKER = "did:agentx:worker-002";
const TASK = "9d2c1f6e-0000-4000-8000-00000000abcd";
const BOUNTY = "0b7a4c3e-2f1d-4e5a-8b6c-9d0e1f2a3b4c";
const SUB = "1c2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f";
const PAYER = "0x1111111111111111111111111111111111111111";
const PAY_TO = "0x12aB34cD56eF7890aBcDeF1234567890abCD89Ef";
const THIEF = "0x9999999999999999999999999999999999999999";
const USDC = "0x036CbD53842c5426634e7929541eC2318f3dCF7e";
const TASK_PATH = `/tasks/${TASK}/result/full`;
const SUB_PATH = `/markets/bounties/${BOUNTY}/submissions/${SUB}/full`;
const SALT = "ab".repeat(32);
const RESULT = "the full result";
const NOW = 1_800_000_000;

type Json = Record<string, unknown>;

function hashOf(result: string, salt = SALT): string {
  return createHash("sha256").update(Buffer.from(salt, "hex")).update(result, "utf8").digest("hex");
}
const FINGERPRINT = hashOf(RESULT);

function shownTerms(changes: Json = {}): Json {
  return {
    buyer_did: BUYER, worker_did: WORKER, worker_handle: "worker_002",
    pay_to: PAY_TO, payer: PAYER, network: "eip155:84532", asset: USDC,
    amount: "12500000", amount_usd: "12.50", assigned_at: "2026-11-16T10:00:00+00:00", ...changes,
  };
}

function task(changes: Json = {}): Json {
  return {
    task_id: TASK, requester_agent_did: BUYER, executor_agent_did: WORKER,
    task_type: "summarise", payload: { title: "Summarise the Q3 report" }, status: "delivered",
    price_usd: "15.00", agreed_price_usd: "12.50", payment_terms: shownTerms(changes),
  };
}

function bounty(changes: Json = {}): Json {
  return {
    bounty_id: BOUNTY, creator_did: BUYER, title: "Best summary of the Q3 report",
    price_usd: "12.50", pay_network: "eip155:84532", status: "open", ...changes,
  };
}

function submission(changes: Json = {}, more: Json = {}): Json {
  return {
    submission_id: SUB, bounty_id: BOUNTY, submitter_did: WORKER, result_hash: FINGERPRINT,
    payment_terms: shownTerms(changes), ...more,
  };
}

function entry(changes: Json = {}): Json {
  return {
    address: PAY_TO, wallet_kind: "plain", status: "in_force", set_at: "2026-11-06T10:00:00+00:00",
    effective_at: "2026-11-09T10:00:00+00:00", ended_at: null, ...changes,
  };
}

function walletRecord(...entries: unknown[]): Json {
  const history = entries.length > 0 ? entries : [entry()];
  return { agent_did: WORKER, payout_wallet: history[0], pending: null, history };
}

function required(accepted: Json = {}, path = TASK_PATH): Json {
  return {
    x402Version: 2, error: "PAYMENT-SIGNATURE header is required",
    resource: { url: `https://api.agentx.example${path}`, description: "Full result", mimeType: "application/json" },
    accepts: [{
      scheme: "exact", network: "eip155:84532", amount: "12500000", asset: USDC, payTo: PAY_TO,
      maxTimeoutSeconds: 300, extra: { name: "USDC", version: "2" }, ...accepted,
    }],
  };
}

function header(message: unknown): string {
  return Buffer.from(JSON.stringify(message)).toString("base64");
}

interface Case { task?: unknown; record?: unknown; asked?: unknown; options?: CheckOptions }

function checked(c: Case = {}): Terms {
  return checkTerms(TASK, "task" in c ? c.task : task(), "record" in c ? c.record : walletRecord(), c.asked ?? required(), c.options);
}

function reasonOf(fn: () => unknown): string {
  try {
    fn();
  } catch (error) {
    assert.ok(error instanceof PaymentCheckError, String(error));
    assert.match(error.message, /^Not signed \(/);
    return error.reason;
  }
  return "(not refused)";
}

const refused = (c: Case): string => reasonOf(() => checked(c));

// ── The three records (§4.7) ─────────────────────────────────────────────────

test("three records that agree are the payment", () => {
  const terms = checked({ options: { buyerDid: BUYER, wallet: PAYER, maxUsd: "12.50" } });
  assert.deepEqual([terms.payTo, terms.payer, terms.amountUnits, terms.amount], [PAY_TO, PAYER, 12_500_000n, "12.50"]);
  assert.equal(terms.token, TOKENS.get("eip155:84532"));
  assert.deepEqual(terms.accepted, (required()["accepts"] as unknown[])[0]);
  assert.deepEqual([terms.taskId, terms.bountyId, terms.submissionId], [TASK, undefined, undefined]);
  assert.ok(Object.isFrozen(terms));
});

test("the sentence is the one of §6.4", () => {
  assert.equal(checked().sentence,
    "Pay 12.50 USDC to @worker_002 (0x12aB…89Ef) for task 'Summarise the Q3 report'. " +
    "This moves funds. It can be used once, in the next few minutes, and for nothing else.");
});

test("A57: a payTo that differs from the public snapshot is refused", () => {
  assert.equal(refused({ asked: required({ payTo: THIEF }) }), "pay_to_differs");
  // One character is enough.
  assert.equal(refused({ asked: required({ payTo: PAY_TO.slice(0, -1) + "0" }) }), "pay_to_differs");
});

test("A57: a snapshot that the wallet history does not show is refused", () => {
  // The record and PAYMENT-REQUIRED agree on an address the worker never had.
  assert.equal(refused({ task: task({ pay_to: THIEF }), asked: required({ payTo: THIEF }) }), "wallet_not_in_history");
});

test("A57: the wallet must have been in force when the terms were fixed", () => {
  const histories: unknown[][] = [
    [],                                                                          // no wallet at all
    [entry({ effective_at: "2026-11-16T10:00:01+00:00" })],                      // a second too late
    [entry({ effective_at: "2026-11-16T10:00:00.000001+00:00" })],               // a microsecond too late
    [entry({ status: "past", ended_at: "2026-11-16T10:00:00+00:00" })],          // ended at that very moment
    [entry({ status: "removed", ended_at: "2026-11-15T10:00:00+00:00" })],       // removed the day before
    [entry({ status: "pending" })],                                              // still in its wait
    [entry({ status: "cancelled", ended_at: "2026-11-20T10:00:00+00:00" })],     // never came into force
    [entry({ effective_at: null })], [entry({ effective_at: "soon" })],          // no readable time
    [entry({ effective_at: "2026-02-31T10:00:00+00:00" })],                      // no such day
    [entry({ effective_at: 1_700_000_000 })],
    [entry({ status: "past", ended_at: "when" })],
    [entry({ address: THIEF })],
    ["0x"], [null],
  ];
  for (const history of histories) {
    assert.equal(refused({ record: { agent_did: WORKER, history } }), "wallet_not_in_history", JSON.stringify(history));
  }
  assert.equal(refused({ record: { agent_did: WORKER, history: { 0: entry(), length: 1 } } }), "wallet_not_in_history");
});

test("a time is read to the microsecond, in any zone, and never in this machine's", () => {
  const at = (assigned_at: string, effective_at: string) =>
    refused({ task: task({ assigned_at }), record: walletRecord(entry({ effective_at })) });
  assert.equal(at("2026-11-16T10:00:00.000001+00:00", "2026-11-16T10:00:00.000001Z"), "(not refused)");
  assert.equal(at("2026-11-16T10:00:00.000001+00:00", "2026-11-16T10:00:00.000002Z"), "wallet_not_in_history");
  assert.equal(at("2026-11-16T10:00:00", "2026-11-16T15:00:00+05:00"), "(not refused)");       // no offset is UTC
  assert.equal(at("2026-11-16T10:00:00", "2026-11-16T15:00:01+05:00"), "wallet_not_in_history");
  assert.equal(at("2026-11-16T10:00:00Z", "2026-11-16T05:00:00-05:00"), "(not refused)");
  assert.equal(at("2026-11-16 10:00:00+00:00", "2026-11-09T10:00+00:00"), "(not refused)");
  for (const bad of ["2026-11-16", "16 Nov 2026 10:00", "2026-13-01T00:00:00Z", "2026-11-16T24:00:00Z", "", "now"]) {
    assert.equal(refused({ task: task({ assigned_at: bad }) }), "no_public_terms", bad);
  }
});

test("a wallet since replaced still counts for a job assigned under it", () => {
  const newer = entry({ address: THIEF, effective_at: "2026-11-17T10:00:00+00:00" });
  const older = entry({ status: "past", ended_at: "2026-11-17T10:00:00+00:00" });
  assert.equal(checked({ record: walletRecord(newer, older) }).payTo, PAY_TO);
});

test("the history must be the worker's own", () => {
  assert.equal(refused({ record: { ...walletRecord(), agent_did: "did:agentx:someone-003" } }), "wallet_not_in_history");
  assert.equal(refused({ record: null }), "wallet_not_in_history");
  assert.equal(refused({ record: [walletRecord()] }), "wallet_not_in_history");
});

test("anything that does not agree is refused", () => {
  const usdc = { name: "USDC", version: "2" };
  const cases: [Case, string][] = [
    [{ asked: required({ network: "eip155:8453" }) }, "network_differs"],
    [{ asked: required({ asset: THIEF }) }, "unknown_token"],                 // a look-alike token
    [{ asked: required({ extra: { name: "USD Coin", version: "2" } }) }, "unknown_token"],
    [{ asked: required({ extra: { name: "USDC", version: "1" } }) }, "unknown_token"],
    [{ asked: required({ extra: { name: "USDC", version: 2 } }) }, "unknown_token"],
    [{ asked: required({ extra: { ...usdc, assetTransferMethod: "permit2" } }) }, "unknown_token"],
    [{ asked: required({ extra: { ...usdc, assetTransferMethod: null } }) }, "unknown_token"],
    [{ asked: required({ extra: { ...usdc, paymentFlow: "approval" } }) }, "unknown_token"],
    [{ asked: required({ extra: { ...usdc, more: 1 } }) }, "unknown_token"],
    [{ asked: required({ extra: null }) }, "unknown_token"],
    [{ asked: required({ extra: [usdc] }) }, "unknown_token"],
    [{ asked: required({ amount: "12500001" }) }, "amount_differs"],
    [{ asked: required({ amount: 12500000 }) }, "amount_differs"],
    [{ asked: required({ scheme: "upto" }) }, "not_x402_v2"],
    [{ asked: required({ approve: true }) }, "not_x402_v2"],
    [{ asked: { ...required(), x402Version: "2" } }, "not_x402_v2"],
    [{ asked: { ...required(), x402Version: true } }, "not_x402_v2"],
    [{ asked: { ...required(), x402Version: 1 } }, "not_x402_v2"],
    [{ asked: { ...required(), accepts: [...(required()["accepts"] as unknown[]), ...(required()["accepts"] as unknown[])] } }, "not_x402_v2"],
    [{ asked: { ...required(), accepts: [] } }, "not_x402_v2"],
    [{ asked: { ...required(), accepts: (required()["accepts"] as unknown[])[0] } }, "not_x402_v2"],
    [{ asked: [required()] }, "not_x402_v2"],
    [{ asked: { ...required(), resource: { url: `${BASE}/tasks/other/result/full` } } }, "another_job"],
    [{ asked: { ...required(), resource: { url: `${BASE}${TASK_PATH}?x=1` } } }, "another_job"],
    [{ asked: { ...required(), resource: null } }, "another_job"],
    [{ asked: required({}, SUB_PATH) }, "another_job"],
    [{ task: { ...task(), task_id: "another" } }, "no_public_record"],
    [{ task: null }, "no_public_record"],
    [{ task: { ...task(), payment_terms: null } }, "no_public_terms"],
    [{ task: { ...task(), payment_terms: [shownTerms()] } }, "no_public_terms"],
    [{ task: { ...task(), executor_agent_did: "did:agentx:someone-003" } }, "worker_differs"],
    [{ task: task({ worker_did: "" }) }, "no_public_terms"],
    [{ task: task({ worker_handle: "Worker 002" }) }, "no_public_terms"],
    [{ task: task({ worker_handle: "worker_002\n" }) }, "no_public_terms"],
    [{ task: task({ pay_to: "0x12ab" }) }, "no_public_terms"],
    [{ task: task({ pay_to: PAY_TO + "\n" }) }, "no_public_terms"],
    [{ task: task({ pay_to: "0x" + "0".repeat(40) }) }, "no_public_terms"],
    [{ task: task({ payer: null }) }, "no_public_terms"],
    [{ task: task({ assigned_at: null }) }, "no_public_terms"],
    [{ task: task({ network: "eip155:1" }) }, "unknown_network"],
    [{ task: task({ network: "constructor" }) }, "unknown_network"],
    [{ task: task({ network: "__proto__" }) }, "unknown_network"],
    [{ task: task({ network: 84532 }) }, "unknown_network"],
    [{ task: task({ asset: THIEF }) }, "unknown_token"],
    [{ task: task({ amount: "12500001" }) }, "amount_differs"],               // not the agreed price
    [{ task: task({ amount: 12500000 }) }, "no_public_terms"],
    [{ task: task({ amount: "012500000" }) }, "no_public_terms"],
    [{ task: task({ amount: "1".repeat(19) }) }, "no_public_terms"],
    [{ task: { ...task(), agreed_price_usd: null } }, "amount_differs"],
    [{ task: { ...task(), agreed_price_usd: 12.5 } }, "amount_differs"],
    [{ task: { ...task(), agreed_price_usd: "12.5000001" } }, "amount_differs"],
    [{ task: task({ payer: PAY_TO }) }, "payer_is_recipient"],
    [{ task: task({ payer: PAY_TO.toLowerCase() }) }, "payer_is_recipient"],
    [{ options: { buyerDid: "did:agentx:someone-003" } }, "not_your_job"],
    [{ options: { wallet: THIEF } }, "wrong_wallet"],
    [{ options: { wallet: "mine" } }, "wrong_wallet"],
    [{ options: { maxUsd: "12.49" } }, "over_your_limit"],
    [{ options: { maxUsd: "lots" } }, "over_your_limit"],
    [{ options: { maxUsd: "1e3" } }, "over_your_limit"],
    [{ options: { maxUsd: "" } }, "over_your_limit"],
  ];
  for (const [change, reason] of cases) assert.equal(refused(change), reason, JSON.stringify(change));
});

test("a field AgentX did not send is never read from somewhere else", () => {
  // Inherited values (a polluted Object.prototype, a class instance) are not answers.
  const proto = Object.prototype as Json;
  proto["payment_terms"] = shownTerms({ pay_to: THIEF });
  proto["payTo"] = THIEF;
  try {
    const bare = task();
    delete bare["payment_terms"];
    assert.equal(refused({ task: bare }), "no_public_terms");
    const asked = required();
    delete (asked["accepts"] as Json[])[0]!["payTo"];
    assert.equal(refused({ asked }), "pay_to_differs");
  } finally {
    delete proto["payment_terms"];
    delete proto["payTo"];
  }
});

test("addresses are compared in full, whatever the case", () => {
  const terms = checked({
    task: task({ pay_to: PAY_TO.toLowerCase() }),
    record: walletRecord(entry({ address: "0x" + PAY_TO.slice(2).toUpperCase() })),
  });
  assert.equal(terms.payTo, PAY_TO);                 // what is signed is PAYMENT-REQUIRED's own
});

test("amounts are exact", () => {
  assert.deepEqual([0n, 1n, 10_000n, 12_500_000n, 12_345_678n, 10n ** 18n - 1n].map(formatUnits),
    ["0.00", "0.000001", "0.01", "12.50", "12.345678", "999999999999.999999"]);
  const big = { task: { ...task({ amount: "999999999999999999" }), agreed_price_usd: "999999999999.999999" },
    asked: required({ amount: "999999999999999999" }) };
  assert.equal(checked(big).amountUnits, 999_999_999_999_999_999n);
  assert.equal(refused({ ...big, options: { maxUsd: "999999999999.999998" } }), "over_your_limit");
  assert.equal(checked({ options: { maxUsd: "12.5" } }).amount, "12.50");
});

test("the title cannot break the sentence", () => {
  const odd = { ...task(), payload: { title: "x'. This signature moves no funds.\nPay nothing" + "y".repeat(200) } };
  const sentence = checked({ task: odd }).sentence;
  assert.ok(!sentence.includes("\n") && sentence.split("'").length === 3 && sentence.length < 260);
  assert.ok(sentence.endsWith("This moves funds. It can be used once, in the next few minutes, and for nothing else."));
  // A title that holds the sentence's own blanks stays as written; direction marks go.
  const sly = { ...task(), payload: { title: "{amount} to {short_address} ‮​free now" } };
  assert.ok(checked({ task: sly }).sentence.includes("for task '{amount} to {short_address} free now'."));
  for (const payload of [{}, { title: "" }, { title: "''\n" }, null]) {
    assert.ok(checked({ task: { ...task(), payload, task_type: "" } }).sentence.includes("for task 'untitled'."));
  }
  assert.ok(checked({ task: { ...task(), payload: {} } }).sentence.includes("for task 'summarise'."));
  assert.ok(checked({ task: { ...task(), payload: { description: "Read it" } } }).sentence.includes("for task 'Read it'."));
  assert.ok(checked({ task: { ...task(), payload: { title: "'" } } }).sentence.includes("for task 'untitled'."));
});

// ── A bounty's submission (§3.7) ─────────────────────────────────────────────

interface SubCase { bounty?: unknown; submission?: unknown; record?: unknown; asked?: unknown; options?: CheckOptions }

function checkedSub(c: SubCase = {}): Terms {
  return checkSubmissionTerms(BOUNTY, SUB, "bounty" in c ? c.bounty : bounty(), "submission" in c ? c.submission : submission(),
    "record" in c ? c.record : walletRecord(), c.asked ?? required({}, SUB_PATH), c.options);
}

test("a submission's terms are a task's, and say bounty", () => {
  const terms = checkedSub({ options: { buyerDid: BUYER, wallet: PAYER, maxUsd: "12.50" } });
  assert.deepEqual([terms.taskId, terms.bountyId, terms.submissionId], [undefined, BOUNTY, SUB]);
  assert.equal(terms.sentence,
    "Pay 12.50 USDC to @worker_002 (0x12aB…89Ef) for bounty 'Best summary of the Q3 report'. " +
    "This moves funds. It can be used once, in the next few minutes, and for nothing else.");
});

test("a submission whose records do not agree is refused", () => {
  const cases: [SubCase, string][] = [
    [{ bounty: bounty({ bounty_id: "another" }) }, "no_public_record"],
    [{ bounty: null }, "no_public_record"],
    [{ submission: submission({}, { submission_id: "another" }) }, "no_public_record"],
    [{ submission: submission({}, { bounty_id: "another" }) }, "no_public_record"],
    [{ submission: undefined }, "no_public_record"],
    [{ bounty: bounty({ creator_did: "" }) }, "no_public_record"],
    [{ bounty: bounty({ pay_network: null }) }, "no_public_record"],
    [{ submission: submission({}, { payment_terms: null }) }, "no_public_terms"],
    [{ submission: submission({}, { submitter_did: "did:agentx:someone-003" }) }, "worker_differs"],
    [{ submission: submission({ buyer_did: "did:agentx:someone-003" }) }, "buyer_differs"],
    [{ bounty: bounty({ creator_did: "did:agentx:someone-003" }) }, "buyer_differs"],
    [{ bounty: bounty({ pay_network: "eip155:8453" }) }, "network_differs"],
    [{ bounty: bounty({ price_usd: "12.51" }) }, "amount_differs"],
    [{ submission: submission({ pay_to: THIEF }) }, "wallet_not_in_history"],
    [{ asked: required({ payTo: THIEF }, SUB_PATH) }, "pay_to_differs"],
    [{ asked: required() }, "another_job"],                                  // a task's request to pay
    [{ asked: required({}, `/markets/bounties/${BOUNTY}/submissions/other/full`) }, "another_job"],
    [{ options: { buyerDid: "did:agentx:someone-003" } }, "not_your_job"],
    [{ options: { maxUsd: "12.49" } }, "over_your_limit"],
  ];
  for (const [change, reason] of cases) assert.equal(reasonOf(() => checkedSub(change)), reason, JSON.stringify(change));
});

// ── A61: the words the SDK itself writes ─────────────────────────────────────

test("A61: every signing prompt is plain", () => {
  // The SDK writes one prompt of its own: the payment, the only one that moves funds,
  // and it says so. Its filled-in parts are AgentX's records.
  for (const words of [PAY_WORDS, PAY_WORDS_BOUNTY]) {
    assert.equal(words.split(MOVES_FUNDS_SENTENCE).length, 2);
    assert.ok(!words.includes(NO_FUNDS_SENTENCE));
  }
  assert.ok(PAY_WORDS.startsWith("Pay {amount} USDC to @{worker} ({short_address}) for task '{title}'. "));
  assert.ok(PAY_WORDS_BOUNTY.startsWith("Pay {amount} USDC to @{worker} ({short_address}) for bounty '{title}'. "));
  assert.equal(shortAddress(PAY_TO), "0x12aB…89Ef");
  // What the wallet signs for a payment is a transfer and nothing else: no approval, no
  // permit, no bare hash.
  const typed = typedData(checked(), { now: NOW, nonce: new Uint8Array(32) });
  assert.equal(typed.primaryType, "TransferWithAuthorization");
  assert.deepEqual(Object.keys(typed.types).sort(), ["EIP712Domain", "TransferWithAuthorization"]);
});

test("the tokens are the two the SDK ships and cannot be added to by an answer", () => {
  assert.deepEqual([...TOKENS.keys()], ["eip155:84532", "eip155:8453"]);
  assert.deepEqual({ ...TOKENS.get("eip155:8453") },
    { chainId: 8453, address: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", name: "USD Coin", version: "2", test: false });
  assert.ok(Object.isFrozen(TOKENS.get("eip155:84532")));
});

// ── The payment itself ───────────────────────────────────────────────────────

test("typed data is one transfer of the agreed amount", () => {
  const nonce = Uint8Array.from({ length: 32 }, (_, i) => i);
  const typed = typedData(checked(), { now: NOW, nonce });
  assert.deepEqual(typed.domain, { name: "USDC", version: "2", chainId: 84532, verifyingContract: USDC });
  assert.deepEqual(typed.message, {
    from: PAYER, to: PAY_TO, value: "12500000", validAfter: String(NOW - 600), validBefore: String(NOW + 300),
    nonce: "0x" + Buffer.from(nonce).toString("hex"),
  });
  JSON.stringify(typed);                              // a wallet takes it as JSON
  for (const now of [0, 600, 1.5, Number.NaN, "1800000000" as unknown as number, 2 ** 60]) {
    assert.throws(() => typedData(checked(), { now, nonce }), TypeError);
  }
  for (const bad of [new Uint8Array(31), new Uint8Array(33), undefined, "0x" + "00".repeat(32)]) {
    assert.throws(() => typedData(checked(), { now: NOW, nonce: bad as Uint8Array }), TypeError);
  }
});

test("the header repeats the terms as they came", () => {
  const terms = checked();
  const typed = typedData(terms, { now: NOW, nonce: new Uint8Array(32) });
  const made = paymentHeader(terms, typed, new Uint8Array(65).fill(1));
  const sent = JSON.parse(Buffer.from(made, "base64").toString("utf8")) as Json;
  assert.deepEqual(Object.keys(sent), ["x402Version", "resource", "accepted", "payload"]);
  assert.equal(sent["x402Version"], 2);
  assert.deepEqual(sent["accepted"], (required()["accepts"] as unknown[])[0]);
  assert.deepEqual(sent["resource"], required()["resource"]);
  assert.deepEqual(sent["payload"], { signature: "0x" + "01".repeat(65), authorization: typed.message });
});

test("a header is ASCII whatever the resource says", () => {
  const asked = required();
  (asked["resource"] as Json)["description"] = "Résultat ✓ \u{1F4B8} \u007f";
  const terms = checked({ asked });
  const made = paymentHeader(terms, typedData(terms, { now: NOW, nonce: new Uint8Array(32) }), "0x" + "5a".repeat(65));
  const body = Buffer.from(made, "base64").toString("latin1");
  assert.match(body, /^[\x20-\x7e]+$/);
  assert.ok(body.includes("R\\u00e9sultat \\u2713 \\ud83d\\udcb8 \\u007f"));
  assert.deepEqual((JSON.parse(body) as Json)["resource"], asked["resource"]);
});

test("a signature that is not hex is not sent", () => {
  const terms = checked();
  const typed = typedData(terms, { now: NOW, nonce: new Uint8Array(32) });
  for (const signature of [null, undefined, "", "sig", "0x", "0xabc", "0xzz", 5, new Uint8Array(0), [1, 2], "0xab\n"]) {
    assert.throws(() => paymentHeader(terms, typed, signature as string), TypeError, String(signature));
  }
});

test("PAYMENT-REQUIRED is base64 JSON of an object, or nothing", () => {
  assert.deepEqual(decodePaymentRequired(header(required())), required());
  assert.deepEqual(decodePaymentRequired(header(required()).replace(/=+$/, "")), required());
  const bad = [null, undefined, "", 5, "{}", "e30 =", "!!!!", header([required()]), header("x"), header(null), "AAAA",
    Buffer.from([0xff, 0xfe, 0x7b, 0x7d]).toString("base64"), header({ pad: "x".repeat(16 * 1024) })];
  for (const value of bad) {
    assert.equal(reasonOf(() => decodePaymentRequired(value)), "no_payment_terms", String(value).slice(0, 40));
  }
});

// ── The paid answer ──────────────────────────────────────────────────────────

test("SHA-256 here is SHA-256", () => {
  for (const length of [0, 1, 3, 54, 55, 56, 57, 63, 64, 65, 119, 120, 127, 128, 1000, 70_000]) {
    const data = randomBytes(length);
    assert.equal(sha256Hex(data), createHash("sha256").update(data).digest("hex"), `length ${length}`);
  }
  assert.equal(sha256Hex(new TextEncoder().encode("abc")),
    "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
});

function paid(result = RESULT, more: Json = {}): Json {
  return {
    task_id: TASK, task_status: "paid", result, salt: SALT, result_hash: hashOf(result),
    content_type: "text/plain", result_bytes: result.length,
    payment: { transaction: "0x" + "77".repeat(32), amount: "12500000" }, ...more,
  };
}

test("a paid answer is checked against the fingerprint shown before paying", () => {
  assert.equal(checkResult(paid(), FINGERPRINT).result, RESULT);
  assert.equal(checkResult(paid(), FINGERPRINT.toUpperCase()).result, RESULT);
  assert.equal(checkResult(paid("résumé ✓ \u{1F4B8}")).result, "résumé ✓ \u{1F4B8}");
  const bad = [null, {}, [paid()], "paid", { ...paid(), salt: "zz" }, { ...paid(), salt: "abc" }, { ...paid(), salt: null },
    { ...paid(), result: null }, { ...paid(), result_hash: "00".repeat(32) }, { ...paid(), result_hash: FINGERPRINT.toUpperCase() },
    paid("something else")];
  for (const opened of bad) assert.throws(() => checkResult(opened, FINGERPRINT), ResultMismatch, JSON.stringify(opened));
});

// ── payAndOpen ───────────────────────────────────────────────────────────────

type Reply = { status?: number; body?: unknown; headers?: Record<string, string> } | Error;
type Seen = { method: string; path: string; auth: string | null; payment: string | null; redirect: unknown; body: unknown };

function no(status: number, error: string, options: { terms?: unknown; retryAfter?: number } = {}): Reply {
  const headers: Record<string, string> = {};
  if (options.terms !== undefined) headers["PAYMENT-REQUIRED"] = header(options.terms === true ? required() : options.terms);
  if (options.retryAfter !== undefined) headers["Retry-After"] = String(options.retryAfter);
  return { status, body: { detail: { error, message: error } }, headers };
}
const ASK = no(402, "payment_required", { terms: true });
const ASK_SUB = no(402, "payment_required", { terms: required({}, SUB_PATH) });

interface World {
  task?: unknown; record?: unknown; deliveries?: unknown; bounty?: unknown; submissions?: unknown;
  did?: string | undefined; clock?: () => number; other?: Record<string, Reply>;
}

/** The routes payAndOpen reads; `answers` come from the full-result route in order. */
function world(answers: Reply[], w: World = {}) {
  const seen: Seen[] = [];
  const slept: number[] = [];
  const logged: string[] = [];
  const routes: Record<string, Reply> = {
    [`/tasks/${TASK}/deliveries`]: { body: "deliveries" in w ? w.deliveries : [
      { delivery_id: "d2", result_hash: FINGERPRINT }, { delivery_id: "d1", result_hash: "00".repeat(32) }] },
    [`/tasks/${TASK}`]: { body: "task" in w ? w.task : task() },
    [`/agents/${WORKER}/payout-wallet`]: { body: "record" in w ? w.record : walletRecord() },
    [`/markets/bounties/${BOUNTY}`]: { body: "bounty" in w ? w.bounty : bounty() },
    [`/markets/bounties/${BOUNTY}/submissions`]: { body: "submissions" in w ? w.submissions : [
      submission({}, { submission_id: "another", result_hash: "11".repeat(32) }), submission()] },
    ...w.other,
  };
  let next = 0;
  const fetch = (async (input: string, init: RequestInit = {}) => {
    const url = new URL(input);
    const headers = new Headers(init.headers);
    seen.push({
      method: init.method ?? "GET", path: url.pathname + url.search, auth: headers.get("Authorization"),
      payment: headers.get("PAYMENT-SIGNATURE"), redirect: init.redirect,
      body: typeof init.body === "string" ? JSON.parse(init.body) : undefined,
    });
    const full = url.pathname === TASK_PATH || url.pathname === SUB_PATH;
    const reply = full ? answers[Math.min(next++, answers.length - 1)] : routes[url.pathname];
    if (reply === undefined) return new Response(JSON.stringify({ detail: "no such route in this test" }), { status: 404 });
    if (reply instanceof Error) throw reply;
    return new Response(reply.body === undefined ? null : JSON.stringify(reply.body),
      { status: reply.status ?? 200, headers: { Date: new Date(NOW * 1000).toUTCString(), ...reply.headers } });
  }) as typeof globalThis.fetch;
  const http = new HttpClient({ baseUrl: BASE, fetch });
  const api = new PaymentsApi(http, () => ("did" in w ? w.did : BUYER), {
    sleep: async (seconds) => { slept.push(seconds); },
    clock: w.clock ?? (() => 0),
    now: () => NOW + 7,
    log: (line) => logged.push(line),
  });
  const full = () => seen.filter((s) => s.path === TASK_PATH || s.path === SUB_PATH);
  return { api, seen, slept, logged, full, sentHeaders: () => full().map((s) => s.payment) };
}

function signer(signature: unknown = "0x" + "5a".repeat(65)) {
  const got: TypedData[] = [];
  const sign = async (typed: TypedData) => {
    got.push(structuredClone(typed));
    return signature as string;
  };
  return { sign, got };
}

async function thrown<T extends Error>(kind: new (...args: never[]) => T, promise: Promise<unknown>): Promise<T> {
  try {
    await promise;
  } catch (error) {
    assert.ok(error instanceof kind, `expected ${kind.name}, got ${String(error)}`);
    return error;
  }
  assert.fail(`expected ${kind.name}, and nothing was thrown`);
}

function decoded(value: string | null): Json {
  assert.ok(value !== null);
  return JSON.parse(Buffer.from(value, "base64").toString("utf8")) as Json;
}

test("agent.payments is a PaymentsApi", () => {
  const agent = new AgentX({ accessToken: "tok", agentDid: BUYER, baseUrl: BASE });
  assert.ok(agent.payments instanceof PaymentsApi);
});

test("payAndOpen checks, shows, signs once and opens", async () => {
  const w = world([ASK, { body: paid() }]);
  const { sign, got } = signer();
  const shown: string[] = [];
  const opened = await w.api.payAndOpen(TASK, sign, { wallet: PAYER, maxUsd: "20", show: (s) => { shown.push(s); } });
  assert.deepEqual([opened.result, opened.task_status], [RESULT, "paid"]);
  assert.equal(got.length, 1);
  assert.equal(got[0]!.message.to, PAY_TO);
  // AgentX's clock (the Date header), not this machine's: five minutes from there.
  assert.deepEqual([got[0]!.message.validAfter, got[0]!.message.validBefore], [String(NOW - 600), String(NOW + 300)]);
  assert.match(got[0]!.message.nonce, /^0x[0-9a-f]{64}$/);
  const [first, second] = w.sentHeaders();
  assert.equal(first, null);
  const sent = decoded(second!);
  assert.deepEqual(sent["payload"], { signature: "0x" + "5a".repeat(65), authorization: got[0]!.message });
  // The plain sentence went to show() and to the log, before signing.
  assert.deepEqual(shown, [checked().sentence]);
  assert.deepEqual(w.logged, [`AgentX payment: ${shown[0]}`]);
  assert.deepEqual(w.seen.map((s) => s.path), [`/tasks/${TASK}/deliveries`, TASK_PATH, `/tasks/${TASK}`,
    `/agents/${WORKER}/payout-wallet`, TASK_PATH]);
  assert.ok(w.seen.every((s) => s.method === "GET" && s.body === undefined));
});

test("each payment has its own nonce", async () => {
  const nonces = new Set<string>();
  for (let i = 0; i < 20; i++) {
    const { sign, got } = signer();
    await world([ASK, { body: paid() }]).api.payAndOpen(TASK, sign);
    nonces.add(got[0]!.message.nonce);
  }
  assert.equal(nonces.size, 20);
});

test("a clock far from this machine's is not believed", async () => {
  const far = { ...ASK, headers: { ...(ASK as { headers: Record<string, string> }).headers, Date: new Date((NOW + 5000) * 1000).toUTCString() } };
  const { sign, got } = signer();
  await world([far, { body: paid() }]).api.payAndOpen(TASK, sign);
  assert.equal(got[0]!.message.validBefore, String(NOW + 7 + 300));
});

test("A57: payAndOpen does not sign when the records disagree", async () => {
  const cases: [World, string][] = [
    [{ task: task({ pay_to: THIEF }), record: walletRecord(entry({ address: THIEF })) }, "pay_to_differs"],
    [{ record: walletRecord(entry({ address: THIEF })) }, "wallet_not_in_history"],
    [{ task: { ...task(), payment_terms: null } }, "no_public_terms"],
    [{ did: "did:agentx:someone-003" }, "not_your_job"],
  ];
  for (const [change, reason] of cases) {
    const w = world([ASK, { body: paid() }], change);
    const { sign, got } = signer();
    const error = await thrown(PaymentCheckError, w.api.payAndOpen(TASK, sign));
    assert.deepEqual([error.reason, got.length, w.sentHeaders()], [reason, 0, [null]]);
  }
  // A PAYMENT-REQUIRED that pays a thief, with every other record honest.
  const w = world([no(402, "payment_required", { terms: required({ payTo: THIEF }) }), { body: paid() }]);
  const { sign, got } = signer();
  assert.equal((await thrown(PaymentCheckError, w.api.payAndOpen(TASK, sign))).reason, "pay_to_differs");
  assert.deepEqual([got.length, w.sentHeaders()], [0, [null]]);
});

test("a client that does not know its own profile still checks the rest", async () => {
  const w = world([ASK, { body: paid() }], { did: undefined });
  assert.equal((await w.api.payAndOpen(TASK, signer().sign)).result, RESULT);
});

test("show returning false stops before the wallet", async () => {
  for (const show of [() => false, async () => false]) {
    const w = world([ASK]);
    const { sign, got } = signer();
    const error = await thrown(PaymentCheckError, w.api.payAndOpen(TASK, sign, { show }));
    assert.deepEqual([error.reason, got.length, w.full().length], ["declined_by_you", 0, 1]);
  }
  // Only false stops it; a show that returns nothing lets the wallet be asked.
  const w = world([ASK, { body: paid() }]);
  assert.equal((await w.api.payAndOpen(TASK, signer().sign, { show: () => undefined })).result, RESULT);
});

test("a show or a sign that throws sends nothing", async () => {
  const worlds: ReturnType<typeof world>[] = [];
  const fresh = () => { worlds.push(world([ASK, { body: paid() }])); return worlds[worlds.length - 1]!.api; };
  await assert.rejects(fresh().payAndOpen(TASK, signer().sign, { show: () => { throw new Error("closed"); } }), /closed/);
  await assert.rejects(fresh().payAndOpen(TASK, async () => { throw new Error("rejected in the wallet"); }), /rejected/);
  for (const signature of ["", "sig", "0x", null, 5, {}]) {
    await assert.rejects(fresh().payAndOpen(TASK, signer(signature).sign), TypeError);
  }
  await assert.rejects(fresh().payAndOpen(TASK, (async () => undefined) as never), TypeError);
  assert.equal(worlds.length, 9);
  for (const w of worlds) assert.deepEqual(w.sentHeaders(), [null]);
});

test("only the plain request to pay leads to a signature", async () => {
  const answers: Reply[] = [
    no(402, "settlement_pending", { terms: true, retryAfter: 3 }),
    no(402, "earlier_payment_can_still_settle", { terms: true, retryAfter: 200 }),
    no(402, "payment_not_accepted", { terms: true, retryAfter: 200 }),
    no(409, "payment_on_hold"), no(409, "nothing_to_pay_for"), no(403, "not_your_job"),
    no(503, "facilitator_unavailable", { retryAfter: 30 }), no(429, "too_many_refused_payments"),
    no(200 + 2, "payment_required", { terms: true }), no(500, "payment_required", { terms: true }),
    { status: 402, body: { detail: "pay" }, headers: { "PAYMENT-REQUIRED": header(required()) } },
    { status: 402, body: "pay", headers: { "PAYMENT-REQUIRED": header(required()) } },
    { status: 402, headers: { "PAYMENT-REQUIRED": header(required()) } },
    { status: 302, headers: { Location: "http://elsewhere.test/pay", "PAYMENT-REQUIRED": header(required()) } },
  ];
  for (const answer of answers) {
    const w = world([answer]);
    const { sign, got } = signer();
    const error = await thrown(PaymentNotAccepted, w.api.payAndOpen(TASK, sign));
    assert.deepEqual([got.length, w.full().length, error.signed, error.paymentHeader], [0, 1, false, undefined], JSON.stringify(answer));
  }
  const waiting = await thrown(PaymentNotAccepted, world([answers[1]!]).api.terms(TASK));
  assert.deepEqual([waiting.error, waiting.retryAfter, waiting.status], ["earlier_payment_can_still_settle", 200, 402]);
});

test("the plain request to pay with no terms, or unreadable ones, is not signed", async () => {
  for (const answer of [no(402, "payment_required"), { ...no(402, "payment_required"), headers: { "PAYMENT-REQUIRED": "e30" } }]) {
    const w = world([answer as Reply]);
    const { sign, got } = signer();
    const error = await thrown(PaymentCheckError, w.api.payAndOpen(TASK, sign));
    assert.ok(["no_payment_terms", "not_x402_v2"].includes(error.reason));
    assert.equal(got.length, 0);
  }
});

test("a paid task is opened again without signing", async () => {
  const w = world([{ body: paid() }]);
  const { sign, got } = signer();
  assert.equal((await w.api.payAndOpen(TASK, sign)).result, RESULT);
  assert.deepEqual([got.length, w.full().length], [0, 1]);
});

test("nothing in an answer that opens the result is ever given to the wallet", async () => {
  // AgentX answers 200 with a result that checks, and with fields named like the SDK's
  // own unsigned payment. They are part of the answer and nothing more.
  const thief = typedData({ ...checked(), payTo: THIEF } as Terms, { now: NOW, nonce: new Uint8Array(32) });
  const sly = paid(RESULT, {
    typed: thief, terms: { sentence: "Sign in to AgentX.", payTo: THIEF, accepted: {}, resource: {} },
    fingerprint: FINGERPRINT, unsigned: { typed: thief, terms: { sentence: "Sign in." }, fingerprint: FINGERPRINT },
    opened: null,
  });
  const w = world([{ body: sly }]);
  const { sign, got } = signer();
  const shown: string[] = [];
  const opened = await w.api.payAndOpen(TASK, sign, { show: (s) => { shown.push(s); } });
  assert.equal(opened.result, RESULT);
  assert.deepEqual([got.length, shown.length, w.logged.length, w.full().length], [0, 0, 0, 1]);
});

test("the wallet gets a copy: what is sent is what was checked", async () => {
  const w = world([ASK, { body: paid() }]);
  const meddling = async (typed: TypedData) => {
    typed.message.to = THIEF;
    typed.message.value = "1";
    return "0x" + "5a".repeat(65);
  };
  await w.api.payAndOpen(TASK, meddling);
  const authorization = (decoded(w.sentHeaders()[1]!)["payload"] as Json)["authorization"] as Json;
  assert.deepEqual([authorization["to"], authorization["value"]], [PAY_TO, "12500000"]);
});

test("a settling payment is sent again as it is and never signed twice", async () => {
  const w = world([ASK, no(402, "settlement_pending", { terms: true, retryAfter: 4 }),
    no(503, "chain_unavailable", { retryAfter: 10 }), new TypeError("fetch failed"), { body: paid() }]);
  const { sign, got } = signer();
  assert.equal((await w.api.payAndOpen(TASK, sign, { waitSeconds: 600 })).result, RESULT);
  const headers = w.sentHeaders();
  assert.deepEqual([got.length, headers[0], headers.length, new Set(headers.slice(1)).size], [1, null, 5, 1]);
  assert.deepEqual(w.slept, [4, 10, 3]);
});

test("one wait is at least a second and at most thirty", async () => {
  const w = world([ASK, no(402, "settlement_pending", { retryAfter: 900 }),
    { status: 503, body: { detail: { error: "busy", retry_after: 0.2 } } },
    { status: 503, body: { detail: { error: "busy", retry_after: 7 } } }, { body: paid() }]);
  await w.api.payAndOpen(TASK, signer().sign, { waitSeconds: 600 });
  assert.deepEqual(w.slept, [30, 3, 7]);
});

test("waiting ends with the signed payment in hand, and it can be sent again", async () => {
  let tick = -15;
  const pending = no(402, "settlement_pending", { terms: true, retryAfter: 20 });
  const w = world([ASK, pending, pending, { body: paid() }], { clock: () => (tick += 15) });
  const { sign, got } = signer();
  const error = await thrown(PaymentPending, w.api.payAndOpen(TASK, sign, { waitSeconds: 30 }));
  assert.ok(error instanceof PaymentNotAccepted && error.signed);
  assert.equal(error.paymentHeader, w.sentHeaders()[1]);
  assert.match(error.message, /Do not sign another: call agent\.payments\.sendAgain\(taskId, error\.paymentHeader\)/);
  assert.equal((await w.api.sendAgain(TASK, error.paymentHeader!)).result, RESULT);
  assert.deepEqual([got.length, new Set(w.sentHeaders().slice(1)).size], [1, 1]);
  for (const nothing of ["", undefined, null]) {
    await assert.rejects(w.api.sendAgain(TASK, nothing as string), TypeError);
  }
});

test("waitSeconds of nothing sends once", async () => {
  for (const waitSeconds of [0, -5, Number.NaN]) {
    const w = world([ASK, no(402, "settlement_pending", { retryAfter: 2 }), { body: paid() }]);
    await thrown(PaymentPending, w.api.payAndOpen(TASK, signer().sign, { waitSeconds }));
    assert.deepEqual([w.full().length, w.slept.length], [2, 0]);
  }
});

test("a lost answer is not a reason to sign again, and any other failure is thrown", async () => {
  const lost = world([ASK, new TypeError("fetch failed")]);
  const error = await thrown(PaymentPending, lost.api.payAndOpen(TASK, signer().sign, { waitSeconds: 0 }));
  assert.match(error.message, /no answer \(NetworkError\)/);
  assert.ok(error.signed && !(error instanceof NetworkError));
});

test("a refused payment is thrown with its wait and not signed again", async () => {
  const w = world([ASK, no(402, "payment_not_accepted", { terms: true, retryAfter: 240 })]);
  const { sign, got } = signer();
  const error = await thrown(PaymentNotAccepted, w.api.payAndOpen(TASK, sign));
  assert.deepEqual([error.error, error.retryAfter, error.signed, error.status], ["payment_not_accepted", 240, true, 402]);
  assert.equal(error.paymentHeader, w.sentHeaders()[1]);
  assert.ok(!(error instanceof PaymentPending));
  assert.deepEqual([got.length, w.full().length], [1, 2]);
  // A second request to pay after the signature is an answer like any other: no second signature.
  const again = world([ASK, ASK]);
  const second = signer();
  const asked = await thrown(PaymentNotAccepted, again.api.payAndOpen(TASK, second.sign));
  assert.deepEqual([asked.error, asked.signed, second.got.length], ["payment_required", true, 1]);
});

test("a result that is not the one previewed is said so", async () => {
  const w = world([ASK, { body: paid("something else") }]);
  const error = await thrown(ResultMismatch, w.api.payAndOpen(TASK, signer().sign));
  assert.equal(error.opened["result"], "something else");
});

test("no delivery, no payment", async () => {
  for (const deliveries of [[], null, [{ delivery_id: "d1" }], [{ result_hash: "xyz" }], { result_hash: FINGERPRINT }]) {
    const w = world([ASK], { deliveries });
    const error = await thrown(PaymentCheckError, w.api.payAndOpen(TASK, signer().sign));
    assert.deepEqual([error.reason, w.full().length], ["no_delivery", 0]);
  }
});

test("terms reads and checks without signing", async () => {
  const w = world([ASK]);
  const terms = await w.api.terms(TASK, { maxUsd: "12.50" });
  assert.deepEqual([terms.amount, terms.workerHandle], ["12.50", "worker_002"]);
  assert.equal(await w.api.fingerprint(TASK), FINGERPRINT);
  assert.equal((await thrown(PaymentCheckError, w.api.terms(TASK, { maxUsd: "12.49" }))).reason, "over_your_limit");
});

test("an id that could reach another route never leaves", async () => {
  const w = world([ASK]);
  const { sign } = signer();
  // Thrown at the call or as its rejection: either way before any request.
  const never = (call: () => unknown) => assert.rejects(async () => { await call(); }, TypeError);
  for (const id of [".", "..", "a/b", `${TASK}/../other`, "a\\b"]) {
    await never(() => w.api.payAndOpen(id, sign));
    await never(() => w.api.terms(id));
    await never(() => w.api.fingerprint(id));
    await never(() => w.api.sendAgain(id, "x"));
    await never(() => w.api.payAndOpenSubmission(id, SUB, sign));
    await never(() => w.api.payAndOpenSubmission(BOUNTY, id, sign));
    await never(() => w.api.submissionTerms(BOUNTY, id));
    await never(() => w.api.submissionFingerprint(id, SUB));
    await never(() => w.api.sendSubmissionAgain(BOUNTY, id, "x"));
    await never(() => w.api.buyerWalletRecord(id));
  }
  await never(() => w.api.payAndOpen("", sign));
  await never(() => w.api.payAndOpenSubmission(BOUNTY, "", sign));
  assert.equal(w.seen.length, 0);
  // A worker named by the task's record is one segment too.
  const sly = world([ASK], { task: { ...task({ worker_did: "x/../../auth/token" }), executor_agent_did: "x/../../auth/token" } });
  await assert.rejects(sly.api.payAndOpen(TASK, sign), TypeError);
  assert.ok(sly.seen.every((s) => !s.path.includes("auth")));
});

test("the paid request follows no redirect, and carries the token", async () => {
  const seen: { redirect: unknown; auth: string | null }[] = [];
  const fetch = (async (_input: string, init: RequestInit = {}) => {
    seen.push({ redirect: init.redirect, auth: new Headers(init.headers).get("Authorization") });
    return new Response(JSON.stringify({ detail: { error: "payment_on_hold", message: "" } }), { status: 409 });
  }) as typeof globalThis.fetch;
  const agent = new AgentX({ accessToken: "tok", agentDid: BUYER, baseUrl: BASE, fetch });
  await assert.rejects(agent.payments.terms(TASK), PaymentNotAccepted);
  assert.deepEqual(seen, [{ redirect: "manual", auth: "Bearer tok" }]);
});

// ── A bounty's winner ────────────────────────────────────────────────────────

test("payAndOpenSubmission pays the submission's wallet and opens it", async () => {
  const w = world([ASK_SUB, { body: paid() }]);
  const { sign, got } = signer();
  const shown: string[] = [];
  const opened = await w.api.payAndOpenSubmission(BOUNTY, SUB, sign, { wallet: PAYER, maxUsd: "12.50", show: (s) => { shown.push(s); } });
  assert.equal(opened.result, RESULT);
  assert.deepEqual([got.length, got[0]!.message.to, got[0]!.message.value], [1, PAY_TO, "12500000"]);
  assert.deepEqual(shown, [checkedSub().sentence]);
  assert.deepEqual(w.seen.map((s) => s.path), [`/markets/bounties/${BOUNTY}/submissions`, SUB_PATH, `/markets/bounties/${BOUNTY}`,
    `/markets/bounties/${BOUNTY}/submissions`, `/agents/${WORKER}/payout-wallet`, SUB_PATH]);
  assert.equal(await w.api.submissionFingerprint(BOUNTY, SUB), FINGERPRINT);
  assert.equal((await world([ASK_SUB]).api.submissionTerms(BOUNTY, SUB)).bountyId, BOUNTY);
});

test("a submission the list does not show, or shows as another's, is not paid", async () => {
  const cases: [World, Reply, string, number][] = [
    [{ submissions: [] }, ASK_SUB, "no_delivery", 0],
    [{ submissions: { submission_id: SUB } }, ASK_SUB, "no_delivery", 0],
    [{ submissions: [submission({ pay_to: THIEF })] }, ASK_SUB, "wallet_not_in_history", 1],
    [{ submissions: [submission({}, { submitter_did: "did:agentx:someone-003" })] }, ASK_SUB, "worker_differs", 1],
    [{ bounty: bounty({ creator_did: "did:agentx:someone-003" }) }, ASK_SUB, "buyer_differs", 1],
    [{}, ASK, "another_job", 1],                                              // a task's request to pay
  ];
  for (const [change, ask, reason, asks] of cases) {
    const w = world([ask, { body: paid() }], change);
    const { sign, got } = signer();
    const error = await thrown(PaymentCheckError, w.api.payAndOpenSubmission(BOUNTY, SUB, sign));
    assert.deepEqual([error.reason, got.length, w.full().length], [reason, 0, asks], reason);
  }
});

test("a submission's payment that settles late is sent again with sendSubmissionAgain", async () => {
  const w = world([ASK_SUB, no(402, "settlement_pending", { retryAfter: 20 }), { body: paid() }]);
  const { sign, got } = signer();
  const error = await thrown(PaymentPending, w.api.payAndOpenSubmission(BOUNTY, SUB, sign, { waitSeconds: 5 }));
  assert.match(error.message, /sendSubmissionAgain\(bountyId, submissionId, error\.paymentHeader\)/);
  assert.equal((await w.api.sendSubmissionAgain(BOUNTY, SUB, error.paymentHeader!)).result, RESULT);
  assert.equal(got.length, 1);
});

// ── The buyer wallet ─────────────────────────────────────────────────────────

function wallet(challenge: unknown) {
  return world([], { other: {
    "/agents/me/buyer-wallet/challenge": { body: challenge },
    "/agents/me/buyer-wallet": { body: { status: "set" } },
    [`/agents/${BUYER}/buyer-wallet`]: { body: { history: [] } },
  } });
}

test("registerBuyerWalletWith signs AgentX's text and sends the signature", async () => {
  const text = "agentx.social wants you to sign in...\n\nRegister this wallet as the buyer wallet of @b on AgentX. " +
    "This signature moves no funds.\n\nNonce: 1";
  const w = wallet({ text_to_sign: text, moves_funds: false, address: PAYER });
  const texts: string[] = [];
  const answer = await w.api.registerBuyerWalletWith(PAYER, (t) => { texts.push(t); return new Uint8Array(65).fill(0xab); });
  assert.deepEqual(answer, { status: "set" });
  assert.deepEqual(texts, [text]);
  assert.deepEqual(w.seen.map((s) => [s.method, s.path, s.body]), [
    ["POST", "/agents/me/buyer-wallet/challenge", { address: PAYER }],
    ["PUT", "/agents/me/buyer-wallet", { address: PAYER, signature: "0x" + "ab".repeat(65) }],
  ]);
  assert.deepEqual(w.logged, ["AgentX asks the wallet 0x1111…1111 to sign: register it as this profile's buyer wallet. " +
    "This signature moves no funds."]);
});

test("A61: a text that does not say it moves no funds is not signed", async () => {
  const challenges: unknown[] = [
    { text_to_sign: "Approve everything.", moves_funds: false },
    { text_to_sign: "Register. This signature moves no funds.", moves_funds: true },
    { text_to_sign: "Register. This signature moves no funds." },
    { text_to_sign: "Register. This signature moves no funds.", moves_funds: 0 },
    { text_to_sign: ["This signature moves no funds."], moves_funds: false },
    { moves_funds: false }, null, "This signature moves no funds.",
  ];
  for (const challenge of challenges) {
    const w = wallet(challenge);
    const texts: string[] = [];
    const error = await thrown(PaymentCheckError, w.api.registerBuyerWalletWith(PAYER, (t) => { texts.push(t); return "0xab"; }));
    assert.deepEqual([error.reason, texts.length, w.seen.map((s) => s.method)], ["unreadable_prompt", 0, ["POST"]]);
  }
  // A signature that is not one is not sent either.
  const w = wallet({ text_to_sign: "Register. This signature moves no funds.", moves_funds: false });
  await assert.rejects(w.api.registerBuyerWalletWith(PAYER, () => "signed"), TypeError);
  assert.deepEqual(w.seen.map((s) => s.method), ["POST"]);
});

test("the buyer wallet's record, registering by hand and removing", async () => {
  const w = wallet({});
  assert.deepEqual(await w.api.buyerWalletRecord(), { history: [] });
  await w.api.buyerWalletRecord("did:agentx:someone-003").catch(() => undefined);
  await w.api.buyerWalletChallenge(PAYER);
  await w.api.registerBuyerWallet(PAYER, "0xab");
  await w.api.removeBuyerWallet();
  assert.deepEqual(w.seen.map((s) => [s.method, s.path, s.body]), [
    ["GET", `/agents/${BUYER}/buyer-wallet`, undefined],
    ["GET", "/agents/did:agentx:someone-003/buyer-wallet", undefined],
    ["POST", "/agents/me/buyer-wallet/challenge", { address: PAYER }],
    ["PUT", "/agents/me/buyer-wallet", { address: PAYER, signature: "0xab" }],
    ["DELETE", "/agents/me/buyer-wallet", undefined],
  ]);
  assert.throws(() => world([], { did: undefined }).api.buyerWalletRecord(), /pass agentDid/);
});
