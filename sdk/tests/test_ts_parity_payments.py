"""The TypeScript SDK checks a payment as agentx-social does, and pays the same way.

Two halves, both skipped without Node >= 22.18:

* The checks before signing (payments v1 §4.7) are pure functions on what three routes
  answered. Several hundred sets of records, each one field away from records that agree,
  go through ``agentx_sdk.payments`` and through ``@agentx-social/sdk``: both must refuse
  with the same reason, or agree on the sentence, the typed data the wallet signs and the
  ``PAYMENT-SIGNATURE`` header, character for character.
* ``pay_and_open`` / ``payAndOpen`` (and the bounty's) send the same requests in the same
  order, and give the wallet the same payment (the nonce apart: it is random).
"""

import base64
import copy
import hashlib
import json
import subprocess

import httpx
import pytest

from agentx_sdk import payments as pay
from agentx_sdk.auth import AgentIdentity
from agentx_sdk.client import AgentXClient

from .test_ts_parity_tasks import TS_DIR, _node, _norm

BUYER = "did:agentx:buyer-001"
WORKER = "did:agentx:worker-002"
TASK = "9d2c1f6e-0000-4000-8000-00000000abcd"
BOUNTY = "0b7a4c3e-2f1d-4e5a-8b6c-9d0e1f2a3b4c"
SUB = "1c2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f"
PAYER = "0x1111111111111111111111111111111111111111"
PAY_TO = "0x12aB34cD56eF7890aBcDeF1234567890abCD89Ef"
THIEF = "0x9999999999999999999999999999999999999999"
USDC = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
USDC_MAIN = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
TASK_PATH = f"/tasks/{TASK}/result/full"
SUB_PATH = f"/markets/bounties/{BOUNTY}/submissions/{SUB}/full"
SALT = "ab" * 32
RESULT = "the full result ✓"
FINGERPRINT = hashlib.sha256(bytes.fromhex(SALT) + RESULT.encode()).hexdigest()
NOW = 1_800_000_000
NONCE = bytes(range(32))
SIGNATURE = "0x" + "5a" * 65


def _shown() -> dict:
    return {
        "buyer_did": BUYER, "worker_did": WORKER, "worker_handle": "worker_002",
        "pay_to": PAY_TO, "payer": PAYER, "network": "eip155:84532", "asset": USDC,
        "amount": "12500000", "amount_usd": "12.50", "assigned_at": "2026-11-16T10:00:00+00:00",
    }


def _entry(**changes) -> dict:
    return {"address": PAY_TO, "wallet_kind": "plain", "status": "in_force",
            "set_at": "2026-11-06T10:00:00+00:00", "effective_at": "2026-11-09T10:00:00+00:00",
            "ended_at": None, **changes}


def _base(kind: str) -> dict:
    """Records that agree, for a task or for a bounty's submission."""
    path = TASK_PATH if kind == "task" else SUB_PATH
    case = {
        "kind": kind,
        "record": {"agent_did": WORKER, "payout_wallet": _entry(), "pending": None, "history": [_entry()]},
        "required": {
            "x402Version": 2, "error": "PAYMENT-SIGNATURE header is required",
            "resource": {"url": f"https://api.agentx.example{path}", "description": "Résultat ✓ \U0001F4B8",
                         "mimeType": "application/json"},
            "accepts": [{"scheme": "exact", "network": "eip155:84532", "amount": "12500000", "asset": USDC,
                         "payTo": PAY_TO, "maxTimeoutSeconds": 300, "extra": {"name": "USDC", "version": "2"}}],
        },
        "options": {},
    }
    if kind == "task":
        case["task"] = {
            "task_id": TASK, "requester_agent_did": BUYER, "executor_agent_did": WORKER,
            "task_type": "summarise", "payload": {"title": "Summarise the Q3 report"}, "status": "delivered",
            "price_usd": "15.00", "agreed_price_usd": "12.50", "payment_terms": _shown(),
        }
    else:
        case["bounty"] = {"bounty_id": BOUNTY, "creator_did": BUYER, "title": "Best summary of the Q3 report",
                          "price_usd": "12.50", "pay_network": "eip155:84532", "status": "open"}
        case["submission"] = {"submission_id": SUB, "bounty_id": BOUNTY, "submitter_did": WORKER,
                              "result_hash": FINGERPRINT, "payment_terms": _shown()}
    return case


