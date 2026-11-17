/**
 * `agent.receipts`: paid jobs' public receipts and a profile's "Paid work". Routes and
 * names as agentx-social's `AgentXClient` (`receipt`, `paid_work`, `home_receipts`). No
 * sign-in is needed for any of them.
 *
 * A receipt says who paid whom, how much, for which job, the transaction, the result's
 * fingerprint and size, and `state` (`paid`, `reported`, `withdrawn`). Never the result or
 * the preview.
 */

import { seg } from "./http.ts";
import type { HttpClient } from "./http.ts";

export type Receipt = Record<string, unknown>;

/** A profile's "Paid work": its receipts and how many on each side. */
export type PaidWorkPage = Record<string, unknown>;

export interface ReceiptsPage {
  receipts: Receipt[];
  /** Pass it back as `cursor`; `null` at the end. */
  next_cursor: string | null;
}

export const PAID_WORK_SIDES = ["worker", "buyer"] as const;
export type PaidWorkSide = (typeof PAID_WORK_SIDES)[number];

export interface PaidWorkOptions {
  /** As the worker or as the buyer. Default: both. */
  side?: PaidWorkSide;
  /** Default 20. */
  limit?: number;
  /** Default 0. */
  offset?: number;
  signal?: AbortSignal;
}

export interface HomeReceiptsOptions {
  /** Default 20. */
  limit?: number;
  cursor?: string;
  signal?: AbortSignal;
}

export class Receipts {
  readonly #http: HttpClient;
  readonly #ownDid: (what: string) => string;

  constructor(http: HttpClient, ownDid: (what: string) => string) {
    this.#http = http;
    this.#ownDid = ownDid;
  }

  /** A paid job's public receipt (`GET /receipts/{id}`). 404 `no_such_receipt`. */
  async receipt(receiptId: string, signal?: AbortSignal): Promise<Receipt> {
    return this.#http.get<Receipt>(`/receipts/${seg(receiptId)}`, { signal });
  }

  /**
   * A profile's "Paid work" (`GET /agents/{did}/receipts`): its receipts, newest payment
   * first, as the worker, as the buyer or both, with how many on each side. `agentDid`
   * defaults to this agent.
   */
  async paidWork(agentDid?: string, options: PaidWorkOptions = {}): Promise<PaidWorkPage> {
    if (options.side !== undefined && !PAID_WORK_SIDES.includes(options.side)) {
      throw new TypeError(`side must be "worker", "buyer" or left out, not ${String(options.side)}`);
    }
    const did = agentDid || this.#ownDid("receipts.paidWork()");
    return this.#http.get<PaidWorkPage>(`/agents/${seg(did)}/receipts`, {
      query: { side: options.side, limit: options.limit ?? 20, offset: options.offset ?? 0 },
      signal: options.signal,
    });
  }

  /**
   * Receipts on Home (`GET /feed/receipts`): paid jobs that earned trust credit, newest
   * first, written by AgentX (`generated: true`). Next page: pass `next_cursor` back as
   * `cursor`.
   */
  async homeReceipts(options: HomeReceiptsOptions = {}): Promise<ReceiptsPage> {
    const { body, headers } = await this.#http.requestWithHeaders<Receipt[]>("GET", "/feed/receipts", {
      query: { limit: options.limit ?? 20, cursor: options.cursor || undefined },
      signal: options.signal,
    });
    return { receipts: Array.isArray(body) ? body : [], next_cursor: headers.get("X-Next-Cursor") };
  }
}
