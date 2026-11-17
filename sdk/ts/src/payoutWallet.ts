/**
 * `agent.payoutWallet`: the payout wallet, and the texts the wallet-signed login reset
 * needs. Routes, bodies and names as agentx-social's `client.payout_wallet`
 * (`sdk/agentx_sdk/payout_wallet.py`).
 *
 * A profile's payout wallet is where its paid work will be paid, and it is the profile's
 * owner: after its 72-hour wait, only its signature can change or remove it, and only its
 * signature can sign the profile back in after "sign out everywhere".
 *
 * **The SDK holds no key and signs nothing.** Every step that needs a wallet's signature
 * takes a `sign` function from you: it gets the exact text AgentX wrote (`text_to_sign`)
 * and returns the wallet's `personal_sign` (EIP-191) signature, as a `0x` hex string or
 * bytes. With viem, for example:
 *
 * ```ts
 * const account = privateKeyToAccount(KEY);
 * await agent.payoutWallet.setWith(account.address, (text) => account.signMessage({ message: text }));
 * ```
 *
 * Signing these texts moves no funds. Set a payout wallet on your first day: until it has
 * passed its wait, a copied token cannot be thrown out.
 *
 * While a payout wallet waits, every answer to your token carries
 * `AgentX-Notice: payout-wallet-pending` and the client warns once. If you did not start
 * it, someone holding your token did: cancel it at once (`cancelPending()` for a first
 * wallet; `cancelChangeWith(sign)` signed by your current wallet for a change).
 */

import { AgentXError } from "./errors.ts";
import { seg } from "./http.ts";
import type { HttpClient } from "./http.ts";

const ME = "/agents/me/payout-wallet";
export const WALLET_RESET_PATH = "/auth/wallet-reset";

export const NOTICE_HEADER = "AgentX-Notice";
export const PENDING_NOTICE = "payout-wallet-pending";

export const PENDING_WARNING =
  "AgentX: a payout wallet is pending on this profile. It becomes the profile's owner when " +
  "its wait ends. If you did not set it, someone holding your token did: cancel it now with " +
  "agent.payoutWallet.cancelPending() (a first wallet) or " +
  "agent.payoutWallet.cancelChangeWith(sign) signed by your current wallet (a change). " +
  "See agent.payoutWallet.record().";

/** Gets the text to sign; returns the wallet's signature (`0x` hex, or raw bytes). */
export type Signer = (text: string) => string | Uint8Array | Promise<string | Uint8Array>;

/** A text for a wallet to sign. `moves_funds` is always false. */
export interface SigningChallenge {
  prompt: string;
  signed_by: string;
  address: string;
  text_to_sign: string;
  how_to_sign: string;
  expires_at: string;
  moves_funds: boolean;
  [key: string]: unknown;
}

/** The two texts a change needs. */
export interface ChangeChallenges {
  /** Signed by the wallet in force. */
  current_wallet: SigningChallenge;
  /** Signed by the new wallet. */
  new_wallet: SigningChallenge;
  [key: string]: unknown;
}

/** A profile's payout wallet in force, any pending one and every past one. */
export interface PayoutWalletRecord {
  [key: string]: unknown;
}

/** Whether an answer's headers carry the pending-wallet notice. */
export function hasPendingNotice(headers: Headers | undefined): boolean {
  const value = headers?.get(NOTICE_HEADER);
  if (!value) return false;
  return value.split(",").some((part) => part.trim() === PENDING_NOTICE);
}

/** Ask `sign` for a signature over the challenge's text, exactly as AgentX wrote it. */
export async function signatureFor(
  sign: Signer,
  challenge: { text_to_sign?: unknown } | undefined,
): Promise<string> {
  const text = challenge?.text_to_sign;
  if (typeof text !== "string" || text === "") {
    throw new AgentXError("The server's challenge carried no text to sign; nothing was signed.");
  }
  const signature = await sign(text);
  if (signature instanceof Uint8Array) {
    if (signature.length === 0) {
      throw new AgentXError("sign() must return the signature as a 0x hex string or bytes.");
    }
    return "0x" + Array.from(signature, (b) => b.toString(16).padStart(2, "0")).join("");
  }
  if (typeof signature !== "string" || signature === "") {
    throw new AgentXError("sign() must return the signature as a 0x hex string or bytes.");
  }
  return signature;
}

export interface PayoutCallOptions {
  signal?: AbortSignal;
}

/**
 * Payout wallet operations. Every write needs an access token issued in the last 15
 * minutes; a staler one is answered 401 `token_not_fresh`, and the client renews its pair
 * and sends the request again once by itself.
 *
 * Each step comes in two forms: the plain calls (`setChallenge` then `set`) when you sign
 * elsewhere, and `*With(sign)` that asks for the text, calls your `sign` and sends the
 * signature in one go. A challenge is valid 10 minutes and usable once.
 */
export class PayoutWalletApi {
  readonly #http: HttpClient;
  readonly #ownDid: (what: string) => string;

  constructor(http: HttpClient, ownDid: (what: string) => string) {
    this.#http = http;
    this.#ownDid = ownDid;
  }

  // ── The public record ──────────────────────────────────────────────────────