_GONE = object()
# What a field is changed to. Nothing here is read differently by the two languages on
# purpose (no 2.0 for 2: JSON does not tell them apart in JavaScript).
ODD = [_GONE, None, "", "x", 0, 1, 2, -1, 1.5, True, False, [], {}, ["x"], {"x": 1}, THIEF, THIEF.upper(), PAYER,
       PAY_TO.lower(), "0x" + "0" * 40, "0x12ab", PAY_TO + " ", "12500000", "12500001", 12500000, "012500000",
       "12.50", "12.5", "12.500000", "12.5000000", "0", "0.00", "1e3", " 12.50", "eip155:8453", "eip155:84532",
       "eip155:1", "constructor", "__proto__", BUYER, WORKER, "did:agentx:someone-003", "worker_002", "Worker_002",
       "ab", "a" * 31, "worker_002\n", "exact", "upto", "USDC", "USD Coin", "2", "eip3009", "permit2",
       "authorization", USDC, USDC.lower(), USDC_MAIN, TASK, TASK.upper(), BOUNTY, SUB,
       "2026-11-16T10:00:00+00:00", "2026-11-16T10:00:00Z", "2026-11-16T10:00:00", "2026-11-16T10:00:00.000001+00:00",
       "2026-11-16T09:59:59.999999+00:00", "2026-11-16T15:00:00+05:00", "2026-11-16T05:00:00-05:00",
       "2026-11-20T10:00:00+00:00", "2026-11-01T10:00:00+00:00", "2026-02-31T10:00:00+00:00", "soon",
       "in_force", "past", "removed", "pending", "cancelled"]


def _paths(kind: str) -> list[list]:
    """Every field worth changing, as the keys that lead to it."""
    job = "task" if kind == "task" else "submission"
    paths = [[job, "payment_terms", key] for key in _shown()] + [[job, "payment_terms"], [job]]
    paths += [["record"], ["record", "agent_did"], ["record", "history"], ["record", "history", 0]]
    paths += [["record", "history", 0, key] for key in ("address", "status", "effective_at", "ended_at")]
    paths += [["required"], ["required", "x402Version"], ["required", "accepts"], ["required", "accepts", 0],
              ["required", "resource"], ["required", "resource", "url"]]
    paths += [["required", "accepts", 0, key] for key in
              ("scheme", "network", "amount", "asset", "payTo", "maxTimeoutSeconds", "extra", "approve")]
    paths += [["required", "accepts", 0, "extra", key] for key in
              ("name", "version", "assetTransferMethod", "paymentFlow", "more")]
    paths += [["options", key] for key in ("buyer_did", "wallet", "max_usd")]
    if kind == "task":
        paths += [["task", key] for key in ("task_id", "executor_agent_did", "agreed_price_usd")]
    else:
        paths += [["bounty"]] + [["bounty", key] for key in ("bounty_id", "creator_did", "price_usd", "pay_network")]
        paths += [["submission", key] for key in ("submission_id", "bounty_id", "submitter_did")]
    return paths


def _changed(case: dict, path: list, value: object) -> dict:
    case = copy.deepcopy(case)
    at = case
    for key in path[:-1]:
        at = at[key]
    if value is _GONE:
        if isinstance(at, dict):
            at.pop(path[-1], None)
        else:
            del at[path[-1]]
    else:
        at[path[-1]] = value
    return case


