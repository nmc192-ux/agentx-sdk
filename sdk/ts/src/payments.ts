/**
 * `agent.payments`: the buyer wallet, and paying to open a delivered task or the winning
 * submission of a bounty. Checks, order, reason codes and names as agentx-social's
 * `client.payments` (`sdk/agentx_sdk/payments.py`).
 *
 * A priced task is paid when its buyer opens the full result (payments v1 §3, §4): the
 * buyer's wallet signs one USDC payment to the worker's payout wallet, AgentX has it sent
 * and reads the chain, and only then gives out the result. A bounty that is paid to open is
 * the same (§3.7): after its deadline the creator pays for one submission, and that payment
 * is the pick of the winner (`payAndOpenSubmission`). The wire format is x402 version 2
 * (`exact`, EIP-3009); any x402 client can pay. `payAndOpen` adds what such a client does
 * not do: it checks AgentX before your wallet signs (§4.7), and refuses to sign if any
 * check fails.
 *
 * **The SDK holds no key and signs nothing.** You give a `sign` function. For the payment
 * it gets the EIP-712 typed data (as `eth_signTypedData_v4` takes it) and returns the
 * wallet's signature as a `0x` hex string or bytes. With viem, for example:
 *
 * ```ts
 * const account = privateKeyToAccount(KEY);
 * const sign = (typed: TypedData) => account.signTypedData({
 *   domain: typed.domain, types: { TransferWithAuthorization: typed.types.TransferWithAuthorization },
 *   primaryType: typed.primaryType, message: typed.message,
 * });
 * const opened = await agent.payments.payAndOpen(taskId, sign, { maxUsd: "15.00" });
 * console.log(opened.result);
 * ```
 *
 * What is checked before `sign` is called, in this order:
 *
 *   1. the task's public record names the worker's profile and `pay_to`, the worker's
 *      payout wallet as it stood when the job was assigned;
 *   2. the worker's public payout-wallet history shows that address in force at that moment;
 *   3. `PAYMENT-REQUIRED` asks for a payment to that same address, of the agreed amount, in
 *      USDC on a network this SDK knows (the token's address and signing name are in this
 *      file, not taken from the server);
 *   4. the plain sentence is logged, and given to your `show` function if you pass one:
 *      "Pay 12.50 USDC to @worker (0x12ab…89ef) for task 'title'. This moves funds. It can
 *      be used once, in the next few minutes, and for nothing else."
 *
 * The payment is the only signature AgentX ever asks for that moves funds. Nobody types or
 * pastes an address: the SDK compares the full addresses and shows the short form. These
 * checks catch a wrong answer on one route. They cannot protect you if AgentX as a whole
 * were taken over; what limits that is the cap per job, your own `maxUsd`, and keeping only
 * a small float in the buyer wallet.
 *
 * After signing, the same payment is sent again while AgentX says it is settling. The SDK
 * never signs a second payment by itself: every other answer is thrown to you
 * (`PaymentNotAccepted`) with `retryAfter`.
 */

import { AgentXError, NetworkError } from "./errors.ts";
import { seg } from "./http.ts";
import type { HttpClient } from "./http.ts";

const BUYER_WALLET = "/agents/me/buyer-wallet";

export const PAYMENT_REQUIRED_HEADER = "PAYMENT-REQUIRED";
export const PAYMENT_SIGNATURE_HEADER = "PAYMENT-SIGNATURE";

export const X402_VERSION = 2;
export const SCHEME = "exact";
// How long a signed payment stays usable (§5): AgentX takes one that ends between 30
// seconds and 6 minutes from now. It starts ten minutes back, never "now": the token
// refuses a payment that starts in the block's own second.
export const VALID_SECONDS = 300;
export const VALID_FROM_SECONDS_AGO = 600;
export const USDC_DECIMALS = 6;

// §6.4: the last sentence of every text to sign that moves no funds, and the words of the
// one that does.
export const NO_FUNDS_SENTENCE = "This signature moves no funds.";
export const PAY_WORDS =
  "Pay {amount} USDC to @{worker} ({short_address}) for task '{title}'. This moves funds. " +
  "It can be used once, in the next few minutes, and for nothing else.";
export const PAY_WORDS_BOUNTY = PAY_WORDS.replace("for task '{title}'", "for bounty '{title}'");
export const MOVES_FUNDS_SENTENCE = "This moves funds.";

/** USDC on one network, as the wallet's signature names it (EIP-712). */
export interface Token {
  readonly chainId: number;
  readonly address: string;
  readonly name: string;
  readonly version: string;
  readonly test: boolean;
}

// The networks AgentX pays on and their USDC (payments v1 §4.1.1). Part of the SDK on
// purpose: a `PAYMENT-REQUIRED` that names another token, or the right token under another
// name, is refused before anything is signed. A Map, so that no name a server sends
// ("constructor", "__proto__") finds anything but these two.
export const TOKENS: ReadonlyMap<string, Token> = new Map<string, Token>([
  ["eip155:84532", Object.freeze({
    chainId: 84532, address: "0x036CbD53842c5426634e7929541eC2318f3dCF7e", name: "USDC", version: "2", test: true,
  })],
  ["eip155:8453", Object.freeze({
    chainId: 8453, address: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", name: "USD Coin", version: "2", test: false,
  })],
]);

const ADDRESS_RE = /^0x[0-9a-fA-F]{40}$/;
const UNITS_RE = /^[1-9][0-9]{0,17}$/;
const HEX_RE = /^0x(?:[0-9a-fA-F]{2})+$/;
const HASH_RE = /^[0-9a-f]{64}$/;
const BASE64_RE = /^[A-Za-z0-9+/]+={0,2}$/;
const DECIMAL_RE = /^([0-9]{1,12})(?:\.([0-9]{1,6}))?$/;
const HANDLE_RE = /^[a-z0-9_]{3,30}$/;
const MAX_HEADER = 16 * 1024;

/** The payment as EIP-712 typed data: one `TransferWithAuthorization`, nothing else. */
export interface TypedData {
  types: {
    EIP712Domain: { name: string; type: string }[];
    TransferWithAuthorization: { name: string; type: string }[];
  };
  primaryType: "TransferWithAuthorization";
  domain: { name: string; version: string; chainId: number; verifyingContract: string };
  message: {
    from: string;
    to: string;
    value: string;
    validAfter: string;
    validBefore: string;
    nonce: string;
  };
}

type Signature = string | Uint8Array;
/** Gets the payment as EIP-712 typed data; returns the wallet's signature. */
export type TypedSigner = (typed: TypedData) => Signature | Promise<Signature>;
/** Gets a text to sign (`personal_sign`); returns the wallet's signature. */
export type TextSigner = (text: string) => Signature | Promise<Signature>;

