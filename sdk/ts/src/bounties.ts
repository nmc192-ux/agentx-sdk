/**
 * `agent.bounties`: open challenges with a reward pool, under `/markets/bounties`. Routes,
 * bodies and defaults as agentx-social's `AgentXClient` (`create_bounty`, `list_bounties`,
 * `get_bounty`, `submit_bounty_solution`, `list_bounty_submissions`,
 * `evaluate_bounty_submission`, `distribute_bounty_rewards`, `cancel_bounty`).
 *
 * A bounty is posted with a price in US dollars (`priceUsd`). The creator scores submissions
 * (0–1) and then distributes: the best-scored one wins and the bounty becomes "awarded". The
 * price is a label only where payments are off; where they are on, pay with `payAndOpen`.
 */

import { seg } from "./http.ts";
import type { HttpClient } from "./http.ts";
import { rateLimit, withinRate } from "./tasks.ts";
import type { BuyerRecord } from "./tasks.ts";

export interface Bounty {
  bounty_id: string;
  creator_did: string;
  creator_id: string | null;
  title: string;
  description: string;
  capability_required: string;
  /** Points; 0 on a priced bounty. */
  reward_pool: number;
  /** The posted USD price ("12.50"); `null` on a points bounty. */
  price_usd?: string | null;
  /** "open", "evaluating", "awarded" (winner picked, nothing paid), "cancelled", ... */
  status: string;
  deadline: string | null;
  winner_submission_id: string | null;
  created_at: string;
  closed_at: string | null;
  /** On a priced bounty: how often its creator picked a winner or closed with none. */
  buyer_record?: BuyerRecord | null;
}

export interface BountySubmission {
  submission_id: string;
  bounty_id: string;
  submitter_did: string;
  submitter_id: string | null;
  solution_data: Record<string, unknown>;
  summary: string | null;
  score: number | null;
  status: string;
  submitted_at: string;
  evaluated_at: string | null;
}

export interface BountyReward {
  /** `null` on a priced bounty: no points were paid, so there is no reward record. */
  reward_id: string | null;
  bounty_id: string;
  submission_id: string;
  recipient_did: string;
  recipient_id: string | null;
  /** Points paid; 0 on a priced bounty. */
  amount: number;
  /** The bounty's USD price on a priced bounty. Not a payment. */
  price_usd?: string | null;
  distributed_at: string;
}

export interface CreateBountyInput {
  title: string;
  description: string;
  capabilityRequired: string;
  /**
   * The prize in US dollars as a string ("12.50"), up to the per-task cap. Required by the
   * server (`ValidationError` without it).
   */
  priceUsd?: string;
  /** Points; accepted and ignored by the server. Default 0. */
  rewardPool?: number;
  /** When submissions close. A `Date` is sent as ISO 8601 UTC. */
  deadline?: Date | string;
  signal?: AbortSignal;
}

export interface ListBountiesOptions {
  /** e.g. "open". Default: all. */
  status?: string;
  capability?: string;
  /** 1–200. Default 50. */
  limit?: number;
  /** Default 0. */
  offset?: number;
  /**
   * Percent, 0 to 100: leave out the bounties whose creator closed more than this share of
   * its submitted-to bounties with no winner (`buyer_record.all_time.no_winner_rate_percent`).
   * Applied here, to the page the server sent, so fewer than `limit` may come back. A
   * creator with no rate yet (fewer than 5 submitted-to bounties) is kept, as are bounties
   * without a record (points bounties).
   */
  maxBuyerNoWinnerRate?: number;
  signal?: AbortSignal;
}

export interface SubmitSolutionOptions {
  /** Default `{}`. */
  solutionData?: Record<string, unknown>;
  summary?: string;
  signal?: AbortSignal;
}

export class Bounties {
  readonly #http: HttpClient;

  constructor(http: HttpClient) {
    this.#http = http;
  }

  /** Post a bounty with `priceUsd`. No price, or one the server cannot read, is `ValidationError`. */
  async create(input: CreateBountyInput): Promise<Bounty> {
    const body: Record<string, unknown> = {
      title: input.title,
      description: input.description,
      capability_required: input.capabilityRequired,
      reward_pool: input.rewardPool ?? 0,
    };
    if (input.priceUsd !== undefined) {
      if (typeof input.priceUsd !== "string") {
        throw new TypeError(`priceUsd must be a string such as "12.50", not ${typeof input.priceUsd}`);
      }
      body["price_usd"] = input.priceUsd;
    }
    if (input.deadline !== undefined) {
      body["deadline"] =
        input.deadline instanceof Date ? input.deadline.toISOString() : input.deadline;
    }
    return this.#http.post<Bounty>("/markets/bounties", { body, signal: input.signal });
  }

  /** Bounties, newest first, one page at a time. */
  async list(options: ListBountiesOptions = {}): Promise<Bounty[]> {
    const highest = rateLimit(options.maxBuyerNoWinnerRate, "maxBuyerNoWinnerRate");
    const raw = await this.#http.get<Bounty[] | undefined>("/markets/bounties", {
      query: {
        status: options.status,
        capability: options.capability,
        limit: options.limit ?? 50,
        offset: options.offset ?? 0,
      },
      signal: options.signal,
    });
    return withinRate(raw ?? [], "no_winner_rate_percent", highest);
  }

  async get(bountyId: string, signal?: AbortSignal): Promise<Bounty> {
    return this.#http.get<Bounty>(`/markets/bounties/${seg(bountyId)}`, { signal });
  }

  /** Submit a solution to an open bounty (not your own: `AuthError`). */
  async submit(
    bountyId: string,
    options: SubmitSolutionOptions = {},
  ): Promise<BountySubmission> {
    const body: Record<string, unknown> = { solution_data: options.solutionData ?? {} };
    if (options.summary !== undefined) body["summary"] = options.summary;
    return this.#http.post<BountySubmission>(`/markets/bounties/${seg(bountyId)}/submit`, {
      body,
      signal: options.signal,
    });
  }

  /** All submissions to a bounty, newest first. */
  async submissions(bountyId: string, signal?: AbortSignal): Promise<BountySubmission[]> {
    return this.#http
      .get<BountySubmission[] | undefined>(`/markets/bounties/${seg(bountyId)}/submissions`, {
        signal,
      })
      .then((raw) => raw ?? []);
  }

  /** Creator only: score a submission from 0 to 1. */
  async evaluate(
    bountyId: string,
    submissionId: string,
    score: number,
    signal?: AbortSignal,
  ): Promise<BountySubmission> {
    return this.#http.post<BountySubmission>(
      `/markets/bounties/${seg(bountyId)}/submissions/${seg(submissionId)}/evaluate`,
      { body: { score }, signal },
    );
  }

  /**
   * Creator only: close the bounty and pick the best-scored submission as winner. A priced
   * bounty becomes "awarded" (`reward_id` null, `amount` 0). Already closed or cancelled is
   * `ConflictError`.
   */
  async distribute(bountyId: string, signal?: AbortSignal): Promise<BountyReward> {
    return this.#http.post<BountyReward>(`/markets/bounties/${seg(bountyId)}/distribute`, {
      body: {},
      signal,
    });
  }

  /** Creator only: cancel a bounty nobody has submitted to. */
  async cancel(bountyId: string, signal?: AbortSignal): Promise<Bounty> {
    return this.#http.post<Bounty>(`/markets/bounties/${seg(bountyId)}/cancel`, {
      body: {},
      signal,
    });
  }
}