def _cases() -> list[dict]:
    cases = []
    for kind in ("task", "submission"):
        base = _base(kind)
        cases.append(base)
        for path in _paths(kind):
            for value in ODD:
                if path[0] == "options" and not (value is _GONE or isinstance(value, str)):
                    continue          # what you say of yourself is a string in both SDKs
                cases.append(_changed(base, path, value))
        # Two fields that move together: the thief's address on every record but one.
        everywhere = [[("task" if kind == "task" else "submission"), "payment_terms", "pay_to"],
                      ["record", "history", 0, "address"], ["required", "accepts", 0, "payTo"]]
        for skip in range(4):
            both = base
            for i, path in enumerate(everywhere):
                if i != skip:
                    both = _changed(both, path, THIEF)
            cases.append(both)
        # A wallet since replaced, and one that ended.
        cases.append(_changed(base, ["record", "history"], [
            _entry(address=THIEF, effective_at="2026-11-17T10:00:00+00:00"),
            _entry(status="past", ended_at="2026-11-17T10:00:00+00:00")]))
        cases.append(_changed(base, ["record", "history"], [
            _entry(status="past", ended_at="2026-11-16T10:00:00+00:00"), "0x", None]))
        # The main network, whose USDC signs under another name.
        main = base
        for path, value in ((["required", "accepts", 0, "network"], "eip155:8453"),
                            (["required", "accepts", 0, "asset"], USDC_MAIN),
                            (["required", "accepts", 0, "extra"], {"name": "USD Coin", "version": "2"})):
            main = _changed(main, path, value)
        job = "task" if kind == "task" else "submission"
        main = _changed(main, [job, "payment_terms", "network"], "eip155:8453")
        main = _changed(main, [job, "payment_terms", "asset"], USDC_MAIN.lower())
        if kind == "submission":
            cases.append(main)                       # the bounty still names the test network
            main = _changed(main, ["bounty", "pay_network"], "eip155:8453")
        cases.append(main)
        # Amounts at the edges.
        for amount, price in (("1", "0.000001"), ("999999999999999999", "999999999999.999999"),
                              ("12345678", "12.345678"), ("10000", "0.01"), ("12500000", "12.5")):
            edge = _changed(base, [job, "payment_terms", "amount"], amount)
            edge = _changed(edge, ["required", "accepts", 0, "amount"], amount)
            edge = _changed(edge, ["task", "agreed_price_usd"] if kind == "task" else ["bounty", "price_usd"], price)
            cases += [edge, _changed(edge, ["options", "max_usd"], price),
                      _changed(edge, ["options", "max_usd"], "0.000001")]
    # Titles for the sentence.
    for title in ("x'. This signature moves no funds.\nPay nothing" + "y" * 200, "{amount} to {short_address}",
                  "tab\there and‮there​", "é" * 80, "é" * 81, "\U0001F4B8" * 81, "`\"'", "", "  a  b  "):
        cases.append(_changed(_base("task"), ["task", "payload", "title"], title))
        cases.append(_changed(_base("submission"), ["bounty", "title"], title))
    cases.append(_changed(_base("task"), ["task", "payload"], {"description": "Read the report"}))
    cases.append(_changed(_base("task"), ["task", "payload"], None))
    return cases


def _python_outcome(case: dict) -> dict:
    options = {key: case["options"].get(key) for key in ("buyer_did", "wallet", "max_usd")}
    try:
        if case["kind"] == "task":
            terms = pay.check_terms(TASK, case.get("task"), case.get("record"), case.get("required"), **options)
        else:
            terms = pay.check_submission_terms(BOUNTY, SUB, case.get("bounty"), case.get("submission"),
                                               case.get("record"), case.get("required"), **options)
    except pay.PaymentCheckError as refused:
        return {"reason": refused.reason, "message": str(refused)}
    typed = pay.typed_data(terms, now=NOW, nonce=NONCE)
    return {"sentence": terms.sentence, "amount": terms.amount, "units": str(terms.amount_units),
            "pay_to": terms.pay_to, "payer": terms.payer, "worker": terms.worker_did, "handle": terms.worker_handle,
            "ids": [terms.task_id, terms.bounty_id, terms.submission_id], "typed": typed,
            "header": pay.payment_header(terms, typed, SIGNATURE)}


CHECK_SCRIPT = """
import { readFileSync } from "node:fs";
import { checkTerms, checkSubmissionTerms, typedData, paymentHeader, PaymentCheckError } from "./src/index.ts";
const { cases, ids: [TASK, BOUNTY, SUB], now, nonce, signature } = JSON.parse(readFileSync(0, "utf8"));
const out = cases.map((c) => {
  const o = { buyerDid: c.options.buyer_did, wallet: c.options.wallet, maxUsd: c.options.max_usd };
  let terms;
  try {
    terms = c.kind === "task"
      ? checkTerms(TASK, c.task, c.record, c.required, o)
      : checkSubmissionTerms(BOUNTY, SUB, c.bounty, c.submission, c.record, c.required, o);
  } catch (refused) {
    if (!(refused instanceof PaymentCheckError)) throw refused;
    return { reason: refused.reason, message: refused.message };
  }
  const typed = typedData(terms, { now, nonce: Uint8Array.from(nonce) });
  return { sentence: terms.sentence, amount: terms.amount, units: terms.amountUnits.toString(),
           pay_to: terms.payTo, payer: terms.payer, worker: terms.workerDid, handle: terms.workerHandle,
           ids: [terms.taskId ?? null, terms.bountyId ?? null, terms.submissionId ?? null], typed,
           header: paymentHeader(terms, typed, signature) };
});
console.log(JSON.stringify(out));
"""


