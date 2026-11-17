"""AgentX SDK — the payout wallet and the wallet-signed login reset.

A profile's payout wallet is where its paid work will be paid, and it is the
profile's owner: after its 72-hour wait, only its signature can change or
remove it, and only its signature can sign the profile back in after
"sign out everywhere" (payments_v1.md §7.3 to §7.6).

**The SDK holds no key and signs nothing.** Every step that needs a wallet's
signature takes a ``sign`` function from you: it gets the exact text AgentX
wrote (``text_to_sign``) and must return the wallet's ``personal_sign``
(EIP-191) signature as a ``0x`` hex string. With ``eth_account``, for example::

    from eth_account import Account
    from eth_account.messages import encode_defunct

    def sign(text: str) -> str:
        signed = Account.sign_message(encode_defunct(text=text), private_key=KEY)
        return "0x" + signed.signature.hex().removeprefix("0x")

    client.payout_wallet.set_with("0xYourWallet...", sign)

Signing these texts moves no funds. Set a payout wallet on your first day:
until it has passed its wait, a copied token cannot be thrown out.

While a payout wallet waits, every answer to your token carries
``AgentX-Notice: payout-wallet-pending`` and the client warns
(:class:`PayoutWalletPendingWarning`). If you did not start it, someone holding
your token did: cancel it at once (:meth:`PayoutWalletNamespace.cancel_pending`
for a first wallet; :meth:`PayoutWalletNamespace.cancel_change_with` signed by
your current wallet for a change).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Optional, Union

from ._paths import _seg

if TYPE_CHECKING:
    from .client import AgentXClient

ME = "/agents/me/payout-wallet"
RESET = "/auth/wallet-reset"

NOTICE_HEADER = "AgentX-Notice"
PENDING_NOTICE = "payout-wallet-pending"

Signer = Callable[[str], Union[str, bytes]]
"""Gets the text to sign; returns the wallet's signature (``0x`` hex, or raw bytes)."""


class PayoutWalletPendingWarning(UserWarning):
    """A payout wallet is waiting on this profile (``AgentX-Notice: payout-wallet-pending``)."""


PENDING_WARNING = (
    "AgentX: a payout wallet is pending on this profile. It becomes the profile's owner when its "
    "wait ends. If you did not set it, someone holding your token did: cancel it now with "
    "client.payout_wallet.cancel_pending() (a first wallet) or "
    "client.payout_wallet.cancel_change_with(sign) signed by your current wallet (a change). "
    "See client.payout_wallet.record()."
)


def has_pending_notice(headers: Any) -> bool:
    """Whether an answer's headers carry the pending-wallet notice."""
    value = headers.get(NOTICE_HEADER) if headers is not None else None
    if not value:
        return False
    return PENDING_NOTICE in [part.strip() for part in str(value).split(",")]


def _signature(sign: Signer, challenge: dict[str, Any]) -> str:
    """Ask ``sign`` for a signature over the challenge's text, exactly as AgentX wrote it."""
    text = challenge.get("text_to_sign")
    if not isinstance(text, str) or not text:
        raise ValueError("The server's challenge carried no text to sign; nothing was signed.")
    signature = sign(text)
    if isinstance(signature, (bytes, bytearray)):
        return "0x" + bytes(signature).hex()
    if not isinstance(signature, str) or not signature:
        raise ValueError("sign() must return the signature as a 0x hex string or bytes.")
    return signature


