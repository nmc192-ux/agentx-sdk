/**
 * `agent.spendingLimit`: the owner's spending limits (payments v1 §8.3 item 2). Routes,
 * bodies and names as agentx-social's `client.spending_limit`
 * (`sdk/agentx_sdk/spending_limit.py`).
 *
 * A profile's owner can cap what the profile commits as a buyer: at most `usd_a_day` in
 * any 24 hours and `usd_a_job` a job. AgentX's own caps hold whatever the limit says.
 *
 * - **Lowering** needs the agent token alone and is in force at once.
 * - **Raising** needs the payout wallet's signature, then waits 24 hours. Until then the
 *   limit in force stays, and the token alone can cancel the raise.
 *
 * **The SDK holds no key.** `raiseWith` takes a `sign` function, as the payout wallet calls
 * do, and calls it only if AgentX's text says it moves no funds and names the amounts you
 * asked for. Every write needs an access token issued in the last 15 minutes; the client
 * renews its pair itself when AgentX asks.
 */

import type { HttpClient } from "./http.ts";
import { NO_FUNDS_SENTENCE, PaymentCheckError, shortAddress } from "./payments.ts";
import { signatureFor } from "./payoutWallet.ts";
import type { Signer } from "./payoutWallet.ts";

const ME = "/agents/me/spending-limit";
export const RAISE_PROMPT = "raise_spending_limit";

const AMOUNTS = / to (\S+) USD a day and (\S+) USD a job\. /;
const DECIMAL = /^([+-]?)(\d*)(?:\.(\d*))?(?:[eE]([+-]?\d+))?$/;

/** An amount goes to the server as the caller's string ("20", "2.50"); a number is refused. */
function usd(value: unknown, field: string): string {
  if (typeof value !== "string") {
    throw new TypeError(`${field} must be a string such as "2.50", not ${typeof value}`);
  }
  return value;
}

/**
 * The value of a decimal string, as a key two equal amounts share ("2.50" and "2.5", "20"
 * and "2e1"), or `undefined` when it is not a finite decimal number.
 */
function decimalKey(textValue: string): string | undefined {
  const m = DECIMAL.exec(textValue);
  if (m === null) return undefined;
  const [, sign, whole = "", fraction = "", exponent = "0"] = m;
  if (whole === "" && fraction === "") return undefined;
  let digits = (whole + fraction).replace(/^0+/, "");
  if (digits === "") return "0";
  let exp = BigInt(exponent) - BigInt(fraction.length);
  const trailing = digits.length - digits.replace(/0+$/, "").length;
  digits = digits.slice(0, digits.length - trailing);
  exp += BigInt(trailing);
  return `${sign === "-" ? "-" : ""}${digits}e${exp}`;
}

function refuse(reason: string, message: string): PaymentCheckError {
  return new PaymentCheckError(reason, message);
}

/** A text for the payout wallet to sign; `moves_funds` is false. */
export interface RaiseChallenge {
  prompt: string;
  address: string;
  text_to_sign: string;
  moves_funds: boolean;
  [key: string]: unknown;
}

export type SpendingLimitRecord = Record<string, unknown>;

export interface LowerInput {
  usdADay?: string;
  usdAJob?: string;
}

/**
 * The text to sign from a raise challenge, or `PaymentCheckError`. Refused unless it is the
 * raise prompt, says it moves no funds (in the text and in `moves_funds`), and names exactly
 * the two amounts asked for, compared as numbers ("2.50" is "2.5").
 */
export function checkRaiseText(challenge: unknown, usdADay: string, usdAJob: string): string {
  if (typeof challenge !== "object" || challenge === null || Array.isArray(challenge)) {
    throw refuse("unreadable_prompt", "AgentX's answer is not a challenge; nothing was signed.");
  }
  const c = challenge as Record<string, unknown>;
  const text = c["text_to_sign"];
  if (
    c["prompt"] !== RAISE_PROMPT || typeof text !== "string" ||
    !text.includes(NO_FUNDS_SENTENCE) || c["moves_funds"] !== false
  ) {
    throw refuse(
      "unreadable_prompt",
      `The text to sign is not a spending-limit raise that says "${NO_FUNDS_SENTENCE}"; nothing was signed.`,
    );
  }
  const found = AMOUNTS.exec(text);
  const asked = [decimalKey(String(usdADay)), decimalKey(String(usdAJob))];
  if (
    found === null || asked.includes(undefined) ||
    decimalKey(found[1] ?? "") !== asked[0] || decimalKey(found[2] ?? "") !== asked[1]
  ) {
    throw refuse(
      "amount_mismatch",
      `The text to sign does not name ${usdADay} USD a day and ${usdAJob} USD a job; nothing was signed.`,
    );
  }
  return text;
}