def _run(node: str, script: str, given: object) -> object:
    proc = subprocess.run([node, "--input-type=module", "-e", script], input=json.dumps(given),
                          capture_output=True, text=True, timeout=120, cwd=TS_DIR)
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-4000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _same_message(py: str, ts: str) -> bool:
    """A refusal's words are the same but for the option's name (max_usd, maxUsd)."""
    return py == ts.replace("maxUsd", "max_usd")


def test_ts_checks_a_payment_as_agentx_social_does():
    node = _node()
    if node is None:
        pytest.skip("Node >= 22.18 not available")
    cases = _cases()
    ts = _run(node, CHECK_SCRIPT, {"cases": cases, "ids": [TASK, BOUNTY, SUB], "now": NOW,
                                   "nonce": list(NONCE), "signature": SIGNATURE})
    assert len(ts) == len(cases) > 3000
    reasons, agreed = set(), 0
    for i, (case, t) in enumerate(zip(cases, ts)):
        p = _python_outcome(case)
        what = f"case {i} ({case['kind']}): agentx-social {p} != @agentx-social/sdk {t}\n{json.dumps(case)[:3000]}"
        if "reason" in p:
            assert t.get("reason") == p["reason"] and _same_message(p["message"], t["message"]), what
            reasons.add(p["reason"])
        else:
            assert p == t, what
            agreed += 1
    # The table reaches every reason the checker has, and many payments that agree.
    assert reasons == {
        "no_public_record", "no_public_terms", "worker_differs", "buyer_differs", "not_your_job",
        "unknown_network", "network_differs", "unknown_token", "amount_differs", "payer_is_recipient",
        "wrong_wallet", "wallet_not_in_history", "not_x402_v2", "pay_to_differs", "another_job", "over_your_limit",
    }
    assert agreed > 300
    # The two that agree outright are the payment of §6.4, on both sides.
    first = _python_outcome(cases[0])
    assert first["sentence"].startswith("Pay 12.50 USDC to @worker_002 (0x12aB…89Ef) for task 'Summarise")
    assert json.loads(base64.b64decode(first["header"]))["payload"]["authorization"]["to"] == PAY_TO


# ── The small readers ────────────────────────────────────────────────────────

def _b64(value: object) -> str:
    return base64.b64encode(json.dumps(value).encode()).decode()


HEADERS = [_b64(_base("task")["required"]), _b64(_base("task")["required"]).rstrip("="), _b64({}), _b64([1]), _b64("x"),
           _b64(None), _b64(5), "", "e30", "e30=", "e30==", "e30 =", "!!!!", "{}", "AAAA", "A", "AA", "AAA",
           base64.b64encode(b"\xff\xfe{}").decode(), base64.b64encode(b"{}\n").decode(),
           base64.urlsafe_b64encode(b'{"a":"\xc3\xbf\xc3\xbe?>"}').decode(), _b64({"pad": "x" * 16400})]


def _paid(result: object = RESULT, **more) -> dict:
    said = hashlib.sha256(bytes.fromhex(SALT) + result.encode()).hexdigest() if isinstance(result, str) else "00" * 32
    return {"task_id": TASK, "task_status": "paid", "result": result, "salt": SALT, "result_hash": said, **more}