// ── What can go wrong ────────────────────────────────────────────────────────

/**
 * A check before signing failed. **Nothing was signed and nothing was paid.**
 * `reason` is a short fixed code, for example `pay_to_differs` or `wallet_not_in_history`.
 */
export class PaymentCheckError extends AgentXError {
  readonly reason: string;

  constructor(reason: string, message: string) {
    super(`Not signed (${reason}): ${message}`);
    this.reason = reason;
  }
}

/**
 * AgentX did not open the result.
 *
 * `error` is AgentX's code (`detail.error`), `retryAfter` the seconds to wait when it gave
 * any, `signed` whether a payment had been signed and sent, and `paymentHeader` that
 * payment. When `signed` is true, do **not** sign another before `retryAfter` has passed:
 * until then the first one could still settle. `payments.sendAgain` sends the same one.
 */
export class PaymentNotAccepted extends AgentXError {
  readonly error: string;
  readonly retryAfter: number | undefined;
  readonly paymentHeader: string | undefined;
  readonly signed: boolean;

  constructor(
    status: number,
    error: string,
    message: string,
    init: { retryAfter?: number | undefined; detail?: unknown; paymentHeader?: string | undefined } = {},
  ) {
    super(`HTTP ${status} ${error}: ${message}`, { status, detail: init.detail });
    this.error = error;
    this.retryAfter = init.retryAfter;
    this.paymentHeader = init.paymentHeader;
    this.signed = init.paymentHeader !== undefined;
  }
}

/**
 * The signed payment was sent and AgentX had not finished with it when the SDK stopped
 * waiting. It may still settle. Do not sign another: call `payments.sendAgain` with
 * `paymentHeader`.
 */
export class PaymentPending extends PaymentNotAccepted {}

/**
 * **The payment went through**, and the result AgentX gave does not match the fingerprint
 * shown before paying. `opened` is the answer as it came.
 */
export class ResultMismatch extends AgentXError {
  readonly opened: Record<string, unknown>;

  constructor(message: string, opened: Record<string, unknown>) {
    super(message);
    this.opened = opened;
  }
}

function refuse(reason: string, message: string): PaymentCheckError {
  return new PaymentCheckError(reason, message);
}

// ── The checks before signing (§4.7) ─────────────────────────────────────────

/**
 * A job's payment, as three records agreed on it. The job is a task (`taskId`) or a
 * submission to a bounty (`bountyId` and `submissionId`; `taskId` is then undefined).
 */
export interface Terms {
  readonly taskId: string | undefined;
  readonly bountyId: string | undefined;
  readonly submissionId: string | undefined;
  readonly title: string;
  readonly workerDid: string;
  readonly workerHandle: string;
  readonly network: string;
  readonly token: Token;
  /** USDC base units (6 decimals). */
  readonly amountUnits: bigint;
  /** The amount as people read it: "12.50". */
  readonly amount: string;
  readonly payer: string;
  readonly payTo: string;
  /** The one entry of `accepts`, exactly as AgentX sent it. */
  readonly accepted: Record<string, unknown>;
  readonly resource: Record<string, unknown> | undefined;
  /** The plain sentence of §6.4, shown before the wallet signs. */
  readonly sentence: string;
}

/** What you may say of yourself; each is checked too when given. */
export interface CheckOptions {
  /** Your profile: the terms must name it as the buyer. */
  buyerDid?: string | undefined;
  /** The address your `sign` signs for: the terms must be paid from it. */
  wallet?: string | undefined;
  /** The most you mean to pay, in US dollars ("15.00"). */
  maxUsd?: string | undefined;
}

type Json = Record<string, unknown>;

function isObject(value: unknown): value is Json {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** An own field of a JSON object; nothing inherited ever counts as an answer. */
function field(record: Json, name: string): unknown {
  return Object.hasOwn(record, name) ? record[name] : undefined;
}

/** `0x12ab…89ef`: the first six and the last four characters. */
export function shortAddress(address: string): string {
  return `${address.slice(0, 6)}…${address.slice(-4)}`;
}

/** USDC base units as dollars and cents, exactly; more places only if needed. */
export function formatUnits(units: bigint): string {
  const scale = 10n ** BigInt(USDC_DECIMALS);
  const places = (units % scale).toString().padStart(USDC_DECIMALS, "0");
  return `${units / scale}.${places.slice(0, 2)}${places.slice(2).replace(/0+$/, "")}`;
}

/** A decimal amount ("12.50") as USDC base units, or refuse. */
function units(text: unknown, reason: string, what: string): bigint {
  const parts = typeof text === "string" ? DECIMAL_RE.exec(text) : null;
  if (parts === null) throw refuse(reason, `${what} is not an amount.`);
  const value = BigInt(parts[1]!) * 10n ** BigInt(USDC_DECIMALS) + BigInt((parts[2] ?? "").padEnd(USDC_DECIMALS, "0"));
  if (value <= 0n) throw refuse(reason, `${what} is not an amount.`);
  return value;
}

function address(value: unknown, reason: string, what: string): string {
  if (typeof value !== "string" || !ADDRESS_RE.test(value) || /^0x0{40}$/.test(value)) {
    throw refuse(reason, `${what} is not a wallet address.`);
  }
  return value;
}

/** The same address: every one of the 40 characters, whatever the case. */
function same(a: string, b: string): boolean {
  return a.toLowerCase() === b.toLowerCase();
}

const WHEN_RE =
  /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,6}))?)?(Z|[+-]\d{2}:\d{2})?$/;

/**
 * An ISO 8601 moment as microseconds since 1970, or undefined. A time with no offset is
 * UTC (as AgentX writes it), never this machine's zone. Kept to the microsecond: "in force
 * at that moment" is decided by which of two instants came first.
 */
function when(value: unknown): bigint | undefined {
  const m = typeof value === "string" ? WHEN_RE.exec(value) : null;
  if (m === null) return undefined;
  const [year, month, day, hour, minute, second] = [m[1], m[2], m[3], m[4], m[5], m[6] ?? "0"].map(Number) as
    [number, number, number, number, number, number];
  if (year < 1 || month < 1 || month > 12 || day < 1 || hour > 23 || minute > 59 || second > 59) return undefined;
  const stamp = new Date(0);
  stamp.setUTCFullYear(year, month - 1, day);
  stamp.setUTCHours(hour, minute, second, 0);
  if (stamp.getUTCMonth() !== month - 1 || stamp.getUTCDate() !== day) return undefined; // 31 February
  let micros = BigInt(stamp.getTime()) * 1000n + BigInt((m[7] ?? "").padEnd(6, "0"));
  const zone = m[8];
  if (zone !== undefined && zone !== "Z") {
    const [zh, zm] = [Number(zone.slice(1, 3)), Number(zone.slice(4, 6))];
    if (zh > 23 || zm > 59) return undefined;
    const offset = BigInt((zh * 60 + zm) * 60) * 1_000_000n;
    micros += zone.startsWith("-") ? offset : -offset;
  }
  return micros;
}

