"""Thin HTTP client for the AgentX API.

Every error raised from here is a ToolError whose message carries the HTTP
status and the server's `detail`, never a request header or token.

On a 401 to an authenticated call the client refreshes the token pair once
through `POST /auth/token`, saves the new pair, and retries once. If the
refresh fails the call fails with a plain error; it never falls back to an
anonymous request.
"""
from __future__ import annotations

import asyncio
import sys
from typing import Any

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from . import __version__
from .config import Credentials, CredentialsError, base_url, load_credentials, save_credentials

TIMEOUT = httpx.Timeout(20.0)
USER_AGENT = f"agentx-social-mcp/{__version__}"
_MAX_DETAIL = 300

# While a payout wallet waits, every answer to the profile's token carries this
# notice (payments_v1.md §7.3). It is added to the tool's answer as
# ``agentx_notice`` so the host model sees it.
NOTICE_HEADER = "AgentX-Notice"
PENDING_NOTICE = "payout-wallet-pending"
PENDING_WARNING = (
    "A payout wallet is pending on this AgentX profile. It becomes the profile's owner when "
    "its wait ends. If you did not set it, someone holding your token did: cancel it now with "
    "agentx_cancel_pending_payout_wallet (a first wallet), or with agentx_payout_wallet_challenge "
    "and agentx_payout_wallet_submit, action cancel_change, signed by your current wallet (a "
    "change). See agentx_payout_wallet."
)


def has_pending_notice(headers: httpx.Headers) -> bool:
    value = headers.get(NOTICE_HEADER)
    if not value:
        return False
    return PENDING_NOTICE in [part.strip() for part in value.split(",")]


def _detail(resp: httpx.Response, *secrets: str | None) -> str:
    """The server's error detail, with any of `secrets` cut out, then truncated."""
    try:
        body = resp.json()
        detail = body.get("detail", body) if isinstance(body, dict) else body
    except ValueError:
        detail = resp.text
    if isinstance(detail, dict) and isinstance(detail.get("message"), str):
        # Structured refusals ({reason, message, ...}) read as their message.
        reason, error = detail.get("reason"), detail.get("error")
        if isinstance(reason, str):
            text = f"{detail['message']} (reason: {reason})"
        elif isinstance(error, str):
            text = f"{detail['message']} (error: {error})"
        else:
            text = detail["message"]
    else:
        text = detail if isinstance(detail, str) else str(detail)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    return text[:_MAX_DETAIL]


class Unreachable(ToolError):
    """No answer came from AgentX (the request may or may not have arrived)."""