OPENED = [(_paid(), FINGERPRINT), (_paid(), FINGERPRINT.upper()), (_paid(), None), (_paid("something else"), FINGERPRINT),
          (_paid("something else"), None), (_paid(""), None), (_paid("é" * 5000), None), (_paid(None), None),
          (_paid(5), None), ({**_paid(), "salt": "zz"}, None), ({**_paid(), "salt": "abc"}, None),
          ({**_paid(), "salt": None}, None), ({**_paid(), "salt": SALT.upper(),
                                               "result_hash": FINGERPRINT}, FINGERPRINT),
          ({**_paid(), "result_hash": FINGERPRINT.upper()}, FINGERPRINT), ({**_paid(), "result_hash": None}, None),
          ({}, None), (None, None), ([_paid()], None), ("paid", None),
          ({"result": "", "salt": "", "result_hash": hashlib.sha256(b"").hexdigest()}, None)]

UNITS = [0, 1, 99, 10_000, 12_500_000, 12_345_678, 1_000_000, 10 ** 18 - 1, 5 * 10 ** 17]

READ_SCRIPT = """
import { readFileSync } from "node:fs";
import { decodePaymentRequired, checkResult, formatUnits, shortAddress, PaymentCheckError, ResultMismatch,
         PAY_WORDS, PAY_WORDS_BOUNTY, NO_FUNDS_SENTENCE, MOVES_FUNDS_SENTENCE, TOKENS } from "./src/index.ts";
import { VALID_SECONDS, VALID_FROM_SECONDS_AGO, X402_VERSION, SCHEME, PAYMENT_REQUIRED_HEADER,
         PAYMENT_SIGNATURE_HEADER } from "./src/payments.ts";
const { headers, opened, units, address } = JSON.parse(readFileSync(0, "utf8"));
const decoded = headers.map((h) => {
  try { return { ok: decodePaymentRequired(h) }; }
  catch (e) { if (!(e instanceof PaymentCheckError)) throw e; return { reason: e.reason, message: e.message }; }
});
const results = opened.map(([answer, fingerprint]) => {
  try { checkResult(answer, fingerprint ?? undefined); return "ok"; }
  catch (e) { if (!(e instanceof ResultMismatch)) throw e; return e.message; }
});
console.log(JSON.stringify({
  decoded, results, units: units.map((u) => formatUnits(BigInt(u))), short: shortAddress(address),
  words: [PAY_WORDS, PAY_WORDS_BOUNTY, NO_FUNDS_SENTENCE, MOVES_FUNDS_SENTENCE],
  numbers: [VALID_SECONDS, VALID_FROM_SECONDS_AGO, X402_VERSION, SCHEME, PAYMENT_REQUIRED_HEADER, PAYMENT_SIGNATURE_HEADER],
  tokens: [...TOKENS].map(([network, t]) => [network, t.chainId, t.address, t.name, t.version, t.test]),
}));
"""


def test_ts_reads_headers_results_and_amounts_as_agentx_social_does():
    node = _node()
    if node is None:
        pytest.skip("Node >= 22.18 not available")
    ts = _run(node, READ_SCRIPT, {"headers": HEADERS, "opened": OPENED, "units": [str(u) for u in UNITS],
                                  "address": PAY_TO})

    for value, t in zip(HEADERS, ts["decoded"], strict=True):
        try:
            p = {"ok": pay.decode_payment_required(value)}
        except pay.PaymentCheckError as refused:
            p = {"reason": refused.reason, "message": str(refused)}
        assert p == t, value[:60]
    assert sum("ok" in t for t in ts["decoded"]) == 7      # the request twice, and five spellings of {}

    for (answer, fingerprint), t in zip(OPENED, ts["results"], strict=True):
        try:
            pay.check_result(answer, fingerprint)
            p = "ok"
        except pay.ResultMismatch as mismatch:
            p = str(mismatch)
        assert p == t, json.dumps(answer)[:200]
    assert ts["results"].count("ok") == 8

    assert ts["units"] == [pay.format_units(u) for u in UNITS]
    assert ts["short"] == pay.short_address(PAY_TO)
    # The words, the window and the tokens are the same constants in both.
    assert ts["words"] == [pay.PAY_WORDS, pay.PAY_WORDS_BOUNTY, pay.NO_FUNDS_SENTENCE, pay.MOVES_FUNDS_SENTENCE]
    assert ts["numbers"] == [pay.VALID_SECONDS, pay.VALID_FROM_SECONDS_AGO, pay.X402_VERSION, pay.SCHEME,
                             pay.PAYMENT_REQUIRED_HEADER, pay.PAYMENT_SIGNATURE_HEADER]
    assert ts["tokens"] == [[n, t.chain_id, t.address, t.name, t.version, t.test] for n, t in pay.TOKENS.items()]


