"""``agentx-pay``: paying to open from the command line, with the wallet
somewhere else (payments v1 §4.7, §6.4; plan step W2-12a3).

The command holds no key. ``check`` signs and sends nothing; ``send`` sends a
signature only for a file that is still exactly the payment AgentX asks for.
"""

import copy
import io
import json
import pathlib

import httpx
import pytest
import respx

from agentx_sdk import pay_cli as cli
from agentx_sdk import payments as pay

from . import test_bounty_payments as bounties
from .test_payments import (
    BASE, FINGERPRINT, FULL, PAY_TO, PAYER, RESULT, TASK, THIEF, WORKER, client, entry, no, paid, sent_headers, task,
    the_job, wallet_record,
)

SIGNATURE = "0x" + "5a" * 65


def run(*argv, me=None) -> tuple[int, str]:
    out = io.StringIO()
    return cli.main([str(a) for a in argv], client=me or client(), out=out), out.getvalue()


@pytest.fixture
def filed(tmp_path) -> pathlib.Path:
    """A payment file as ``check`` writes it for the task."""
    path = tmp_path / "payment.json"
    with respx.mock:
        the_job(no(402, "payment_required", terms=True))
        assert run("check", "--task", TASK, "--out", path)[0] == 0
    return path


def changed(path: pathlib.Path, change) -> pathlib.Path:
    data = json.loads(path.read_text())
    change(data)
    path.write_text(json.dumps(data))
    return path


# ── check ────────────────────────────────────────────────────────────────────

@respx.mock
def test_check_says_the_sentence_writes_the_file_and_sends_no_payment(tmp_path):
    full = the_job(no(402, "payment_required", terms=True))
    path = tmp_path / "payment.json"
    code, said = run("check", "--task", TASK, "--out", path, "--wallet", PAYER, "--max-usd", "12.50")
    assert code == 0
    assert said.startswith("Pay 12.50 USDC to @worker_002 (0x12aB…89Ef) for task 'Summarise the Q3 report'. "
                           "This moves funds.")
    assert "Nothing was signed or sent." in said and "0x1111…1111" in said
    assert sent_headers(full) == [None]
    data = json.loads(path.read_text())
    assert data["kind"] == "agentx-payment" and data["job"] == {"task_id": TASK} and data["fingerprint"] == FINGERPRINT
    assert data["sentence"] == said.splitlines()[0]
    typed = data["typed_data"]
    assert typed["primaryType"] == "TransferWithAuthorization"
    assert (typed["message"]["from"], typed["message"]["to"], typed["message"]["value"]) == (PAYER, PAY_TO, "12500000")
    assert "signature" not in json.dumps(data).lower().replace("payment-signature", "")


@respx.mock
def test_check_for_a_bounty(tmp_path):
    bounties.the_bounty(bounties.no(402, "payment_required", terms=True))
    path = tmp_path / "payment.json"
    code, said = run("check", "--bounty", bounties.BOUNTY, "--submission", bounties.SUB, "--out", path)
    assert code == 0 and "for bounty 'Find the sources'" in said
    data = json.loads(path.read_text())
    assert data["job"] == {"bounty_id": bounties.BOUNTY, "submission_id": bounties.SUB}
    job, ready = cli.from_file(data)
    assert job.path.endswith(f"/submissions/{bounties.SUB}/full") and ready.terms.bounty_id == bounties.BOUNTY


@respx.mock
@pytest.mark.parametrize("a_task, record, more, reason", [
    (task(pay_to=THIEF), wallet_record(entry(address=THIEF)), [], "pay_to_differs"),
    (None, wallet_record(entry(address=THIEF)), [], "wallet_not_in_history"),
    (None, None, ["--wallet", THIEF], "wrong_wallet"),
    (None, None, ["--max-usd", "1"], "over_your_limit"),
])
def test_check_writes_no_file_when_the_records_disagree(tmp_path, capsys, a_task, record, more, reason):
    the_job(no(402, "payment_required", terms=True), a_task=a_task, record=record)
    path = tmp_path / "payment.json"
    code, said = run("check", "--task", TASK, "--out", path, *more)
    assert code == cli.REFUSED and said == "" and not path.exists()
    assert f"({reason})" in capsys.readouterr().err