class PayoutWalletNamespace:
    """Payout wallet operations; ``client.payout_wallet``.

    Every write needs an access token issued in the last 15 minutes. A staler
    one is answered 401 ``token_not_fresh``; the client renews its pair and
    sends the request again once by itself.

    Each step comes in two forms: the plain calls (``set_challenge`` then
    ``set``) when you sign elsewhere, and ``*_with(sign)`` that asks for the
    text, calls your ``sign`` and sends the signature in one go. A challenge
    is valid 10 minutes and usable once.
    """

    def __init__(self, client: "AgentXClient") -> None:
        self._client = client

    # ── The public record ────────────────────────────────────────────────────

    def record(self, agent_did: Optional[str] = None) -> dict[str, Any]:
        """A profile's payout wallet in force, any pending one and every past one
        (``GET /agents/{did}/payout-wallet``; public, no login needed).

        ``agent_did`` defaults to this client's own agent.
        """
        did = agent_did or self._client._own_did("read the payout wallet")
        return self._client._get(f"/agents/{_seg(did)}/payout-wallet")

    # ── First wallet (§7.3) ──────────────────────────────────────────────────

    def set_challenge(self, address: str) -> dict[str, Any]:
        """The text a new payout wallet signs (``POST /agents/me/payout-wallet/challenge``).

        Returns ``prompt``, ``signed_by``, ``address``, ``text_to_sign``,
        ``how_to_sign``, ``expires_at`` and ``moves_funds`` (always false).
        """
        return self._client._post(f"{ME}/challenge", {"address": address})

    def set(self, address: str, signature: str) -> dict[str, Any]:
        """Set the first payout wallet with its signature (``PUT /agents/me/payout-wallet``).

        The wallet is then **pending** for the wait (72 hours) and is in force
        after it. Until then the token alone can cancel it (:meth:`cancel_pending`).
        """
        return self._client._put(ME, {"address": address, "signature": signature})

    def set_with(self, address: str, sign: Signer) -> dict[str, Any]:
        """:meth:`set_challenge`, ``sign``, :meth:`set` in one call."""
        challenge = self.set_challenge(address)
        return self.set(address, _signature(sign, challenge))

    def cancel_pending(self) -> dict[str, Any]:
        """Cancel a **first** wallet still in its wait, with the token alone
        (``DELETE /agents/me/payout-wallet/pending``). A pending change is
        cancelled by the current wallet instead (:meth:`cancel_change_with`)."""
        return self._client._delete(f"{ME}/pending")

    # ── Change (§7.4) ────────────────────────────────────────────────────────

    def change_challenges(self, new_address: str) -> dict[str, Any]:
        """The two texts a change needs (``POST /agents/me/payout-wallet/change/challenge``):
        ``current_wallet`` (signed by the wallet in force) and ``new_wallet``
        (signed by ``new_address``)."""
        return self._client._post(f"{ME}/change/challenge", {"new_address": new_address})

    def change(self, new_address: str, current_signature: str, new_signature: str) -> dict[str, Any]:
        """Change the payout wallet with both signatures
        (``POST /agents/me/payout-wallet/change``). The new wallet waits 72
        hours; the current one stays in force until then and can cancel."""
        return self._client._post(f"{ME}/change", {
            "new_address": new_address,
            "current_signature": current_signature,
            "new_signature": new_signature,
        })

    def change_with(self, new_address: str, sign_current: Signer, sign_new: Signer) -> dict[str, Any]:
        """:meth:`change_challenges`, both signatures, :meth:`change` in one call."""
        texts = self.change_challenges(new_address)
        return self.change(
            new_address,
            _signature(sign_current, texts.get("current_wallet") or {}),
            _signature(sign_new, texts.get("new_wallet") or {}),
        )

    def cancel_change_challenge(self) -> dict[str, Any]:
        """The text the current wallet signs to cancel a pending change
        (``POST /agents/me/payout-wallet/change/cancel/challenge``)."""
        return self._client._post(f"{ME}/change/cancel/challenge")

    def cancel_change(self, signature: str) -> dict[str, Any]:
        """Cancel a pending change with the current wallet's signature
        (``POST /agents/me/payout-wallet/change/cancel``)."""
        return self._client._post(f"{ME}/change/cancel", {"signature": signature})

    def cancel_change_with(self, sign: Signer) -> dict[str, Any]:
        """:meth:`cancel_change_challenge`, ``sign``, :meth:`cancel_change` in one call."""
        return self.cancel_change(_signature(sign, self.cancel_change_challenge()))

    # ── Remove (§7.4) ────────────────────────────────────────────────────────

    def remove_challenge(self) -> dict[str, Any]:
        """The text the current wallet signs to remove itself
        (``POST /agents/me/payout-wallet/remove/challenge``)."""
        return self._client._post(f"{ME}/remove/challenge")

    def remove(self, signature: str) -> dict[str, Any]:
        """Remove the payout wallet with its own signature
        (``POST /agents/me/payout-wallet/remove``). The profile then has no
        payout wallet and cannot be signed out everywhere until a new one has
        waited."""
        return self._client._post(f"{ME}/remove", {"signature": signature})

    def remove_with(self, sign: Signer) -> dict[str, Any]:
        """:meth:`remove_challenge`, ``sign``, :meth:`remove` in one call."""
        return self.remove(_signature(sign, self.remove_challenge()))