# ── The calls ────────────────────────────────────────────────────────────────

CHALLENGE = {"text_to_sign": "Register this wallet as the buyer wallet of @b on AgentX. "
                             "This signature moves no funds.\n\nNonce: 1", "moves_funds": False, "address": PAYER}


def _answers() -> dict:
    """One answer per path; the full-result routes answer in turn (see ``FULL``)."""
    task, sub = _base("task"), _base("submission")
    return {
        f"/tasks/{TASK}/deliveries": [{"delivery_id": "d2", "result_hash": FINGERPRINT},
                                      {"delivery_id": "d1", "result_hash": "00" * 32}],
        f"/tasks/{TASK}": task["task"],
        f"/agents/{WORKER}/payout-wallet": task["record"],
        f"/markets/bounties/{BOUNTY}": sub["bounty"],
        f"/markets/bounties/{BOUNTY}/submissions": [{**sub["submission"], "submission_id": "another"}, sub["submission"]],
        "/agents/me/buyer-wallet/challenge": CHALLENGE,
    }


def _no(error: str, required: dict | None = None, retry_after: int | None = None, status: int = 402) -> dict:
    headers = {}
    if required is not None:
        headers["PAYMENT-REQUIRED"] = _b64(required)
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return {"status": status, "body": {"detail": {"error": error, "message": error}}, "headers": headers}


def _full() -> list[dict]:
    """What the two full-result routes answer, in the order they are asked."""
    ask, ask_sub = _no("payment_required", _base("task")["required"]), _no("payment_required", _base("submission")["required"])
    opened = {"status": 200, "body": _paid(), "headers": {}}
    return [
        ask, _no("settlement_pending", retry_after=4), _no("chain_unavailable", retry_after=10, status=503), opened,  # 1
        opened,                                                                # 2: already paid
        ask, _no("payment_not_accepted", retry_after=240),                     # 3: refused after signing
        _no("payment_on_hold", status=409),                                    # 4: not asked to pay
        ask,                                                                   # 5: terms()
        ask_sub, opened,                                                       # 6: the bounty's winner
        ask_sub,                                                               # 7: submission_terms()
        ask, _no("settlement_pending", retry_after=20),                        # 8: still settling when we stop
        opened,                                                                # 9: send_again
        opened,                                                                # 10: send_submission_again
    ]


def _python_calls() -> dict:
    seen: list[dict] = []
    signed: list[dict] = []
    texts: list[str] = []
    events: list[object] = []
    answers, full = _answers(), iter(_full())

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        seen.append({**_norm(request.method, str(request.url), body), "auth": "authorization" in request.headers,
                     "payment": request.headers.get("PAYMENT-SIGNATURE")})
        if request.url.path in (TASK_PATH, SUB_PATH):
            answer = next(full)
            return httpx.Response(answer["status"], json=answer["body"], headers=answer["headers"])
        return httpx.Response(200, json=answers.get(request.url.path, {"status": "ok", "history": []}))

    def sign(typed: dict) -> str:
        signed.append(copy.deepcopy(typed))
        return SIGNATURE

    def text_sign(text: str) -> str:
        texts.append(text)
        return "0x" + "ab" * 65

    c = AgentXClient(api_key="k", base_url="http://api.test", max_retries=0)
    c._http = httpx.Client(base_url="http://api.test", transport=httpx.MockTransport(handler))
    c.identity = AgentIdentity(agent_did=BUYER, api_key="k")
    p = c.payments
    slept: list[float] = []
    p._sleep = slept.append
    p._clock = lambda: 0.0
    shown: list[str] = []

    def outcome(call) -> None:
        try:
            events.append({"result": call()["result"]})
        except pay.PaymentPending as e:
            events.append({"pending": e.error, "signed": e.signed, "header": e.payment_header})
        except pay.PaymentNotAccepted as e:
            events.append({"not_accepted": e.error, "status": e.status_code, "retry_after": e.retry_after,
                           "signed": e.signed, "header": e.payment_header})

    outcome(lambda: p.pay_and_open(TASK, sign, wallet=PAYER, max_usd="20", show=shown.append, wait_seconds=600))  # 1
    outcome(lambda: p.pay_and_open(TASK, sign))                                                 # 2
    outcome(lambda: p.pay_and_open(TASK, sign))                                                 # 3
    outcome(lambda: p.pay_and_open(TASK, sign))                                                 # 4
    events.append(p.terms(TASK, max_usd="12.50").sentence)                                      # 5
    outcome(lambda: p.pay_and_open_submission(BOUNTY, SUB, sign, show=shown.append))            # 6
    events.append(p.submission_terms(BOUNTY, SUB, wallet=PAYER).sentence)                       # 7
    outcome(lambda: p.pay_and_open(TASK, sign, wait_seconds=5))                                 # 8
    outcome(lambda: p.send_again(TASK, events[-1]["header"]))                                   # 9
    outcome(lambda: p.send_submission_again(BOUNTY, SUB, "c2lnbmVk"))                           # 10
    events.append(p.fingerprint(TASK))
    events.append(p.submission_fingerprint(BOUNTY, SUB))
    p.buyer_wallet_record()
    p.buyer_wallet_record(WORKER)
    p.buyer_wallet_challenge(PAYER)
    p.register_buyer_wallet(PAYER, "0xaa")
    p.register_buyer_wallet_with(PAYER, text_sign)
    p.remove_buyer_wallet()
    return {"seen": seen, "signed": signed, "texts": texts, "events": events, "shown": shown, "slept": slept}


