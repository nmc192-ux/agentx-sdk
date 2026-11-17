"""The TypeScript SDK's payout wallet, sign-out-everywhere and login reset send what agentx-social sends.

Same method as ``test_ts_parity_tasks.py``: identical calls through ``AgentXClient``
(httpx mock transport) and ``@agentx-social/sdk`` (Node, ``fetch`` stubbed); method, path,
query, JSON body and whether a token was sent must match for every request. Each side's
``sign`` function records the texts it was given, and those must match too. Skipped
without Node >= 22.18.
"""

import json
import subprocess

import httpx
import pytest

from agentx_sdk.auth import AgentIdentity
from agentx_sdk.client import AgentXClient

from .test_ts_parity_tasks import TS_DIR, _node, _norm

DID = "did:agentx:atlas"
OTHER = "did:agentx:bruno"
WALLET = "0x1111111111111111111111111111111111111111"
NEW = "0x2222222222222222222222222222222222222222"


def _challenge(text: str) -> dict:
    return {
        "prompt": "x", "signed_by": "x", "address": WALLET, "text_to_sign": text,
        "how_to_sign": "personal_sign (EIP-191) over text_to_sign, exactly as given",
        "expires_at": "2026-11-14T00:10:00+00:00", "moves_funds": False,
    }


# One answer per path, the same on both sides.
ANSWERS = {
    "/agents/me/payout-wallet/challenge": _challenge("set text"),
    "/agents/me/payout-wallet/change/challenge": {
        "current_wallet": _challenge("current text"), "new_wallet": _challenge("new text"),
    },
    "/agents/me/payout-wallet/change/cancel/challenge": _challenge("cancel text"),
    "/agents/me/payout-wallet/remove/challenge": _challenge("remove text"),
    "/auth/wallet-reset/challenge": _challenge("reset text"),
    "/auth/wallet-reset": {
        "access_token": "new-acc", "refresh_token": "new-ref", "token_type": "bearer",
        "expires_in": 900, "agent_did": DID,
    },
}
DEFAULT = {"agent_did": DID, "status": "pending", "signed_out": True}


def _python_requests() -> tuple[list[dict], list[str]]:
    seen: list[dict] = []
    texts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        seen.append({**_norm(request.method, str(request.url), body),
                     "auth": "authorization" in request.headers})
        return httpx.Response(200, json=ANSWERS.get(request.url.path, DEFAULT))

    def sign(label: str):
        def _sign(text: str) -> str:
            texts.append(f"{label}:{text}")
            return f"0x{label}"
        return _sign

    c = AgentXClient(api_key="k", base_url="http://api.test")
    c._http = httpx.Client(base_url="http://api.test", transport=httpx.MockTransport(handler))
    c.identity = AgentIdentity(agent_did=DID, api_key="k")
    p = c.payout_wallet

    p.record()
    p.record(OTHER)
    p.set_challenge(WALLET)
    p.set(WALLET, "0xaa")
    p.set_with(WALLET, sign("a"))
    p.cancel_pending()
    p.change_challenges(NEW)
    p.change(NEW, "0xbb", "0xcc")
    p.change_with(NEW, sign("cur"), sign("new"))
    p.cancel_change_challenge()
    p.cancel_change("0xdd")
    p.cancel_change_with(sign("c"))
    p.remove_challenge()
    p.remove("0xee")
    p.remove_with(sign("r"))
    c.logout_all()
    c.reset_login_challenge()
    c.reset_login_challenge(OTHER)
    c.reset_login("0xff")
    c.reset_login_with(sign("z"))
    c.payout_wallet.record()  # with the pair the reset returned
    return seen, texts


TS_SCRIPT = """
import { AgentX } from "./src/index.ts";
const [WALLET, NEW, OTHER, ANSWERS, DEFAULT] = %s;
const seen = [];
const texts = [];
const fetch = async (input, init = {}) => {
  const url = new URL(input);
  seen.push({ method: init.method ?? "GET", url: input,
              body: init.body === undefined ? null : JSON.parse(init.body),
              auth: new Headers(init.headers).has("Authorization") });
  return new Response(JSON.stringify(ANSWERS[url.pathname] ?? DEFAULT), { status: 200 });
};
const sign = (label) => async (text) => { texts.push(`${label}:${text}`); return `0x${label}`; };
const a = new AgentX({ accessToken: "k", agentDid: "did:agentx:atlas",
                       baseUrl: "http://api.test", fetch });
const p = a.payoutWallet;
await p.record();
await p.record(OTHER);
await p.setChallenge(WALLET);
await p.set(WALLET, "0xaa");
await p.setWith(WALLET, sign("a"));
await p.cancelPending();
await p.changeChallenges(NEW);
await p.change(NEW, "0xbb", "0xcc");
await p.changeWith(NEW, sign("cur"), sign("new"));
await p.cancelChangeChallenge();
await p.cancelChange("0xdd");
await p.cancelChangeWith(sign("c"));
await p.removeChallenge();
await p.remove("0xee");
await p.removeWith(sign("r"));
await a.logoutAll();
await a.resetLoginChallenge();
await a.resetLoginChallenge(OTHER);
await a.resetLogin("0xff");
await a.resetLoginWith(sign("z"));
await p.record();
console.log(JSON.stringify({ seen, texts }));
"""


def _ts_requests(node: str) -> tuple[list[dict], list[str]]:
    script = TS_SCRIPT % json.dumps([WALLET, NEW, OTHER, ANSWERS, DEFAULT])
    proc = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=60, cwd=TS_DIR,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    raw = json.loads(proc.stdout.strip().splitlines()[-1])
    seen = [{**_norm(r["method"], r["url"], r["body"]), "auth": r["auth"]} for r in raw["seen"]]
    return seen, raw["texts"]


def test_ts_payout_wallet_and_login_reset_match_agentx_social():
    node = _node()
    if node is None:
        pytest.skip("Node >= 22.18 not available")
    (py, py_texts), (ts, ts_texts) = _python_requests(), _ts_requests(node)
    assert len(py) == len(ts) == 26
    for i, (p, t) in enumerate(zip(py, ts)):
        assert p == t, f"call {i}: agentx-social {p} != @agentx-social/sdk {t}"
    assert py_texts == ts_texts == [
        "a:set text", "cur:current text", "new:new text", "c:cancel text",
        "r:remove text", "z:reset text",
    ]
    # The reset calls carry no token on either side; the call after them does.
    reset = [r for r in py if r["path"].startswith("/auth/wallet-reset")]
    assert len(reset) == 5 and not any(r["auth"] for r in reset)
    assert py[-1]["auth"] is True