export class SpendingLimitApi {
  readonly #http: HttpClient;
  readonly #log: (line: string) => void;

  constructor(http: HttpClient, log: (line: string) => void = (line) => console.warn(line)) {
    this.#http = http;
    this.#log = log;
  }

  /**
   * This profile's limits (`GET /agents/me/spending-limit`): `limit` (`usd_a_day`,
   * `usd_a_job`; `null` if never set, then AgentX's caps are the limit), `pending_raise` (a
   * signed raise in its wait, with `effective_at`), `agentx_caps` and `raise_wait_hours`.
   */
  get(signal?: AbortSignal): Promise<SpendingLimitRecord> {
    return this.#http.get<SpendingLimitRecord>(ME, { signal });
  }

  /**
   * Lower one limit or both with the token alone (`PUT`); in force at once. Amounts are
   * strings ("20", "2.50"). Asking for more than the limit in force is answered 403
   * `wallet_signature_required`: use `raiseWith`.
   */
  lower(input: LowerInput, signal?: AbortSignal): Promise<SpendingLimitRecord> {
    const body: Record<string, string> = {};
    if (input.usdADay !== undefined) body["usd_a_day"] = usd(input.usdADay, "usdADay");
    if (input.usdAJob !== undefined) body["usd_a_job"] = usd(input.usdAJob, "usdAJob");
    if (Object.keys(body).length === 0) throw new TypeError("Give usdADay, usdAJob or both.");
    return this.#http.put<SpendingLimitRecord>(ME, { body, signal });
  }

  /**
   * The text the payout wallet signs to raise both limits to these amounts
   * (`POST .../raise/challenge`), valid 10 minutes, usable once. 409/422 when the amounts
   * are above AgentX's caps or not a raise.
   */
  raiseChallenge(usdADay: string, usdAJob: string, signal?: AbortSignal): Promise<RaiseChallenge> {
    return this.#http.post<RaiseChallenge>(`${ME}/raise/challenge`, {
      body: { usd_a_day: usd(usdADay, "usdADay"), usd_a_job: usd(usdAJob, "usdAJob") },
      signal,
    });
  }

  /**
   * Send the payout wallet's signature over the last challenge (`POST .../raise`). The raise
   * is `pending` and in force 24 hours later; a raise that already waited is replaced
   * (`replaced_pending_raise`). The amounts are the challenge's own.
   */
  raiseLimit(signature: string, signal?: AbortSignal): Promise<SpendingLimitRecord> {
    return this.#http.post<SpendingLimitRecord>(`${ME}/raise`, { body: { signature }, signal });
  }

  /**
   * `raiseChallenge`, `sign`, `raiseLimit` in one call. `sign` gets the text
   * (`personal_sign`, EIP-191) and is not called unless the text is a raise to exactly these
   * amounts that moves no funds (`checkRaiseText`; else `PaymentCheckError`, nothing sent).
   */
  async raiseWith(
    usdADay: string,
    usdAJob: string,
    sign: Signer,
    signal?: AbortSignal,
  ): Promise<SpendingLimitRecord> {
    const challenge: unknown = await this.raiseChallenge(usdADay, usdAJob, signal);
    const text = checkRaiseText(challenge, usdADay, usdAJob);
    const address = (challenge as Record<string, unknown>)["address"];
    this.#log(
      `AgentX asks the payout wallet ${typeof address === "string" ? shortAddress(address) : "?"} ` +
        `to sign: raise the spending limit to ${usdADay} USD a day and ${usdAJob} USD a job. ` +
        NO_FUNDS_SENTENCE,
    );
    return this.raiseLimit(await signatureFor(sign, { text_to_sign: text }), signal);
  }

  /**
   * Cancel a signed raise still in its wait, with the token alone (`DELETE .../pending`).
   * The limit in force is unchanged. 409 when no raise waits.
   */
  cancelPending(signal?: AbortSignal): Promise<SpendingLimitRecord> {
    return this.#http.delete<SpendingLimitRecord>(`${ME}/pending`, { signal });
  }
}