CALLS_SCRIPT = """
import { readFileSync } from "node:fs";
import { HttpClient } from "./src/http.ts";
import { PaymentsApi, PaymentPending, PaymentNotAccepted } from "./src/index.ts";
const { ANSWERS, FULL, TASK, BOUNTY, SUB, PAYER, WORKER, BUYER, SIGNATURE, TASK_PATH, SUB_PATH, NOW } =
  JSON.parse(readFileSync(0, "utf8"));
const seen = [], signed = [], texts = [], events = [], shown = [], slept = [];
let next = 0;
const fetch = async (input, init = {}) => {
  const url = new URL(input);
  const headers = new Headers(init.headers);
  seen.push({ method: init.method ?? "GET", url: input, body: init.body === undefined ? null : JSON.parse(init.body),
              auth: headers.has("Authorization"), payment: headers.get("PAYMENT-SIGNATURE") });
  const date = { Date: new Date(NOW * 1000).toUTCString() };
  if (url.pathname === TASK_PATH || url.pathname === SUB_PATH) {
    const answer = FULL[next++];
    return new Response(JSON.stringify(answer.body), { status: answer.status, headers: { ...date, ...answer.headers } });
  }
  return new Response(JSON.stringify(ANSWERS[url.pathname] ?? { status: "ok", history: [] }), { status: 200, headers: date });
};
const tokens = { accessToken: async () => "k", canRefresh: false, setRefreshFn() {} };
const http = new HttpClient({ baseUrl: "http://api.test", fetch, tokens });
const p = new PaymentsApi(http, () => BUYER, {
  sleep: async (s) => { slept.push(s); }, clock: () => 0, now: () => NOW, log: () => {} });
const sign = async (typed) => { signed.push(structuredClone(typed)); return SIGNATURE; };
const show = (s) => { shown.push(s); };
const outcome = async (call) => {
  try { events.push({ result: (await call()).result }); }
  catch (e) {
    if (e instanceof PaymentPending) events.push({ pending: e.error, signed: e.signed, header: e.paymentHeader ?? null });
    else if (e instanceof PaymentNotAccepted) events.push({ not_accepted: e.error, status: e.status,
      retry_after: e.retryAfter ?? null, signed: e.signed, header: e.paymentHeader ?? null });
    else throw e;
  }
};
await outcome(() => p.payAndOpen(TASK, sign, { wallet: PAYER, maxUsd: "20", show, waitSeconds: 600 }));
await outcome(() => p.payAndOpen(TASK, sign));
await outcome(() => p.payAndOpen(TASK, sign));
await outcome(() => p.payAndOpen(TASK, sign));
events.push((await p.terms(TASK, { maxUsd: "12.50" })).sentence);
await outcome(() => p.payAndOpenSubmission(BOUNTY, SUB, sign, { show }));
events.push((await p.submissionTerms(BOUNTY, SUB, { wallet: PAYER })).sentence);
await outcome(() => p.payAndOpen(TASK, sign, { waitSeconds: 5 }));
await outcome(() => p.sendAgain(TASK, events[events.length - 1].header));
await outcome(() => p.sendSubmissionAgain(BOUNTY, SUB, "c2lnbmVk"));
events.push(await p.fingerprint(TASK));
events.push(await p.submissionFingerprint(BOUNTY, SUB));
await p.buyerWalletRecord();
await p.buyerWalletRecord(WORKER);
await p.buyerWalletChallenge(PAYER);
await p.registerBuyerWallet(PAYER, "0xaa");
await p.registerBuyerWalletWith(PAYER, async (text) => { texts.push(text); return "0x" + "ab".repeat(65); });
await p.removeBuyerWallet();
console.log(JSON.stringify({ seen, signed, texts, events, shown, slept }));
"""


