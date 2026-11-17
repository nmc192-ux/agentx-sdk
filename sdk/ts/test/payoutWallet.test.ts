// agent.payoutWallet, logoutAll and the wallet-signed login reset, with fetch stubbed
// (payments v1 §7.3 to §7.8, plan step W1-8c). The SDK signs nothing: every signature
// comes from the caller's sign function, over the exact text the server wrote.
// Request-for-request parity with agentx-social: sdk/tests/test_ts_parity_payout_wallet.py.
// Run: npm test in sdk/ts   (Node >= 22.18 strips the types)
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { AgentX } from "../src/agentx.ts";
import { AgentXError, AuthError, ConflictError } from "../src/errors.ts";
import { PayoutWalletApi, hasPendingNotice } from "../src/index.ts";

const BASE = "http://api.test";
const DID = "did:agentx:payee-001";
const WALLET = "0x1111111111111111111111111111111111111111";
const NEW = "0x2222222222222222222222222222222222222222";
const PENDING = { "AgentX-Notice": "payout-wallet-pending" };

type Seen = { method: string; path: string; auth: string | null; body: unknown };
type Reply = { status?: number; body?: unknown; headers?: Record<string, string> };

function challenge(text: string, address = WALLET) {
  return {
    prompt: "x", signed_by: "x", address, text_to_sign: text,
    how_to_sign: "personal_sign (EIP-191) over text_to_sign, exactly as given",
    expires_at: "2026-11-14T00:10:00+00:00", moves_funds: false,
  };
}

/** Replies in turn (the last one repeats). Records every request. */
function client(replies: Reply[] = [{ body: {} }], extra: Record<string, unknown> = {}) {
  const seen: Seen[] = [];
  const warnings: string[] = [];
  let i = 0;
  const fetch = (async (input: string, init: RequestInit = {}) => {
    const url = new URL(input);
    seen.push({
      method: init.method ?? "GET",
      path: url.pathname + url.search,
      auth: new Headers(init.headers).get("Authorization"),
      body:
        init.body === undefined
          ? undefined
          : typeof init.body === "string" && init.body.startsWith("{")
            ? JSON.parse(init.body)
            : String(init.body),
    });
    const reply = replies[Math.min(i++, replies.length - 1)]!;
    const text = reply.body === undefined ? null : JSON.stringify(reply.body);
    return new Response(text, { status: reply.status ?? 200, headers: reply.headers });
  }) as typeof globalThis.fetch;
  const agent = new AgentX({
    accessToken: "tok",
    refreshToken: "ref",
    agentDid: DID,
    baseUrl: BASE,
    fetch,
    onPayoutWalletPending: (m) => warnings.push(m),
    ...extra,
  });
  return { agent, seen, warnings, last: () => seen[seen.length - 1]! };
}

function recorder(signature: string | Uint8Array = "0xsig") {
  const texts: string[] = [];
  const sign = async (text: string) => {
    texts.push(text);
    return signature;
  };
  return { sign, texts };
}

test("payoutWallet is a PayoutWalletApi", () => {
  assert.ok(client().agent.payoutWallet instanceof PayoutWalletApi);
});

// ── Each route ───────────────────────────────────────────────────────────────

test("record defaults to this agent and takes another DID", async () => {
  const { agent, seen } = client([{ body: { in_force: null, pending: null, history: [] } }]);
  await agent.payoutWallet.record();
  await agent.payoutWallet.record("did:agentx:other");
  assert.deepEqual(seen.map((s) => [s.method, s.path]), [
    ["GET", `/agents/${DID}/payout-wallet`],
    ["GET", "/agents/did:agentx:other/payout-wallet"],
  ]);
});

test("record refuses a DID that would pick another route", async () => {
  const { agent, seen } = client();
  assert.throws(() => agent.payoutWallet.record("did:agentx:x/../me"), TypeError);
  assert.equal(seen.length, 0);
});

test("setWith signs the server's text exactly and sends address and signature", async () => {
  const text = "agentx.social wants you to sign...\n\nline two ";
  const { agent, seen } = client([{ body: challenge(text) }, { body: { status: "pending" } }]);
  const { sign, texts } = recorder("0xabc");
  await agent.payoutWallet.setWith(WALLET, sign);
  assert.deepEqual(texts, [text]);
  assert.deepEqual(seen.map((s) => [s.method, s.path, s.body]), [
    ["POST", "/agents/me/payout-wallet/challenge", { address: WALLET }],
    ["PUT", "/agents/me/payout-wallet", { address: WALLET, signature: "0xabc" }],
  ]);
  assert.equal(seen[1]!.auth, "Bearer tok");
});

test("a bytes signature becomes 0x hex", async () => {
  const { agent, last } = client([{ body: challenge("t") }, { body: {} }]);
  await agent.payoutWallet.setWith(WALLET, recorder(new Uint8Array([0xde, 0xad, 0x01])).sign);
  assert.deepEqual(last().body, { address: WALLET, signature: "0xdead01" });
});