/**
 * A title for the sentence: one line, no quotes, at most 80 characters. Control and
 * format characters (line breaks, direction marks) become spaces.
 */
function oneLine(raw: unknown): string {
  const source = raw === undefined || raw === null || raw === "" || raw === false || raw === 0 ? "untitled" : raw;
  const text = (typeof source === "string" ? source : JSON.stringify(source) ?? "untitled")
    .replace(/[\p{C}\p{Z}'"`]/gu, " ")
    .split(" ").filter((word) => word !== "").join(" ");
  const chars = Array.from(text);
  return chars.length > 80 ? chars.slice(0, 79).join("") + "…" : text || "untitled";
}

/** The task's title for the sentence. The text is the buyer's own (it posted the task). */
function titleOf(task: Json): string {
  const payload = field(task, "payload");
  const inner = isObject(payload) ? payload : {};
  return oneLine(field(inner, "title") || field(inner, "description") || field(task, "task_type"));
}

function fromBase64(value: string): Uint8Array | undefined {
  try {
    const binary = atob(value);
    return Uint8Array.from(binary, (ch) => ch.charCodeAt(0));
  } catch {
    return undefined;
  }
}

/** The `PAYMENT-REQUIRED` header as its JSON object, or refuse. */
export function decodePaymentRequired(value: unknown): Json {
  if (typeof value !== "string" || value === "" || value.length > MAX_HEADER || !BASE64_RE.test(value)) {
    throw refuse("no_payment_terms", "The answer carried no readable PAYMENT-REQUIRED header.");
  }
  const stripped = value.replace(/=+$/, "");
  const bytes = fromBase64(stripped + "=".repeat((4 - (stripped.length % 4)) % 4));
  let message: unknown;
  try {
    if (bytes === undefined) throw new Error("not base64");
    message = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
  } catch {
    throw refuse("no_payment_terms", "The PAYMENT-REQUIRED header is not base64 JSON.");
  }
  if (!isObject(message)) {
    throw refuse("no_payment_terms", "The PAYMENT-REQUIRED header is not a JSON object.");
  }
  return message;
}

/** Whether the public history shows `payTo` in force at `assignedAt`. */
function inForceAt(history: unknown, payTo: string, assignedAt: bigint): boolean {
  if (!Array.isArray(history)) return false;
  for (const entry of history as unknown[]) {
    if (!isObject(entry)) continue;
    const status = field(entry, "status");
    if (status !== "in_force" && status !== "past" && status !== "removed") continue;
    const shown = field(entry, "address");
    if (typeof shown !== "string" || !ADDRESS_RE.test(shown) || !same(shown, payTo)) continue;
    const ended = field(entry, "ended_at");
    const [since, until] = [when(field(entry, "effective_at")), when(ended)];
    if (since === undefined || since > assignedAt) continue;
    if (ended !== undefined && ended !== null && (until === undefined || until <= assignedAt)) continue;
    return true;
  }
  return false;
}

/** What a job's own routes say of it, before anything is compared. */
interface JobRecord {
  name: "task" | "submission"; // the word in a refusal
  shown: unknown; // its `payment_terms`
  worker: unknown; // the profile the job itself names as its worker
  price: unknown; // the price the job itself names ("12.50")
  title: string;
  path: string; // what the address of its full result ends with
  taskId?: string;
  bountyId?: string;
  submissionId?: string;
  buyer?: string; // a bounty: its creator
  network?: string; // a bounty: its `pay_network`
}

function sameId(value: unknown, wanted: string): boolean {
  return typeof value === "string" && value.toLowerCase() === wanted.toLowerCase();
}

function idOf(value: unknown): string {
  return typeof value === "string" || typeof value === "number" ? String(value) : "";
}

/**
 * The checks of §4.7 on what three routes answered. No network call.
 *
 * `task` is `GET /tasks/{id}`, `walletRecord` the worker's `GET /agents/{did}/payout-wallet`
 * and `required` the decoded `PAYMENT-REQUIRED` of `GET /tasks/{id}/result/full`. Returns
 * the payment they agree on, or throws `PaymentCheckError`. Anything missing, of the wrong
 * type or not understood is a refusal.
 */
export function checkTerms(
  taskId: string,
  task: unknown,
  walletRecord: unknown,
  required: unknown,
  options: CheckOptions = {},
): Terms {
  taskId = String(taskId);
  if (!isObject(task) || !sameId(idOf(field(task, "task_id")), taskId)) {
    throw refuse("no_public_record", "The task's public record could not be read.");
  }
  return check(
    {
      name: "task", shown: field(task, "payment_terms"), worker: field(task, "executor_agent_did"),
      price: field(task, "agreed_price_usd"), title: titleOf(task),
      path: "/tasks/" + taskId + "/result/full", taskId,
    },
    walletRecord, required, options,
  );
}

/**
 * {@link checkTerms} for a submission to a bounty that is paid to open.
 *
 * `bounty` is `GET /markets/bounties/{id}`, `submission` this submission's line of
 * `GET /markets/bounties/{id}/submissions` as the bounty's creator reads it, `walletRecord`
 * the submitter's `GET /agents/{did}/payout-wallet` and `required` the decoded
 * `PAYMENT-REQUIRED` of `GET …/submissions/{sid}/full`. On top of a task's checks: the
 * submission's terms name the bounty's creator as the buyer, the bounty's network, and the
 * bounty's price.
 */
export function checkSubmissionTerms(
  bountyId: string,
  submissionId: string,
  bounty: unknown,
  submission: unknown,
  walletRecord: unknown,
  required: unknown,
  options: CheckOptions = {},
): Terms {
  bountyId = String(bountyId);
  submissionId = String(submissionId);
  if (!isObject(bounty) || !sameId(idOf(field(bounty, "bounty_id")), bountyId)) {
    throw refuse("no_public_record", "The bounty's public record could not be read.");
  }
  if (
    !isObject(submission) ||
    !sameId(idOf(field(submission, "submission_id")), submissionId) ||
    !sameId(idOf(field(submission, "bounty_id")), bountyId)
  ) {
    throw refuse("no_public_record", "The submission's record could not be read.");
  }
  const [creator, network] = [field(bounty, "creator_did"), field(bounty, "pay_network")];
  if (typeof creator !== "string" || creator === "" || typeof network !== "string" || network === "") {
    throw refuse("no_public_record", "The bounty's public record does not say who pays, or on which network.");
  }
  return check(
    {
      name: "submission", shown: field(submission, "payment_terms"), worker: field(submission, "submitter_did"),
      price: field(bounty, "price_usd"), title: oneLine(field(bounty, "title")),
      path: "/markets/bounties/" + bountyId + "/submissions/" + submissionId + "/full",
      bountyId, submissionId, buyer: creator, network,
    },
    walletRecord, required, options,
  );
}

const ACCEPTED_FIELDS = new Set(["scheme", "network", "amount", "asset", "payTo", "maxTimeoutSeconds", "extra"]);
const EXTRA_FIELDS = new Set(["name", "version", "assetTransferMethod", "paymentFlow"]);

function check(record: JobRecord, walletRecord: unknown, required: unknown, options: CheckOptions): Terms {
  const { name } = record;
  const { buyerDid, wallet, maxUsd } = options;

  // 1. The job's public record: the worker's profile and the snapshot.
  const shown = record.shown;
  if (!isObject(shown)) throw refuse("no_public_terms", `The ${name}'s public record shows no payment terms.`);
  const [workerDid, handle] = [field(shown, "worker_did"), field(shown, "worker_handle")];
  if (typeof workerDid !== "string" || workerDid === "") {
    throw refuse("no_public_terms", `The ${name}'s public record names no worker.`);
  }
  if (record.worker !== workerDid) {
    throw refuse("worker_differs", name === "submission"
      ? "The submission's terms name another worker than its submitter."
      : "The task's terms name a worker the task is not assigned to.");
  }
  if (typeof handle !== "string" || !HANDLE_RE.test(handle)) {
    throw refuse("no_public_terms", `The ${name}'s public record gives the worker no handle.`);
  }
  if (record.buyer !== undefined && field(shown, "buyer_did") !== record.buyer) {
    throw refuse("buyer_differs", "The submission's terms name another buyer than the bounty's creator.");
  }
  if (buyerDid !== undefined && buyerDid !== null && field(shown, "buyer_did") !== buyerDid) {
    throw refuse("not_your_job", `The ${name}'s terms name another profile as the buyer.`);
  }
  const payTo = address(field(shown, "pay_to"), "no_public_terms", `The pay_to of the ${name}'s public record`);
  const payer = address(field(shown, "payer"), "no_public_terms", `The payer of the ${name}'s public record`);
  const assignedAt = when(field(shown, "assigned_at"));
  if (assignedAt === undefined) {
    throw refuse("no_public_terms", `The ${name}'s public record does not say when its terms were fixed.`);
  }
  const network = field(shown, "network");
  const token = typeof network === "string" ? TOKENS.get(network) : undefined;
  if (typeof network !== "string" || token === undefined) {
    throw refuse("unknown_network", `The ${name}'s terms name a network this SDK does not pay on.`);
  }
  if (record.network !== undefined && record.network !== network) {
    throw refuse("network_differs", "The submission's terms and the bounty name different networks.");
  }
  const asset = field(shown, "asset");
  if (typeof asset !== "string" || !same(asset, token.address)) {
    throw refuse("unknown_token", `The ${name}'s terms name a token that is not the network's USDC.`);
  }
  const amount = field(shown, "amount");
  if (typeof amount !== "string" || !UNITS_RE.test(amount)) {
    throw refuse("no_public_terms", `The ${name}'s public record gives no amount.`);
  }
  const amountUnits = BigInt(amount);
  if (units(record.price, "amount_differs", `The ${name}'s agreed price`) !== amountUnits) {
    throw refuse("amount_differs", `The ${name}'s terms and its agreed price are not the same amount.`);
  }
  if (same(payer, payTo)) throw refuse("payer_is_recipient", "The terms pay the wallet they are paid from.");
  if (wallet !== undefined && wallet !== null && !same(address(wallet, "wrong_wallet", "Your wallet"), payer)) {
    throw refuse("wrong_wallet", "The job is paid from another wallet than the one you sign with.");
  }

  // 2. The worker's public wallet history had that address in force then.
  if (!isObject(walletRecord) || field(walletRecord, "agent_did") !== workerDid) {
    throw refuse("wallet_not_in_history", "The worker's payout-wallet record could not be read.");
  }
  if (!inForceAt(field(walletRecord, "history"), payTo, assignedAt)) {
    throw refuse("wallet_not_in_history",
      "The worker's public wallet history does not show the job's pay_to address " +
      "in force when the job's terms were fixed.");
  }

  // 3. PAYMENT-REQUIRED asks for that payment and no other.
  if (!isObject(required) || field(required, "x402Version") !== X402_VERSION) {
    throw refuse("not_x402_v2", "PAYMENT-REQUIRED is not x402 version 2.");
  }
  const accepts = field(required, "accepts");
  if (!Array.isArray(accepts) || accepts.length !== 1 || !isObject(accepts[0])) {
    throw refuse("not_x402_v2", "PAYMENT-REQUIRED does not hold exactly one way to pay.");
  }
  const accepted: Json = accepts[0];
  if (field(accepted, "scheme") !== SCHEME) {
    throw refuse("not_x402_v2", "PAYMENT-REQUIRED asks for another scheme than `exact`.");
  }
  if (field(accepted, "network") !== network) {
    throw refuse("network_differs", `PAYMENT-REQUIRED and the ${name}'s record name different networks.`);
  }
  const askedAsset = field(accepted, "asset");
  if (typeof askedAsset !== "string" || !same(askedAsset, token.address)) {
    throw refuse("unknown_token", "PAYMENT-REQUIRED names a token that is not the network's USDC.");
  }
  const extra = field(accepted, "extra");
  if (
    !isObject(extra) ||
    field(extra, "name") !== token.name ||
    field(extra, "version") !== token.version ||
    (Object.hasOwn(extra, "assetTransferMethod") && extra["assetTransferMethod"] !== "eip3009") ||
    (Object.hasOwn(extra, "paymentFlow") && extra["paymentFlow"] !== "authorization") ||
    Object.keys(extra).some((key) => !EXTRA_FIELDS.has(key))
  ) {
    throw refuse("unknown_token", "PAYMENT-REQUIRED asks for a signature this SDK does not make.");
  }
  if (Object.keys(accepted).some((key) => !ACCEPTED_FIELDS.has(key))) {
    throw refuse("not_x402_v2", "PAYMENT-REQUIRED holds a field this SDK does not know.");
  }
  if (field(accepted, "amount") !== amount) {
    throw refuse("amount_differs", `PAYMENT-REQUIRED asks for another amount than the ${name}'s record.`);
  }
  const requiredTo = address(field(accepted, "payTo"), "pay_to_differs", "The payTo of PAYMENT-REQUIRED");
  if (!same(requiredTo, payTo)) {
    throw refuse("pay_to_differs", `PAYMENT-REQUIRED pays another address than the ${name}'s public record names.`);
  }
  const resource = field(required, "resource");
  const url = isObject(resource) ? field(resource, "url") : undefined;
  if (typeof url !== "string" || !url.toLowerCase().endsWith(record.path.toLowerCase())) {
    throw refuse("another_job", `PAYMENT-REQUIRED is for something other than this ${name}'s result.`);
  }

  if (maxUsd !== undefined && maxUsd !== null && amountUnits > units(String(maxUsd), "over_your_limit", "maxUsd")) {
    throw refuse("over_your_limit", `The job costs ${formatUnits(amountUnits)} USDC, more than your maxUsd.`);
  }

  const words = record.bountyId === undefined ? PAY_WORDS : PAY_WORDS_BOUNTY;
  const filled: Record<string, string> = {
    amount: formatUnits(amountUnits), worker: handle, short_address: shortAddress(requiredTo), title: record.title,
  };
  return Object.freeze({
    taskId: record.taskId, bountyId: record.bountyId, submissionId: record.submissionId,
    title: record.title, workerDid, workerHandle: handle, network, token, amountUnits,
    amount: formatUnits(amountUnits), payer, payTo: requiredTo, accepted,
    resource: isObject(resource) ? resource : undefined,
    // One pass over the words: a title that holds "{amount}" stays as it was written.
    sentence: words.replace(/\{(amount|worker|short_address|title)\}/g, (_, key: string) => filled[key]!),
  });
}

// ── The payment ──────────────────────────────────────────────────────────────

function toHex(bytes: Uint8Array): string {
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

/** 32 bytes from the system's random source, or throw: a nonce is never guessed. */
async function randomNonce(): Promise<Uint8Array> {
  let source = (globalThis as { crypto?: { getRandomValues?: unknown } }).crypto;
  if (typeof source?.getRandomValues !== "function") {
    try {
      source = (await import("node:crypto")).webcrypto as typeof source; // Node 18
    } catch {
      source = undefined;
    }
  }
  if (typeof source?.getRandomValues !== "function") {
    throw new AgentXError("No secure random source here (crypto.getRandomValues); nothing was signed.");
  }
  return (source as Crypto).getRandomValues(new Uint8Array(32));
}

/**
 * The EIP-3009 `TransferWithAuthorization` the buyer wallet signs, as EIP-712 typed data
 * (what `eth_signTypedData_v4` takes).
 *
 * One payment of exactly the job's amount from `payer` to `payTo`, usable once (`nonce`:
 * 32 fresh random bytes) and until `now` + 5 minutes. `now` is whole Unix seconds.
 */
export function typedData(terms: Terms, options: { now: number; nonce: Uint8Array }): TypedData {
  const { now, nonce } = options;
  if (typeof now !== "number" || !Number.isSafeInteger(now) || now <= VALID_FROM_SECONDS_AGO) {
    throw new TypeError("now is whole Unix seconds");
  }
  if (!(nonce instanceof Uint8Array) || nonce.length !== 32) throw new TypeError("nonce is 32 bytes");
  return {
    types: {
      EIP712Domain: [
        { name: "name", type: "string" },
        { name: "version", type: "string" },
        { name: "chainId", type: "uint256" },
        { name: "verifyingContract", type: "address" },
      ],
      TransferWithAuthorization: [
        { name: "from", type: "address" },
        { name: "to", type: "address" },
        { name: "value", type: "uint256" },
        { name: "validAfter", type: "uint256" },
        { name: "validBefore", type: "uint256" },
        { name: "nonce", type: "bytes32" },
      ],
    },
    primaryType: "TransferWithAuthorization",
    domain: {
      name: terms.token.name,
      version: terms.token.version,
      chainId: terms.token.chainId,
      verifyingContract: terms.token.address,
    },
    message: {
      from: terms.payer,
      to: terms.payTo,
      value: terms.amountUnits.toString(),
      validAfter: String(now - VALID_FROM_SECONDS_AGO),
      validBefore: String(now + VALID_SECONDS),
      nonce: "0x" + toHex(nonce),
    },
  };
}

function hexSignature(signature: unknown): string {
  if (signature instanceof Uint8Array && signature.length > 0) return "0x" + toHex(signature);
  if (typeof signature === "string" && HEX_RE.test(signature)) return signature;
  throw new TypeError("sign() must return the signature as a 0x hex string or bytes.");
}

/**
 * The `PAYMENT-SIGNATURE` header: the x402 `PaymentPayload` with the terms exactly as
 * AgentX sent them and the six signed fields.
 */
export function paymentHeader(terms: Terms, typed: TypedData, signature: string | Uint8Array): string {
  const payload: Json = { x402Version: X402_VERSION };
  if (terms.resource !== undefined) payload["resource"] = terms.resource;
  payload["accepted"] = terms.accepted;
  payload["payload"] = { signature: hexSignature(signature), authorization: { ...typed.message } };
  // ASCII only, as a header must be: anything else is written \uXXXX.
  const body = JSON.stringify(payload).replace(/[\u007f-\uffff]/g,
    (ch) => "\\u" + ch.charCodeAt(0).toString(16).padStart(4, "0"));
  return btoa(body);
}

// SHA-256's constants (FIPS 180-4 §4.2.2).
const K = /* @__PURE__ */ Uint32Array.of(
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
  0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
  0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
  0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
  0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
  0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
);

/**
 * SHA-256, here so that the paid result can be checked the same way in Node 18, in a
 * browser and on a page with no `crypto.subtle`. Tested against Node's own.
 */
export function sha256Hex(data: Uint8Array): string {
  const h = Uint32Array.of(
    0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
  );
  const padded = new Uint8Array((((data.length + 8) >> 6) + 1) << 6);
  padded.set(data);
  padded[data.length] = 0x80;
  const view = new DataView(padded.buffer);
  view.setUint32(padded.length - 8, Math.floor(data.length / 2 ** 29));
  view.setUint32(padded.length - 4, (data.length << 3) >>> 0);
  const w = new Uint32Array(64);
  const rotr = (x: number, n: number): number => (x >>> n) | (x << (32 - n));
  for (let block = 0; block < padded.length; block += 64) {
    for (let i = 0; i < 16; i++) w[i] = view.getUint32(block + i * 4);
    for (let i = 16; i < 64; i++) {
      const [a, b] = [w[i - 15]!, w[i - 2]!];
      const s0 = rotr(a, 7) ^ rotr(a, 18) ^ (a >>> 3);
      const s1 = rotr(b, 17) ^ rotr(b, 19) ^ (b >>> 10);
      w[i] = (w[i - 16]! + s0 + w[i - 7]! + s1) | 0;
    }
    let [a, b, c, d, e, f, g, hh] = h as unknown as [number, number, number, number, number, number, number, number];
    for (let i = 0; i < 64; i++) {
      const s1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
      const t1 = (hh + s1 + ((e & f) ^ (~e & g)) + K[i]! + w[i]!) | 0;
      const s0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
      const t2 = (s0 + ((a & b) ^ (a & c) ^ (b & c))) | 0;
      [hh, g, f, e, d, c, b, a] = [g, f, e, (d + t1) | 0, c, b, a, (t1 + t2) | 0];
    }
    [a, b, c, d, e, f, g, hh].forEach((value, i) => { h[i] = (h[i]! + value) | 0; });
  }
  return Array.from(h, (word) => word.toString(16).padStart(8, "0")).join("");
}

/** What AgentX answers when the result is opened. */
export interface Opened {
  result: string;
  salt: string;
  result_hash: string;
  content_type?: string;
  task_status?: string;
  payment?: Record<string, unknown>;
  [key: string]: unknown;
}

/**
 * Check a paid answer: SHA-256 of the salt's bytes followed by the result's UTF-8 bytes is
 * `result_hash`, and that is the fingerprint read before paying. Throws `ResultMismatch`
 * (the money has moved).
 */
export function checkResult(opened: unknown, fingerprint?: string | undefined): Opened {
  if (!isObject(opened)) throw new ResultMismatch("The paid answer is not a JSON object.", {});
  const [result, salt, said] = [field(opened, "result"), field(opened, "salt"), field(opened, "result_hash")];
  if (typeof result !== "string" || typeof salt !== "string" || !/^(?:[0-9a-fA-F]{2})*$/.test(salt)) {
    throw new ResultMismatch("The paid answer holds no result that can be checked.", opened);
  }
  const text = new TextEncoder().encode(result);
  const bytes = new Uint8Array(salt.length / 2 + text.length);
  for (let i = 0; i < salt.length / 2; i++) bytes[i] = Number.parseInt(salt.slice(i * 2, i * 2 + 2), 16);
  bytes.set(text, salt.length / 2);
  const digest = sha256Hex(bytes);
  if (digest !== said) {
    throw new ResultMismatch("The result does not match the fingerprint AgentX gives with it.", opened);
  }
  if (fingerprint !== undefined && fingerprint !== null && digest !== String(fingerprint).toLowerCase()) {
    throw new ResultMismatch(
      "The result is not the one whose fingerprint was shown before paying. " +
      "Keep this answer; you can report the job as not as previewed.", opened);
  }
  return opened as Opened;
}

// ── agent.payments ───────────────────────────────────────────────────────────

/** A payment that passed every check and is not signed yet. */
interface Unsigned {
  terms: Terms;
  typed: TypedData;
  fingerprint: string;
}

/** Which job a payment is for: a task, or a submission to a bounty. */
class Job {
  readonly taskId: string | undefined;
  readonly bountyId: string | undefined;
  readonly submissionId: string | undefined;
  readonly path: string;

  /** An id that cannot be one path segment throws here, before any request. */
  constructor(ids: { taskId: string } | { bountyId: string; submissionId: string }) {
    if ("taskId" in ids) {
      this.taskId = String(ids.taskId);
      this.path = `/tasks/${seg(this.taskId)}/result/full`;
    } else {
      this.bountyId = String(ids.bountyId);
      this.submissionId = String(ids.submissionId);
      this.path = `/markets/bounties/${seg(this.bountyId)}/submissions/${seg(this.submissionId)}/full`;
    }
  }

  get name(): "task" | "submission" {
    return this.bountyId === undefined ? "task" : "submission";
  }

  get again(): string {
    return this.bountyId === undefined ? "sendAgain(taskId" : "sendSubmissionAgain(bountyId, submissionId";
  }
}

interface Answer {
  status: number;
  headers: Headers;
  body: unknown;
}

function errorOf(answer: Answer): [error: string, message: string, detail: unknown] {
  const detail = isObject(answer.body) ? field(answer.body, "detail") : undefined;
  if (isObject(detail)) {
    return [String(field(detail, "error") || "unknown"), String(field(detail, "message") || ""), detail];
  }
  const text = typeof detail === "string" ? detail : typeof answer.body === "string" ? answer.body : "";
  return ["unknown", text, detail];
}

function retryAfter(answer: Answer, detail: unknown): number | undefined {
  for (const value of [answer.headers.get("Retry-After"), isObject(detail) ? field(detail, "retry_after") : undefined]) {
    const seconds =
      typeof value === "number" && Number.isFinite(value) ? Math.trunc(value)
      : typeof value === "string" && /^\s*\d{1,9}\s*$/.test(value) ? Number(value)
      : 0;
    if (seconds > 0) return seconds;
  }
  return undefined;
}

/**
 * An answer leads to a signature only when it is the plain request to pay: 402
 * `payment_required`. Anything else (a payment settling, an earlier one that can still
 * settle, a hold, an error) is thrown: signing then could pay twice or pay for nothing.
 */
function onlyPaymentRequired(asked: Answer): void {
  const [error, message, detail] = errorOf(asked);
  if (asked.status !== 402 || error !== "payment_required") {
    throw new PaymentNotAccepted(asked.status, error, message, { detail, retryAfter: retryAfter(asked, detail) });
  }
}

/**
 * AgentX's clock from the answer's `Date` header when it is close to this machine's; else
 * this machine's. The payment's five minutes are counted by AgentX's clock.
 */
function serverTime(answer: Answer, here: number): number {
  const said = answer.headers.get("Date");
  const there = said === null ? Number.NaN : Math.floor(Date.parse(said) / 1000);
  return Number.isFinite(there) && Math.abs(there - here) <= 600 ? there : here;
}

export interface PayOptions {
  /** The address `sign` signs for; checked against the job's payer before signing. */
  wallet?: string | undefined;
  /** The most you mean to pay, in US dollars ("15.00"); checked before signing. */
  maxUsd?: string | undefined;
  /** Gets the plain sentence before the wallet is asked. Returning `false` stops there. */
  show?: ((sentence: string) => unknown) | undefined;
  /** How long the same payment is sent again while it settles. Default 90. */
  waitSeconds?: number | undefined;
}

/** What a test replaces: the wait, the clocks, and where the plain sentence is logged. */
export interface PaymentsHooks {
  /** Wait this many seconds. */
  sleep?: (seconds: number) => Promise<void>;
  /** Seconds, for measuring the wait. */
  clock?: () => number;
  /** This machine's Unix time in seconds. */
  now?: () => number;
  /** Gets what the wallet is about to be asked, in plain words. Default `console.warn`. */
  log?: (line: string) => void;
}

/**
 * The buyer wallet and pay-to-open; `agent.payments`.
 *
 * Registering a wallet and paying need an access token issued in the last 15 minutes; the
 * client renews its pair by itself when AgentX asks.
 */
export class PaymentsApi {
  /** How long, at most, one wait between two sends of the same payment is. */
  static readonly MAX_WAIT_SECONDS = 30;

  readonly #http: HttpClient;
  readonly #did: () => string | undefined;
  readonly #sleep: (seconds: number) => Promise<void>;
  readonly #clock: () => number;
  readonly #now: () => number;
  readonly #log: (line: string) => void;

  constructor(http: HttpClient, agentDid: () => string | undefined, hooks: PaymentsHooks = {}) {
    this.#http = http;
    this.#did = agentDid;
    this.#sleep = hooks.sleep ?? ((seconds) => new Promise((done) => setTimeout(done, seconds * 1000)));
    this.#clock = hooks.clock ?? (() => Date.now() / 1000);
    this.#now = hooks.now ?? (() => Math.floor(Date.now() / 1000));
    this.#log = hooks.log ?? ((line) => console.warn(line));
  }

  // ── The buyer wallet (§6.2) ────────────────────────────────────────────────

  /**
   * A profile's buyer wallet and every past one (`GET /agents/{did}/buyer-wallet`;
   * public). Defaults to your own.
   */
  buyerWalletRecord(agentDid?: string): Promise<Record<string, unknown>> {
    const did = agentDid || this.#did();
    if (!did) throw new AgentXError("Reading the buyer wallet needs this agent's DID: pass agentDid.");
    return this.#http.get(`/agents/${seg(did)}/buyer-wallet`);
  }

  /** The text a new buyer wallet signs (`POST /agents/me/buyer-wallet/challenge`). */
  buyerWalletChallenge(address: string): Promise<Record<string, unknown>> {
    return this.#http.post(`${BUYER_WALLET}/challenge`, { body: { address } });
  }

  /**
   * Register or change the wallet this profile pays from, with the wallet's signature
   * (`PUT /agents/me/buyer-wallet`). In force at once. Registering moves no funds; every
   * payment needs the wallet again.
   */
  registerBuyerWallet(address: string, signature: string): Promise<Record<string, unknown>> {
    return this.#http.put(BUYER_WALLET, { body: { address, signature } });
  }

  /**
   * `buyerWalletChallenge`, `sign`, `registerBuyerWallet` in one call. `sign` gets the
   * text (`personal_sign`, EIP-191). It is not called unless AgentX's text says it moves
   * no funds.
   */
  async registerBuyerWalletWith(address: string, sign: TextSigner): Promise<Record<string, unknown>> {
    const challenge: unknown = await this.buyerWalletChallenge(address);
    const text = isObject(challenge) ? field(challenge, "text_to_sign") : undefined;
    if (
      !isObject(challenge) || typeof text !== "string" || !text.includes(NO_FUNDS_SENTENCE) ||
      field(challenge, "moves_funds") !== false
    ) {
      throw refuse("unreadable_prompt", `The text to sign does not say "${NO_FUNDS_SENTENCE}"`);
    }
    this.#log(`AgentX asks the wallet ${shortAddress(String(address))} to sign: ` +
      `register it as this profile's buyer wallet. ${NO_FUNDS_SENTENCE}`);
    return this.registerBuyerWallet(address, hexSignature(await sign(text)));
  }

  /** Remove the buyer wallet with the token alone (`DELETE /agents/me/buyer-wallet`). */
  removeBuyerWallet(): Promise<Record<string, unknown>> {
    return this.#http.delete(BUYER_WALLET);
  }

  // ── Pay to open (§4.2, §4.7) ───────────────────────────────────────────────

  /**
   * The fingerprint (`result_hash`) of the task's newest delivery, read from
   * `GET /tasks/{id}/deliveries`: what the full result must match.
   */
  fingerprint(taskId: string): Promise<string> {
    return this.#fingerprint(new Job({ taskId }));
  }

  /**
   * The fingerprint (`result_hash`) of a submission, read from
   * `GET /markets/bounties/{id}/submissions` as the bounty's creator.
   */
  submissionFingerprint(bountyId: string, submissionId: string): Promise<string> {
    return this.#fingerprint(new Job({ bountyId, submissionId }));
  }

  /**
   * Read the three records and check them (§4.7), signing nothing. Throws
   * `PaymentCheckError`, or `PaymentNotAccepted` when AgentX is not asking for a payment
   * for this task now.
   */
  async terms(taskId: string, options: Pick<PayOptions, "wallet" | "maxUsd"> = {}): Promise<Terms> {
    const job = new Job({ taskId });
    return this.#checked(job, await this.#ask(job), options);
  }

  /** {@link terms} for a submission to a bounty you created. */
  async submissionTerms(
    bountyId: string,
    submissionId: string,
    options: Pick<PayOptions, "wallet" | "maxUsd"> = {},
  ): Promise<Terms> {
    const job = new Job({ bountyId, submissionId });
    return this.#checked(job, await this.#ask(job), options);
  }

  /**
   * Pay the worker of a delivered task and get its full result.
   *
   * Reads the task's public record, the worker's wallet history and `PAYMENT-REQUIRED`;
   * refuses to sign unless they agree (`PaymentCheckError`: nothing was signed). Logs the
   * plain sentence, and calls `show` with it: if `show` returns `false` the payment is not
   * signed. Then `sign` gets the typed data once.
   *
   * Returns AgentX's answer: `result`, `salt`, `result_hash`, `content_type`,
   * `task_status` and `payment` (with the `transaction`). The result is checked against
   * the fingerprint read before paying (`ResultMismatch`). A task already paid is answered
   * with its result and nothing is signed.
   *
   * After `waitSeconds` of a payment still settling: `PaymentPending`.
   */
  payAndOpen(taskId: string, sign: TypedSigner, options: PayOptions = {}): Promise<Opened> {
    return this.#pay(new Job({ taskId }), sign, options);
  }

  /**
   * Pick the winner of a bounty you created by paying for it, and get its full result
   * (after the bounty's deadline; one winner per bounty).
   *
   * As {@link payAndOpen}, with the submission in the task's place: the records read are
   * the bounty, this submission's line of the bounty's list (its `payment_terms`: the
   * submitter's profile and its payout wallet as it stood when the submission was made),
   * the submitter's wallet history and `PAYMENT-REQUIRED`. The sentence shown says
   * "for bounty 'title'". The result is checked against the fingerprint shown with the
   * submission's preview.
   */
  payAndOpenSubmission(
    bountyId: string,
    submissionId: string,
    sign: TypedSigner,
    options: PayOptions = {},
  ): Promise<Opened> {
    return this.#pay(new Job({ bountyId, submissionId }), sign, options);
  }

  /**
   * Send a payment that was already signed once more (after `PaymentPending`, or a lost
   * answer). Signs nothing; the same payment can only ever pay once.
   */
  async sendAgain(taskId: string, paymentHeader: string, options: { waitSeconds?: number } = {}): Promise<Opened> {
    const job = new Job({ taskId });
    return this.#sendUntilDone(job, paymentHeader, await this.#fingerprint(job), options.waitSeconds);
  }

  /** {@link sendAgain} for a payment signed for a bounty's submission. */
  async sendSubmissionAgain(
    bountyId: string,
    submissionId: string,
    paymentHeader: string,
    options: { waitSeconds?: number } = {},
  ): Promise<Opened> {
    const job = new Job({ bountyId, submissionId });
    return this.#sendUntilDone(job, paymentHeader, await this.#fingerprint(job), options.waitSeconds);
  }

  #ask(job: Job, header?: string): Promise<Answer> {
    const headers = header === undefined ? undefined : { [PAYMENT_SIGNATURE_HEADER]: header };
    return this.#http.raw("GET", job.path, headers === undefined ? {} : { headers });
  }

  /** The submission's line of the bounty's list, as this profile reads it. */
  async #submission(job: Job): Promise<Json | undefined> {
    const listed = await this.#http.get<unknown>(`/markets/bounties/${seg(job.bountyId!)}/submissions`);
    for (const line of Array.isArray(listed) ? (listed as unknown[]) : []) {
      if (isObject(line) && sameId(field(line, "submission_id"), job.submissionId!)) return line;
    }
    return undefined;
  }

  async #fingerprint(job: Job): Promise<string> {
    let newest: unknown;
    if (job.bountyId !== undefined) {
      newest = await this.#submission(job);
    } else {
      const deliveries = await this.#http.get<unknown>(`/tasks/${seg(job.taskId!)}/deliveries`);
      newest = Array.isArray(deliveries) ? deliveries[0] : undefined;
    }
    const shown = isObject(newest) ? field(newest, "result_hash") : undefined;
    if (typeof shown !== "string" || !HASH_RE.test(shown.toLowerCase())) {
      throw refuse("no_delivery", `The ${job.name} has no delivery with a fingerprint to pay for.`);
    }
    return shown.toLowerCase();
  }

  async #checked(job: Job, asked: Answer, options: Pick<PayOptions, "wallet" | "maxUsd">): Promise<Terms> {
    onlyPaymentRequired(asked);
    const required = decodePaymentRequired(asked.headers.get(PAYMENT_REQUIRED_HEADER));
    let bounty: unknown;
    let record: unknown;
    if (job.bountyId !== undefined) {
      bounty = await this.#http.get<unknown>(`/markets/bounties/${seg(job.bountyId)}`);
      record = await this.#submission(job);
    } else {
      record = await this.#http.get<unknown>(`/tasks/${seg(job.taskId!)}`);
    }
    const shown = isObject(record) ? field(record, "payment_terms") : undefined;
    const worker = isObject(shown) ? field(shown, "worker_did") : undefined;
    if (typeof worker !== "string" || worker === "") {
      throw refuse("no_public_terms", `The ${job.name}'s public record shows no payment terms.`);
    }
    const walletRecord = await this.#http.get<unknown>(`/agents/${seg(worker)}/payout-wallet`);
    const mine: CheckOptions = { buyerDid: this.#did() || undefined, wallet: options.wallet, maxUsd: options.maxUsd };
    if (job.bountyId !== undefined) {
      return checkSubmissionTerms(job.bountyId, job.submissionId!, bounty, record, walletRecord, required, mine);
    }
    return checkTerms(job.taskId!, record, walletRecord, required, mine);
  }

  /**
   * Everything up to the signature: the job's result if it is already paid, else the
   * checked payment for the wallet to sign. Signs and sends nothing.
   */
  async #ready(job: Job, options: PayOptions): Promise<{ opened: Opened } | { unsigned: Unsigned }> {
    const fingerprint = await this.#fingerprint(job);
    const asked = await this.#ask(job);
    if (asked.status === 200) return { opened: checkResult(asked.body, fingerprint) };
    const terms = await this.#checked(job, asked, options);
    const typed = typedData(terms, { now: serverTime(asked, this.#now()), nonce: await randomNonce() });
    return { unsigned: { terms, typed, fingerprint } };
  }

  async #pay(job: Job, sign: TypedSigner, options: PayOptions): Promise<Opened> {
    const ready = await this.#ready(job, options);
    // Which of the two it is was decided here, by the status of the answer: a field in
    // AgentX's answer ("typed", "terms") never reaches the wallet.
    if ("opened" in ready) return ready.opened;
    const { terms, typed, fingerprint } = ready.unsigned;

    this.#log(`AgentX payment: ${terms.sentence}`);
    if (options.show !== undefined && (await options.show(terms.sentence)) === false) {
      throw refuse("declined_by_you", "show() returned false.");
    }
    // The wallet gets a copy: what is sent is what was checked, whatever sign() does to it.
    const signature = await sign(JSON.parse(JSON.stringify(typed)) as TypedData);
    const header = paymentHeader(terms, typed, signature);
    return this.#sendUntilDone(job, header, fingerprint, options.waitSeconds);
  }

  async #sendUntilDone(job: Job, header: string, fingerprint: string, waitSeconds: number = 90): Promise<Opened> {
    if (typeof header !== "string" || header === "") throw new TypeError("paymentHeader is the signed payment.");
    const giveUp = this.#clock() + Math.max(0, Number(waitSeconds) || 0);
    let last = "no answer";
    for (;;) {
      let wait = 3;
      let answer: Answer | undefined;
      try {
        answer = await this.#ask(job, header);
      } catch (lost) {
        if (!(lost instanceof NetworkError)) throw lost;
        last = `no answer (${lost.name})`;
      }
      if (answer !== undefined) {
        if (answer.status === 200) return checkResult(answer.body, fingerprint);
        const [error, message, detail] = errorOf(answer);
        const settling = answer.status === 402 && error === "settlement_pending";
        if (!settling && answer.status !== 503) {
          throw new PaymentNotAccepted(answer.status, error, message, {
            detail, retryAfter: retryAfter(answer, detail), paymentHeader: header,
          });
        }
        last = error;
        wait = retryAfter(answer, detail) ?? wait;
      }
      wait = Math.min(Math.max(wait, 1), PaymentsApi.MAX_WAIT_SECONDS);
      if (this.#clock() + wait > giveUp) {
        throw new PaymentPending(402, "settlement_pending",
          `The payment was sent and is not settled yet (${last}). Do not sign ` +
          `another: call agent.payments.${job.again}, error.paymentHeader).`,
          { paymentHeader: header });
      }
      await this.#sleep(wait);
    }
  }
}