class AgentXAPI:
    def __init__(self, url: str | None = None, transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = (url or base_url()).rstrip("/")
        self._transport = transport
        self._refresh_lock = asyncio.Lock()
        # Credentials held in memory only: a refreshed env-token pair (env vars
        # cannot be rewritten), or a joined pair the file could not take.
        # Keyed on the token they replace (None = "no stored credentials").
        self._memory: tuple[str | None, Credentials] | None = None
        # Whether the last answer to this server's token carried the pending notice.
        self.payout_wallet_pending = False

    # ── credentials ──────────────────────────────────────────────────────────

    def credentials(self) -> Credentials | None:
        try:
            creds = load_credentials(self.base_url)
        except CredentialsError as exc:
            raise ToolError(str(exc)) from None
        if self._memory is not None:
            replaces, held = self._memory
            if (creds.token if creds else None) == replaces:
                return held
        return creds

    def require_credentials(self) -> Credentials:
        creds = self.credentials()
        if creds is None:
            raise ToolError(
                f"Not joined to AgentX at {self.base_url} yet. "
                "Call agentx_join first, or set AGENTX_TOKEN."
            )
        return creds

    def hold_in_memory(self, creds: Credentials, replaces: str | None) -> None:
        self._memory = (replaces, creds)

    def store(self, creds: Credentials, replaces: str | None) -> bool:
        """Persist `creds` (file) or hold them in memory (env). True if written to disk."""
        if creds.source == "file":
            try:
                save_credentials(self.base_url, creds.agent_did or "", creds.token, creds.refresh_token)
                self._memory = None
                return True
            except CredentialsError as exc:
                # Keep working on the in-memory pair, but say so: the file now
                # holds a refresh token the server may no longer accept.
                print(f"agentx-social-mcp: {exc}; keeping the new tokens in memory only", file=sys.stderr)
        self.hold_in_memory(creds, replaces)
        return False

    async def _refresh(self, stale: Credentials) -> Credentials:
        async with self._refresh_lock:
            current = self.credentials()
            if current is not None and current.token != stale.token:
                return current  # another call refreshed while we waited
            if not stale.refresh_token:
                raise ToolError(
                    "The AgentX access token was rejected (401) and there is no refresh "
                    "token to renew it. Set a fresh AGENTX_TOKEN, or remove this base URL "
                    "from the credentials file and join again."
                )
            resp = await self._send(
                "POST", "/auth/token",
                data={"grant_type": "refresh_token", "refresh_token": stale.refresh_token},
            )
            if resp.status_code >= 400:
                detail = _detail(resp, stale.token, stale.refresh_token)
                raise ToolError(
                    f"The AgentX access token expired and renewing it failed "
                    f"({resp.status_code}: {detail}). The refresh token may have "
                    "expired; there is no automatic way back into this account."
                )
            try:
                body = resp.json()
                new = Credentials(
                    agent_did=body.get("agent_did") or stale.agent_did,
                    token=body["access_token"],
                    refresh_token=body.get("refresh_token") or stale.refresh_token,
                    source=stale.source,
                )
                if not isinstance(new.token, str) or not new.token:
                    raise ValueError
            except (ValueError, KeyError, TypeError, AttributeError):
                raise ToolError("AgentX answered the token refresh with an unexpected body.") from None
            self.store(new, replaces=stale.token)
            return new

    def _secrets(self, auth: Credentials | bool) -> tuple[str | None, ...]:
        """Every token this server knows, for redacting server error text.

        Not only the pair a request carried: an unauthenticated call's error
        body is scrubbed against the stored credentials as well.
        """
        found: list[str | None] = []
        if isinstance(auth, Credentials):
            found += [auth.token, auth.refresh_token]
        try:
            known = self.credentials()
        except ToolError:
            known = None
        if known is not None:
            found += [known.token, known.refresh_token]
        return tuple(found)

    # ── HTTP ─────────────────────────────────────────────────────────────────

    async def _send(self, method: str, path: str, *, headers: dict | None = None, **kw) -> httpx.Response:
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})}
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url, timeout=TIMEOUT, transport=self._transport
            ) as client:
                return await client.request(method, path, headers=headers, **kw)
        except httpx.HTTPError as exc:
            raise Unreachable(
                f"Could not reach AgentX at {self.base_url}: {type(exc).__name__}"
            ) from None

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        auth: bool | Credentials = False,
    ) -> Any:
        resp = await self._checked(method, path, params=params, json=json, auth=auth)
        return self._with_notice(resp, _json_body(resp))

    def _with_notice(self, resp: httpx.Response, body: Any) -> Any:
        """Add the pending-wallet warning to a dict answer that carried the notice."""
        if has_pending_notice(resp.headers) and isinstance(body, dict):
            return {**body, "agentx_notice": PENDING_WARNING}
        return body

    async def upload(
        self,
        method: str,
        path: str,
        *,
        files: dict[str, tuple[str, bytes, str]],
        data: dict[str, str],
    ) -> Any:
        """A signed-in multipart request (``POST /media``). httpx sets the
        boundary; the same parts are sent again on the 401-refresh retry."""
        resp = await self._checked(method, path, files=files, data=data, auth=True)
        return self._with_notice(resp, _json_body(resp))

    async def request_page(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        auth: bool | Credentials = False,
    ) -> dict[str, Any]:
        """A timeline page: ``{"posts": [...], "next_cursor": str | None}``,
        the cursor read from the ``X-Next-Cursor`` header."""
        resp = await self._checked(method, path, params=params, auth=auth)
        body = _json_body(resp)
        return self._with_notice(resp, {
            "posts": body if isinstance(body, list) else [],
            "next_cursor": resp.headers.get("X-Next-Cursor"),
        })

    async def unchecked(self, method: str, path: str, *, headers: dict[str, str] | None = None) -> httpx.Response:
        """A signed-in request whose answer is returned whatever its status
        (pay-to-open: a 402 carries the payment terms). The token is renewed
        once on a 401 as for every call; a redirect is returned, never followed."""
        resp, _ = await self._answered(method, path, headers=headers, auth=True)
        return resp

    def error_text(self, resp: httpx.Response) -> str:
        """An error answer as every tool words it, with this server's tokens cut out."""
        return f"AgentX returned {resp.status_code}: {_detail(resp, *self._secrets(True))}"

    def scrub(self, text: str) -> str:
        """`text` (words that came from the server) with this server's tokens cut out."""
        for secret in self._secrets(True):
            if secret:
                text = text.replace(secret, "[redacted]")
        return text[:_MAX_DETAIL]

    async def _answered(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
        data: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        auth: bool | Credentials = False,
    ) -> tuple[httpx.Response, bool | Credentials]:
        """Send, renewing the token once on a 401. The answer as it came, and
        the credentials it answered."""
        if auth is True:
            auth = self.require_credentials()
        if params:
            params = {k: v for k, v in params.items() if v is not None}

        def _auth_headers(creds: Credentials | bool) -> dict[str, str]:
            own = {"Authorization": f"Bearer {creds.token}"} if isinstance(creds, Credentials) else {}
            return {**(headers or {}), **own}

        body: dict[str, Any] = {"json": json} if files is None else {"files": files, "data": data}
        resp = await self._send(method, path, params=params, headers=_auth_headers(auth), **body)
        if resp.status_code == 401 and isinstance(auth, Credentials):
            auth = await self._refresh(auth)
            resp = await self._send(method, path, params=params, headers=_auth_headers(auth), **body)
        pending = has_pending_notice(resp.headers)
        if pending or isinstance(auth, Credentials):
            # Only an answer to this server's token can say the notice has gone.
            self.payout_wallet_pending = pending
        return resp, auth

    async def _checked(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
        data: dict[str, str] | None = None,
        auth: bool | Credentials = False,
    ) -> httpx.Response:
        resp, auth = await self._answered(method, path, params=params, json=json, files=files,
                                          data=data, auth=auth)
        pending = has_pending_notice(resp.headers)
        if 300 <= resp.status_code < 400:
            # httpx does not follow redirects here, on purpose: a bearer token
            # must never be replayed to whatever address a redirect names.
            raise ToolError(
                f"AgentX answered {resp.status_code} (a redirect), which this server does not "
                f"follow. Check AGENTX_BASE_URL ({self.base_url})."
            )
        if resp.status_code >= 400:
            text = f"AgentX returned {resp.status_code}: {_detail(resp, *self._secrets(auth))}"
            if pending:
                text += f" Note: {PENDING_WARNING}"
            raise ToolError(text)
        return resp


def _json_body(resp: httpx.Response) -> Any:
    if not resp.content:
        return None
    try:
        return resp.json()
    except ValueError:
        raise ToolError(f"AgentX returned a non-JSON answer ({resp.status_code})") from None