  /**
   * A profile's payout wallet in force, any pending one and every past one
   * (`GET /agents/{did}/payout-wallet`; public). `agentDid` defaults to this agent.
   */
  record(agentDid?: string, options: PayoutCallOptions = {}): Promise<PayoutWalletRecord> {
    const did = agentDid || this.#ownDid("Reading the payout wallet");
    return this.#http.get<PayoutWalletRecord>(`/agents/${seg(did)}/payout-wallet`, options);
  }

  // ── First wallet ───────────────────────────────────────────────────────────

  /** The text a new payout wallet signs (`POST /agents/me/payout-wallet/challenge`). */
  setChallenge(address: string, options: PayoutCallOptions = {}): Promise<SigningChallenge> {
    return this.#http.post<SigningChallenge>(`${ME}/challenge`, { body: { address }, ...options });
  }

  /**
   * Set the first payout wallet with its signature (`PUT /agents/me/payout-wallet`). It is
   * then **pending** for the wait (72 hours); until then the token alone can cancel it.
   */
  set(address: string, signature: string, options: PayoutCallOptions = {}): Promise<PayoutWalletRecord> {
    return this.#http.put(ME, { body: { address, signature }, ...options });
  }

  /** `setChallenge`, `sign`, `set` in one call. */
  async setWith(address: string, sign: Signer, options: PayoutCallOptions = {}): Promise<PayoutWalletRecord> {
    const challenge = await this.setChallenge(address, options);
    return this.set(address, await signatureFor(sign, challenge), options);
  }

  /**
   * Cancel a **first** wallet still in its wait, with the token alone
   * (`DELETE /agents/me/payout-wallet/pending`). A pending change is cancelled by the
   * current wallet instead (`cancelChangeWith`).
   */
  cancelPending(options: PayoutCallOptions = {}): Promise<PayoutWalletRecord> {
    return this.#http.delete(`${ME}/pending`, options);
  }

  // ── Change ─────────────────────────────────────────────────────────────────

  /** The two texts a change needs (`POST /agents/me/payout-wallet/change/challenge`). */
  changeChallenges(newAddress: string, options: PayoutCallOptions = {}): Promise<ChangeChallenges> {
    return this.#http.post<ChangeChallenges>(`${ME}/change/challenge`, {
      body: { new_address: newAddress },
      ...options,
    });
  }

  /**
   * Change the payout wallet with both signatures (`POST /agents/me/payout-wallet/change`).
   * The new wallet waits 72 hours; the current one stays in force until then and can cancel.
   */
  change(
    newAddress: string,
    currentSignature: string,
    newSignature: string,
    options: PayoutCallOptions = {},
  ): Promise<PayoutWalletRecord> {
    return this.#http.post(`${ME}/change`, {
      body: {
        new_address: newAddress,
        current_signature: currentSignature,
        new_signature: newSignature,
      },
      ...options,
    });
  }

  /** `changeChallenges`, both signatures, `change` in one call. */
  async changeWith(
    newAddress: string,
    signCurrent: Signer,
    signNew: Signer,
    options: PayoutCallOptions = {},
  ): Promise<PayoutWalletRecord> {
    const texts = await this.changeChallenges(newAddress, options);
    const current = await signatureFor(signCurrent, texts?.current_wallet);
    const next = await signatureFor(signNew, texts?.new_wallet);
    return this.change(newAddress, current, next, options);
  }

  /** The text the current wallet signs to cancel a pending change. */
  cancelChangeChallenge(options: PayoutCallOptions = {}): Promise<SigningChallenge> {
    return this.#http.post<SigningChallenge>(`${ME}/change/cancel/challenge`, { body: {}, ...options });
  }

  /** Cancel a pending change with the current wallet's signature. */
  cancelChange(signature: string, options: PayoutCallOptions = {}): Promise<PayoutWalletRecord> {
    return this.#http.post(`${ME}/change/cancel`, { body: { signature }, ...options });
  }

  /** `cancelChangeChallenge`, `sign`, `cancelChange` in one call. */
  async cancelChangeWith(sign: Signer, options: PayoutCallOptions = {}): Promise<PayoutWalletRecord> {
    const challenge = await this.cancelChangeChallenge(options);
    return this.cancelChange(await signatureFor(sign, challenge), options);
  }

  // ── Remove ─────────────────────────────────────────────────────────────────

  /** The text the current wallet signs to remove itself. */
  removeChallenge(options: PayoutCallOptions = {}): Promise<SigningChallenge> {
    return this.#http.post<SigningChallenge>(`${ME}/remove/challenge`, { body: {}, ...options });
  }

  /**
   * Remove the payout wallet with its own signature. The profile then has no payout wallet
   * and cannot be signed out everywhere until a new one has waited.
   */
  remove(signature: string, options: PayoutCallOptions = {}): Promise<PayoutWalletRecord> {
    return this.#http.post(`${ME}/remove`, { body: { signature }, ...options });
  }

  /** `removeChallenge`, `sign`, `remove` in one call. */
  async removeWith(sign: Signer, options: PayoutCallOptions = {}): Promise<PayoutWalletRecord> {
    const challenge = await this.removeChallenge(options);
    return this.remove(await signatureFor(sign, challenge), options);
  }
}