test("no text to sign: nothing is signed or sent", async () => {
  const { agent, seen } = client([{ body: { ...challenge("t"), text_to_sign: "" } }]);
  const { sign, texts } = recorder();
  await assert.rejects(agent.payoutWallet.setWith(WALLET, sign), AgentXError);
  assert.equal(texts.length, 0);
  assert.equal(seen.length, 1);
});

test("an empty signature is refused before sending", async () => {
  const { agent, seen } = client([{ body: challenge("t") }]);
  await assert.rejects(agent.payoutWallet.setWith(WALLET, recorder("").sign), AgentXError);
  await assert.rejects(
    agent.payoutWallet.setWith(WALLET, recorder(new Uint8Array()).sign),
    AgentXError,
  );
  assert.ok(seen.every((s) => s.path.endsWith("/challenge")));
});

test("cancelPending is a DELETE with the token", async () => {
  const { agent, last } = client();
  await agent.payoutWallet.cancelPending();
  assert.equal(last().method, "DELETE");
  assert.equal(last().path, "/agents/me/payout-wallet/pending");
  assert.equal(last().auth, "Bearer tok");
});

test("changeWith has each wallet sign its own text", async () => {
  const { agent, seen } = client([
    { body: { current_wallet: challenge("old text"), new_wallet: challenge("new text", NEW) } },
    { body: {} },
  ]);
  const current = recorder("0xcur");
  const next = recorder("0xnew");
  await agent.payoutWallet.changeWith(NEW, current.sign, next.sign);
  assert.deepEqual(current.texts, ["old text"]);
  assert.deepEqual(next.texts, ["new text"]);
  assert.deepEqual(seen.map((s) => [s.method, s.path, s.body]), [
    ["POST", "/agents/me/payout-wallet/change/challenge", { new_address: NEW }],
    ["POST", "/agents/me/payout-wallet/change",
      { new_address: NEW, current_signature: "0xcur", new_signature: "0xnew" }],
  ]);
});

test("cancelChangeWith and removeWith send only the signature", async () => {
  const { agent, seen } = client([{ body: challenge("c") }, { body: {} }, { body: challenge("r") }, { body: {} }]);
  await agent.payoutWallet.cancelChangeWith(recorder("0x1").sign);
  const { agent: a2, seen: s2 } = client([{ body: challenge("r") }, { body: {} }]);
  await a2.payoutWallet.removeWith(recorder("0x2").sign);
  assert.deepEqual(seen.map((s) => [s.method, s.path, s.body]), [
    ["POST", "/agents/me/payout-wallet/change/cancel/challenge", {}],
    ["POST", "/agents/me/payout-wallet/change/cancel", { signature: "0x1" }],
  ]);
  assert.deepEqual(s2.map((s) => [s.method, s.path, s.body]), [
    ["POST", "/agents/me/payout-wallet/remove/challenge", {}],
    ["POST", "/agents/me/payout-wallet/remove", { signature: "0x2" }],
  ]);
});

test("a refusal reaches the caller with the server's detail", async () => {
  const { agent } = client([{ status: 409, body: { detail: { reason: "wallet_pending", message: "wait" } } }]);
  await assert.rejects(agent.payoutWallet.set(WALLET, "0xsig"), ConflictError);
});

test("token_not_fresh renews the pair and sends once more", async () => {
  const { agent, seen } = client([
    { status: 401, body: { detail: "token_not_fresh" } },
    { body: { access_token: "fresh", refresh_token: "ref2", expires_in: 900 } },
    { body: { status: "pending" } },
  ]);
  await agent.payoutWallet.set(WALLET, "0xsig");
  assert.deepEqual(seen.map((s) => [s.method, s.path, s.auth]), [
    ["PUT", "/agents/me/payout-wallet", "Bearer tok"],
    ["POST", "/auth/token", null],
    ["PUT", "/agents/me/payout-wallet", "Bearer fresh"],
  ]);
});

// ── The pending notice ───────────────────────────────────────────────────────

test("hasPendingNotice reads one value among several", () => {
  assert.equal(hasPendingNotice(new Headers({ "AgentX-Notice": "other, payout-wallet-pending" })), true);
  assert.equal(hasPendingNotice(new Headers({ "AgentX-Notice": "payout-wallet-pending-x" })), false);
  assert.equal(hasPendingNotice(new Headers()), false);
});

test("the pending notice warns once while it lasts", async () => {
  const { agent, warnings } = client([{ body: {}, headers: PENDING }]);
  await agent.payoutWallet.record();
  await agent.payoutWallet.record();
  assert.equal(warnings.length, 1);
  assert.match(warnings[0]!, /cancelPending\(\)/);
  assert.equal(agent.payoutWalletPending, true);
});

test("the pending notice warns again after it went away", async () => {
  const { agent, warnings } = client([
    { body: {}, headers: PENDING },
    { body: {} },
    { body: {}, headers: PENDING },
  ]);
  await agent.payoutWallet.record();
  await agent.payoutWallet.record();
  assert.equal(agent.payoutWalletPending, false);
  await agent.payoutWallet.record();
  assert.equal(warnings.length, 2);
});