def _without_nonce(value: object) -> object:
    """A payment with its random nonce taken out: a typed-data dict, or a header."""
    if isinstance(value, dict) and "message" in value:
        assert len(value["message"]["nonce"]) == 66
        return {**value, "message": {**value["message"], "nonce": "(random)"}}
    if isinstance(value, str) and value != "c2lnbmVk":
        sent = json.loads(base64.b64decode(value))
        assert len(sent["payload"]["authorization"]["nonce"]) == 66
        sent["payload"]["authorization"]["nonce"] = "(random)"
        return sent
    return value


def test_ts_pays_and_opens_with_the_requests_agentx_social_sends(monkeypatch):
    node = _node()
    if node is None:
        pytest.skip("Node >= 22.18 not available")
    monkeypatch.setattr(pay.time, "time", lambda: NOW)
    py = _python_calls()
    raw = _run(node, CALLS_SCRIPT, {
        "ANSWERS": _answers(), "FULL": _full(), "TASK": TASK, "BOUNTY": BOUNTY, "SUB": SUB, "PAYER": PAYER,
        "WORKER": WORKER, "BUYER": BUYER, "SIGNATURE": SIGNATURE, "TASK_PATH": TASK_PATH, "SUB_PATH": SUB_PATH, "NOW": NOW})
    ts = {**raw, "seen": [{**_norm(r["method"], r["url"], r["body"]), "auth": r["auth"], "payment": r["payment"]}
                          for r in raw["seen"]]}

    assert len(py["seen"]) == len(ts["seen"]) == 47
    for i, (p, t) in enumerate(zip(py["seen"], ts["seen"])):
        p, t = ({**r, "payment": _without_nonce(r["payment"])} for r in (p, t))
        assert p == t, f"call {i}: agentx-social {p} != @agentx-social/sdk {t}"
    assert all(r["auth"] for r in py["seen"])

    # The wallet was asked four times on each side (calls 1, 3, 6 and 8), for the same payment.
    assert len(py["signed"]) == len(ts["signed"]) == 4
    assert [_without_nonce(s) for s in py["signed"]] == [_without_nonce(s) for s in ts["signed"]]
    assert py["signed"][0]["message"]["validBefore"] == str(NOW + 300)
    nonces = [s["message"]["nonce"] for s in py["signed"] + ts["signed"]]
    assert len(set(nonces)) == 8

    assert [_without_nonce(e.get("header")) if isinstance(e, dict) else e for e in py["events"]] == \
           [_without_nonce(e.get("header")) if isinstance(e, dict) else e for e in ts["events"]]
    assert [{k: v for k, v in e.items() if k != "header"} if isinstance(e, dict) else e for e in py["events"]] == \
           [{k: v for k, v in e.items() if k != "header"} if isinstance(e, dict) else e for e in ts["events"]]
    assert [next(iter(e)) if isinstance(e, dict) else "text" for e in py["events"]] == [
        "result", "result", "not_accepted", "not_accepted", "text", "result", "text", "pending", "result", "result",
        "text", "text"]
    assert py["shown"] == ts["shown"] and len(py["shown"]) == 2 and "for bounty '" in py["shown"][1]
    assert py["slept"] == ts["slept"] == [4.0, 10.0]
    assert py["texts"] == ts["texts"] == [CHALLENGE["text_to_sign"]]