@respx.mock
def test_check_on_a_paid_job_gives_the_result_and_no_file(tmp_path):
    the_job(httpx.Response(200, json=paid()))
    path = tmp_path / "payment.json"
    code, said = run("check", "--task", TASK, "--out", path)
    assert code == 0 and json.loads(said)["result"] == RESULT and not path.exists()


@respx.mock
def test_check_when_agentx_asks_for_no_payment(tmp_path, capsys):
    the_job(no(409, "nothing_to_pay_for"))
    code, _ = run("check", "--task", TASK, "--out", tmp_path / "p.json")
    assert code == cli.REFUSED and "nothing_to_pay_for" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [
    ["check", "--out", "p.json"],
    ["check", "--task", TASK, "--bounty", bounties.BOUNTY, "--submission", bounties.SUB, "--out", "p.json"],
    ["check", "--bounty", bounties.BOUNTY, "--out", "p.json"],
    ["check", "--task", "../x", "--out", "p.json"],
    ["check", "--bounty", bounties.BOUNTY, "--submission", "a/b", "--out", "p.json"],
])
def test_check_needs_one_job(argv, capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with respx.mock(assert_all_called=False) as mock:
        anything = mock.route().mock(return_value=httpx.Response(200, json={}))
        assert run(*argv)[0] == cli.REFUSED and not anything.called
    assert "(no_job)" in capsys.readouterr().err and not (tmp_path / "p.json").exists()


# ── send ─────────────────────────────────────────────────────────────────────

@respx.mock
def test_send_checks_again_then_sends_the_payment_with_the_signature(filed):
    full = the_job(no(402, "payment_required", terms=True), httpx.Response(200, json=paid()))
    code, said = run("send", filed, "--signature", SIGNATURE)
    assert code == 0 and json.loads(said)["result"] == RESULT
    first, second = sent_headers(full)
    assert first is None
    sent = json.loads(pay.base64.b64decode(second))
    data = json.loads(filed.read_text())
    assert sent["payload"] == {"signature": SIGNATURE, "authorization": data["typed_data"]["message"]}
    assert sent["accepted"] == data["terms"]["accepted"]


@respx.mock
def test_send_takes_the_signature_from_a_file(filed, tmp_path):
    full = the_job(no(402, "payment_required", terms=True), httpx.Response(200, json=paid()))
    (tmp_path / "sig").write_text(SIGNATURE + "\n")
    assert run("send", filed, "--signature-file", tmp_path / "sig")[0] == 0
    assert json.loads(pay.base64.b64decode(sent_headers(full)[1]))["payload"]["signature"] == SIGNATURE


@respx.mock
@pytest.mark.parametrize("a_task, record, reason", [
    (task(pay_to=THIEF), wallet_record(entry(address=THIEF)), "pay_to_differs"),     # AgentX's records now disagree
    (None, wallet_record(entry(address=THIEF)), "wallet_not_in_history"),
])
def test_send_sends_no_signature_when_the_records_disagree_now(filed, capsys, a_task, record, reason):
    full = the_job(no(402, "payment_required", terms=True), a_task=a_task, record=record)
    assert run("send", filed, "--signature", SIGNATURE)[0] == cli.REFUSED
    assert f"({reason})" in capsys.readouterr().err and sent_headers(full) == [None]


def to_thief(data: dict) -> None:
    data["typed_data"]["message"]["to"] = THIEF


def to_thief_throughout(data: dict) -> None:
    to_thief(data)
    data["terms"]["pay_to"] = THIEF
    data["terms"]["accepted"]["payTo"] = THIEF


@respx.mock
@pytest.mark.parametrize("change, reason", [
    (to_thief, "unreadable_file"),                                   # the typed data alone
    (to_thief_throughout, "terms_changed"),                          # the terms with it: not AgentX's of now
    (lambda d: d["typed_data"]["message"].update(value="99000000"), "unreadable_file"),
    (lambda d: d["terms"].update(amount="99000000"), "unreadable_file"),
    (lambda d: d["typed_data"].update(primaryType="Permit"), "unreadable_file"),
    (lambda d: d["typed_data"]["types"].update(Permit=[]), "unreadable_file"),
    (lambda d: d["typed_data"]["domain"].update(verifyingContract=THIEF), "unreadable_file"),
    (lambda d: d["typed_data"]["domain"].update(chainId=8453), "unreadable_file"),
    (lambda d: d["typed_data"]["message"].update(validBefore="9999999999"), "unreadable_file"),
    (lambda d: d["typed_data"]["message"].update(nonce="0x00"), "unreadable_file"),
    (lambda d: d["terms"].update(network="eip155:1"), "unreadable_file"),
    (lambda d: d["terms"].update(network="eip155:8453"), "unreadable_file"),      # the other network's token
    (lambda d: d["terms"]["accepted"].update(amount="1"), "terms_changed"),
    (lambda d: d["terms"].update(resource=None), "terms_changed"),
    (lambda d: d["terms"].update(worker_did="did:agentx:someone-003"), "terms_changed"),
    (lambda d: d.update(fingerprint="00" * 32), "delivery_changed"),
    (lambda d: d.update(fingerprint="nope"), "unreadable_file"),
    (lambda d: d.update(job={"task_id": "../x"}), "unreadable_file"),
    (lambda d: d.update(job={"task_id": TASK, "bounty_id": "b"}), "unreadable_file"),
    (lambda d: d.update(job={"task_id": 7}), "unreadable_file"),
    (lambda d: d.update(version=2), "unreadable_file"),
    (lambda d: d.update(kind="something"), "unreadable_file"),
    (lambda d: d.pop("typed_data"), "unreadable_file"),
    (lambda d: d.clear(), "unreadable_file"),
])
def test_send_sends_no_signature_for_a_file_that_was_changed(filed, capsys, change, reason):
    full = the_job(no(402, "payment_required", terms=True), httpx.Response(200, json=paid()))
    assert run("send", changed(filed, change), "--signature", SIGNATURE)[0] == cli.REFUSED
    assert f"({reason})" in capsys.readouterr().err
    assert all(h is None for h in sent_headers(full))


@respx.mock
@pytest.mark.parametrize("change", [
    dict(pay_to=THIEF), dict(payer=THIEF), dict(amount_units=1), dict(worker_did="did:agentx:someone-003"),
    dict(network="eip155:8453", token=pay.TOKENS["eip155:8453"]),
])
def test_send_sends_no_signature_for_a_well_formed_file_of_other_terms(filed, capsys, change):
    # A file as `check` would have written it for another payment of the same job: only
    # AgentX's records of now tell it apart. `accepted` is left as AgentX sent it.
    import dataclasses
    _, ready = cli.from_file(json.loads(filed.read_text()))
    terms = dataclasses.replace(ready.terms, **change)
    message = ready.typed["message"]
    typed = pay.typed_data(terms, now=int(message["validBefore"]) - pay.VALID_SECONDS,
                           nonce=bytes.fromhex(message["nonce"][2:]))
    filed.write_text(json.dumps(cli.to_file(pay.Unsigned(terms=terms, typed=typed, fingerprint=FINGERPRINT))))
    cli.from_file(json.loads(filed.read_text()))                     # it reads back: nothing odd in it
    full = the_job(no(402, "payment_required", terms=True), httpx.Response(200, json=paid()))
    assert run("send", filed, "--signature", SIGNATURE)[0] == cli.REFUSED
    assert "(terms_changed)" in capsys.readouterr().err and sent_headers(full) == [None]


@respx.mock
def test_send_for_another_job_than_the_files_is_refused(filed, capsys):
    # The file is repointed at another task whose records are honest: its terms are not the file's.
    other = "9d2c1f6e-0000-4000-8000-00000000eeee"
    respx.get(f"{BASE}/tasks/{other}/result/full").mock(return_value=no(402, "payment_required", terms=True))
    respx.get(f"{BASE}/tasks/{other}").mock(return_value=httpx.Response(200, json={**task(), "task_id": other}))
    respx.get(f"{BASE}/agents/{WORKER}/payout-wallet").mock(return_value=httpx.Response(200, json=wallet_record()))
    assert run("send", changed(filed, lambda d: d.update(job={"task_id": other})), "--signature", SIGNATURE)[0] \
        == cli.REFUSED
    assert "(another_job)" in capsys.readouterr().err


@respx.mock
def test_send_refuses_a_payment_that_has_run_out_of_time(filed, capsys, monkeypatch):
    full = the_job(no(402, "payment_required", terms=True))
    ends = int(json.loads(filed.read_text())["typed_data"]["message"]["validBefore"])
    monkeypatch.setattr(cli, "_server_time", lambda answer: ends - cli.MIN_SECONDS_LEFT + 1)
    assert run("send", filed, "--signature", SIGNATURE)[0] == cli.REFUSED
    assert "(payment_expired)" in capsys.readouterr().err and sent_headers(full) == [None]


@respx.mock
@pytest.mark.parametrize("answer", ["payment_required", "settlement_pending"])
def test_send_passes_on_no_signature_for_a_payment_that_ends_later_than_check_writes(filed, capsys, answer):
    # W2-16: the file's window is moved as a whole (ten years on), so the typed data is still
    # "the payment of its terms" and reads back. `check` never writes it: a signature made
    # over it stays here, also while a payment for the job is settling.
    def later(d):
        message = d["typed_data"]["message"]
        for bound in ("validAfter", "validBefore"):
            message[bound] = str(int(message[bound]) + 10 * 365 * 86400)
    moved = changed(filed, later)
    cli.from_file(json.loads(moved.read_text()))                     # it reads back
    full = the_job(no(402, answer, terms=True), httpx.Response(200, json=paid()))
    assert run("send", moved, "--signature", SIGNATURE)[0] == cli.REFUSED
    assert "(payment_window_wrong)" in capsys.readouterr().err and sent_headers(full) == [None]


@respx.mock
@pytest.mark.parametrize("signature", ["", "sig", "0x", "0xabc", "0x" + "zz" * 65])
def test_send_refuses_a_signature_that_is_not_hex(filed, capsys, signature):
    full = the_job(no(402, "payment_required", terms=True))
    assert run("send", filed, f"--signature={signature}")[0] == cli.REFUSED
    assert "(unreadable_signature)" in capsys.readouterr().err and not full.called


@respx.mock
def test_send_while_the_payment_is_settling_sends_the_same_one_again(filed):
    # The first send's answer was lost: AgentX has the payment and is settling it.
    full = the_job(no(402, "settlement_pending", terms=True, retry_after=2),
                   no(402, "settlement_pending", terms=True, retry_after=2), httpx.Response(200, json=paid()))
    code, said = run("send", filed, "--signature", SIGNATURE)
    assert code == 0 and json.loads(said)["result"] == RESULT
    first, second, third = sent_headers(full)
    assert first is None and second == third is not None


@respx.mock
def test_send_on_a_paid_job_gives_the_result_without_asking_again(filed):
    full = the_job(httpx.Response(200, json=paid()))
    code, said = run("send", filed, "--signature", SIGNATURE)
    assert code == 0 and json.loads(said)["result"] == RESULT and sent_headers(full) == [None]


@respx.mock
@pytest.mark.parametrize("answer", [
    no(409, "nothing_to_pay_for"), no(409, "payment_on_hold"), no(403, "not_your_job"),
    no(402, "earlier_payment_can_still_settle", terms=True, retry_after=200), no(503, "payments_off", retry_after=60),
    no(402, "payment_not_accepted", terms=True),
])
def test_send_sends_no_signature_when_the_job_cannot_be_paid_for_now(filed, capsys, answer):
    full = the_job(answer)
    assert run("send", filed, "--signature", SIGNATURE)[0] == cli.REFUSED
    assert "AgentX did not accept it" in capsys.readouterr().err and sent_headers(full) == [None]


@respx.mock
def test_send_that_does_not_settle_in_time_says_do_not_sign_another(filed, capsys):
    the_job(no(402, "payment_required", terms=True), no(402, "settlement_pending", terms=True, retry_after=20))
    me = client()
    ticks = iter(range(0, 1000, 15))
    me.payments._clock = lambda: next(ticks)
    assert run("send", filed, "--signature", SIGNATURE, "--wait", "10", me=me)[0] == cli.PENDING
    assert "Do NOT sign another" in capsys.readouterr().err


@respx.mock
def test_send_refused_by_agentx_and_a_result_that_does_not_match(filed, capsys):
    the_job(no(402, "payment_required", terms=True), no(402, "invalid_signature", terms=True))
    assert run("send", filed, "--signature", SIGNATURE)[0] == cli.NOT_ACCEPTED
    assert "invalid_signature" in capsys.readouterr().err
    respx.get(FULL).mock(side_effect=[no(402, "payment_required", terms=True),
                                      httpx.Response(200, json=paid("something else"))])
    code, said = run("send", filed, "--signature", SIGNATURE)
    assert code == cli.MISMATCH and json.loads(said)["result"] == "something else"
    assert "The payment went through" in capsys.readouterr().err


def test_send_of_a_file_that_is_not_there_or_not_json(tmp_path, capsys):
    assert run("send", tmp_path / "none.json", "--signature", SIGNATURE)[0] == cli.REFUSED
    (tmp_path / "bad.json").write_text("{")
    assert run("send", tmp_path / "bad.json", "--signature", SIGNATURE)[0] == cli.REFUSED
    (tmp_path / "big.json").write_text(" " * 70_000)
    assert run("send", tmp_path / "big.json", "--signature", SIGNATURE)[0] == cli.REFUSED
    assert capsys.readouterr().err.count("(unreadable_file)") == 3


# ── The file, and what the command is ────────────────────────────────────────

@respx.mock
def test_the_file_reads_back_as_the_same_payment(filed):
    the_job(no(402, "payment_required", terms=True))
    data = json.loads(filed.read_text())
    job, ready = cli.from_file(copy.deepcopy(data))
    assert job.task_id == TASK and ready.typed == data["typed_data"] and ready.fingerprint == FINGERPRINT
    assert ready.terms.token is pay.TOKENS["eip155:84532"] and ready.terms.sentence == data["sentence"]
    assert cli.to_file(ready) == data


def test_the_command_takes_no_key_and_no_token_on_its_command_line():
    options = {flag for step in ("check", "send")
               for action in cli._parser()._subparsers._group_actions[0].choices[step]._actions
               for flag in action.option_strings}
    assert options == {"-h", "--help", "--task", "--bounty", "--submission", "--out", "--wallet", "--max-usd",
                       "--identity", "--base-url", "--signature", "--signature-file", "--wait"}
    source = pathlib.Path(cli.__file__).read_text()
    for word in ("eth_account", "private_key", "mnemonic", "seed phrase", "sign_typed_data"):
        assert word not in source
    # It builds nothing a wallet signs by itself: the typed data is payments.typed_data's.
    assert "TransferWithAuthorization" not in source


def test_signing_in(monkeypatch, capsys, tmp_path):
    monkeypatch.delenv("AGENTX_TOKEN", raising=False)
    out = io.StringIO()
    assert cli.main(["check", "--task", TASK, "--out", str(tmp_path / "p.json")], out=out) == cli.REFUSED
    assert cli.main(["check", "--task", TASK, "--out", str(tmp_path / "p.json"), "--identity",
                     str(tmp_path / "none.json")], out=out) == cli.REFUSED
    assert capsys.readouterr().err.count("(no_login)") == 2
    monkeypatch.setenv("AGENTX_TOKEN", "tok")
    monkeypatch.setenv("AGENTX_BASE_URL", BASE)
    with respx.mock:
        full = the_job(no(402, "payment_required", terms=True))
        assert cli.main(["check", "--task", TASK, "--out", str(tmp_path / "p.json")], out=out) == 0
        assert full.calls[0].request.headers["Authorization"] == "Bearer tok"


def test_it_is_installed_as_agentx_pay():
    import tomllib
    project = tomllib.loads((pathlib.Path(cli.__file__).parents[1] / "pyproject.toml").read_text())["project"]
    assert project["scripts"] == {"agentx-pay": "agentx_sdk.pay_cli:main"}