test("the pending notice on an error answer still warns", async () => {
  const { agent, warnings } = client([{ status: 409, body: { detail: "x" }, headers: PENDING }]);
  await assert.rejects(agent.payoutWallet.cancelPending(), ConflictError);
  assert.equal(warnings.length, 1);
});

test("no notice, no warning", async () => {
  const { agent, warnings } = client();
  await agent.payoutWallet.record();
  assert.equal(warnings.length, 0);
  assert.equal(agent.payoutWalletPending, false);
});

test("by default the warning is a Node process warning", async () => {
  const fetch = (async () =>
    new Response("{}", { status: 200, headers: PENDING })) as typeof globalThis.fetch;
  const agent = new AgentX({ accessToken: "tok", agentDid: DID, baseUrl: BASE, fetch });
  const seen: Error[] = [];
  const listener = (w: Error) => seen.push(w);
  process.on("warning", listener);
  try {
    await agent.payoutWallet.record();
    await new Promise((r) => setImmediate(r));
  } finally {
    process.off("warning", listener);
  }
  assert.equal(seen.length, 1);
  assert.equal(seen[0]!.name, "PayoutWalletPendingWarning");
});

// ── Sign out everywhere and the login reset ──────────────────────────────────

test("logoutAll posts with the token", async () => {
  const { agent, last } = client([{ body: { signed_out: true, agent_did: DID } }]);
  const out = await agent.logoutAll();
  assert.equal(out.signed_out, true);
  assert.deepEqual([last().method, last().path, last().auth], ["POST", "/auth/logout-all", "Bearer tok"]);
});

test("logoutAll without a settled wallet is a ConflictError", async () => {
  const { agent } = client([{ status: 409, body: { detail: { reason: "no_recovery_wallet", message: "no" } } }]);
  await assert.rejects(agent.logoutAll(), ConflictError);
});

test("the reset sends no token and holds and saves the new pair", async () => {
  const dir = await mkdtemp(join(tmpdir(), "agentx-reset-"));
  const identityPath = join(dir, "identity.json");
  const pairs: unknown[] = [];
  const { agent, seen } = client(
    [
      { body: challenge("reset text") },
      { body: { access_token: "new-acc", refresh_token: "new-ref", token_type: "bearer",
                expires_in: 900, agent_did: DID } },
      { body: {} },
    ],
    { identityPath, onRefresh: (p: unknown) => pairs.push(p) },
  );
  const { sign, texts } = recorder("0xreset");
  const out = await agent.resetLoginWith(sign);
  assert.equal(out.access_token, "new-acc");
  assert.deepEqual(texts, ["reset text"]);
  assert.deepEqual(seen.slice(0, 2).map((s) => [s.method, s.path, s.auth, s.body]), [
    ["POST", "/auth/wallet-reset/challenge", null, { agent: DID }],
    ["POST", "/auth/wallet-reset", null, { agent: DID, signature: "0xreset" }],
  ]);
  assert.equal(agent.tokens.snapshot().accessToken, "new-acc");
  assert.equal(agent.tokens.snapshot().refreshToken, "new-ref");
  await agent.payoutWallet.record();
  assert.equal(seen[2]!.auth, "Bearer new-acc");
  const saved = await readFile(identityPath, "utf8");
  assert.match(saved, /new-acc/);
  assert.match(saved, /new-ref/);
  assert.equal(pairs.length, 1);
});

test("the reset never tries to refresh a cancelled pair", async () => {
  const { agent, seen } = client([{ status: 401, body: { detail: "nope" } }]);
  await assert.rejects(agent.resetLogin("0xsig"), AuthError);
  assert.equal(seen.length, 1);
  assert.equal(seen[0]!.auth, null);
});

test("a refused reset keeps the old pair", async () => {
  const { agent } = client([{ status: 403, body: { detail: "signature not accepted" } }]);
  await assert.rejects(agent.resetLogin("0xbad"));
  assert.equal(agent.tokens.snapshot().accessToken, "tok");
});

test("a reset without a known DID says how", async () => {
  const fetch = (async () => new Response("{}")) as typeof globalThis.fetch;
  const agent = new AgentX({ accessToken: "not-a-jwt", baseUrl: BASE, fetch });
  assert.throws(() => agent.resetLoginChallenge(), /agentDid/);
});

test("a reset for another profile returns the pair and does not hold it", async () => {
  const { agent, seen } = client([
    { body: { access_token: "theirs", refresh_token: "r", expires_in: 900, agent_did: "did:agentx:other" } },
  ]);
  const out = await agent.resetLogin("0xsig", "did:agentx:other");
  assert.equal(out.access_token, "theirs");
  assert.deepEqual(seen[0]!.body, { agent: "did:agentx:other", signature: "0xsig" });
  assert.equal(agent.tokens.snapshot().accessToken, "tok");
});

test("a reset answer with the pending notice warns", async () => {
  const { agent, warnings } = client([
    { body: { access_token: "a", refresh_token: "r", expires_in: 900, agent_did: DID }, headers: PENDING },
  ]);
  await agent.resetLogin("0xsig");
  assert.equal(warnings.length, 1);
});
