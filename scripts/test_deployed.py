"""Re-test a USER-DEPLOYED DisputeArbiter on studionet (no deploy step).

Tests every public method against an existing contract address with real
consensus, web and LLM:

  create_dispute, deposit, adjudicate, withdraw_payout, request_settle/settle,
  cancel/refund, and access-control rejections for an outsider.

The deployer's key is NOT needed (and never handled). The owner-only
`withdraw_protocol_fees` is therefore only verified via its access-control
rejection; the accrued fee is reported so the owner can withdraw it themselves.

Usage:
    python scripts/test_deployed.py --address 0x...
"""

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from genlayer_py import create_client, create_account
from genlayer_py.chains import studionet
from genlayer_py.types.transactions import TransactionStatus
from gltest.assertions import tx_execution_succeeded

STUDIO_API = studionet.rpc_urls["default"]["http"][0]

CLAIM_URL = "https://raw.githubusercontent.com/genlayerlabs/genlayer-py/main/README.md"
RESP_URL = "https://raw.githubusercontent.com/genlayerlabs/genlayer-py/main/LICENSE"
DEPOSIT = 1_000_000  # wei
FEE_BPS = 500


def rpc(method, params, _retries=6):
    for attempt in range(_retries):
        resp = requests.post(STUDIO_API, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=60)
        if resp.status_code == 429:
            time.sleep(10 * (attempt + 1))
            continue
        resp.raise_for_status()
        body = resp.json()
        if "error" in body:
            raise RuntimeError(f"{method} error: {body['error']}")
        return body.get("result")
    raise RuntimeError(f"{method}: rate limited after {_retries} retries")


def rate_retry(fn, *args, **kwargs):
    """Retry a client call when studionet rate-limits (30 req/min)."""
    while True:
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            msg = str(e)
            if "rate limit" in msg.lower() or "Rate limit" in msg or "-32029" in msg:
                print("  rate limited; waiting 65s...")
                time.sleep(65)
                continue
            raise


def fund(account, amount=10 ** 21):
    rpc("sim_fundAccount", [account.address, amount])
    time.sleep(3)


def evidence(url):
    raw = requests.get(url, timeout=30).content
    return json.dumps([{"url": url, "hash": hashlib.sha256(raw).hexdigest()}])


def send_tx(client, tx_hash, label, interval=6000, retries=90):
    def _wait():
        return client.wait_for_transaction_receipt(
            tx_hash, status=TransactionStatus.ACCEPTED, interval=interval, retries=retries
        )

    receipt = rate_retry(_wait)
    ok = tx_execution_succeeded(receipt)
    print(f"  {label}: tx={tx_hash} success={ok}")
    if not ok:
        leader = receipt["consensus_data"]["leader_receipt"][0]
        print(f"    stderr: {leader.get('genvm_result', {}).get('stderr', '')[:300]}")
    time.sleep(3)
    return ok


def wait_balance(address, target, label, timeout_s=180, poll_s=15):
    deadline = time.time() + timeout_s
    last = 0
    while time.time() < deadline:
        last = int(rpc("eth_getBalance", [address, "latest"]), 16)
        if last >= target:
            print(f"  {label}: balance={last} (target {target}) OK")
            return last
        time.sleep(poll_s)
    print(f"  {label}: balance={last} but expected >= {target} TIMEOUT")
    return last


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--address", required=True, help="Deployed DisputeArbiter address")
    args = parser.parse_args()
    addr = args.address

    print(f"=== Testing DisputeArbiter at {addr} ===")
    print(f"    explorer: https://explorer-studio.genlayer.com/address/{addr}")

    alice = create_account()
    bob = create_account()
    charlie = create_account()
    for acct in (alice, bob, charlie):
        fund(acct)

    client = create_client(chain=studionet, account=alice)
    read = lambda fn, *a, **kw: rate_retry(client.read_contract, addr, fn, args=list(a), **kw)
    write = lambda fn, account, args=None, value=0: rate_retry(client.write_contract, addr, fn, account=account, args=args or [], value=value)

    # The owner is the user's deployer address (read-only, we never hold its key).
    owner = read("get_owner")
    print(f"\n[0] get_owner = {owner}  (deployer's address)")

    # Sequential ids from d0: the next new dispute id = d{count of existing}.
    existing = read("get_disputes") or {}
    base = len(existing)
    print(f"    existing disputes: {sorted(existing.keys())} -> next ids start at d{base}")

    # ---- create + fund + adjudicate + withdraw (dispute d{base}) ----
    print(f"\n[1] create_dispute / deposit / adjudicate / withdraw_payout (d{base})")
    d_main = f"d{base}"
    args = [
        bob.address,
        "Repayment of an acknowledged loan",
        "I lent the respondent 100 GEN and the respondent acknowledged the debt in writing.",
        "I acknowledge the debt of 100 GEN to the claimant.",
        "Rule for the claimant if the respondent acknowledges the debt; otherwise rule for the respondent.",
        evidence(CLAIM_URL),
        evidence(RESP_URL),
        DEPOSIT,
        FEE_BPS,
    ]
    assert send_tx(client, write("create_dispute", alice, args=args), "create_dispute")
    assert send_tx(client, write("deposit", alice, args=[d_main], value=DEPOSIT), "deposit alice")
    assert send_tx(client, write("deposit", bob, args=[d_main], value=DEPOSIT), "deposit bob")
    dispute = read("get_dispute", d_main)
    assert dispute["status"] == "funded", f"expected funded, got {dispute['status']}"
    print(f"    status={dispute['status']} deposits={dispute['claimant_deposit']}/{dispute['respondent_deposit']}")

    # ---- staff-fix check: recovery timeout configured + guard before window ----
    print("\n[1b] emergency_withdraw (staff fix) — bounded recovery timeout")
    rt = read("get_recovery_timeout")
    print(f"    get_recovery_timeout = {rt}")
    assert rt > 0, "recovery timeout must be positive"
    assert not send_tx(client, write("emergency_withdraw", alice, args=[d_main]), "emergency_withdraw early")
    print("    guard OK: emergency_withdraw rejected before the recovery window opens")

    assert send_tx(client, write("adjudicate", charlie, args=[d_main]), "adjudicate", retries=120)
    dispute = read("get_dispute", d_main)
    assert dispute["status"] == "adjudicated", f"adjudicate failed: {dispute['status']}"
    winner = alice if dispute["claimant_payout"] > 0 else bob
    payout = dispute["claimant_payout"] if winner is alice else dispute["respondent_payout"]
    print(f"    verdict={dispute['verdict']} confidence={dispute['confidence']} references={dispute['references']}")

    winner_bal0 = int(rpc("eth_getBalance", [winner.address, "latest"]), 16)
    assert send_tx(client, write("withdraw_payout", winner, args=[d_main]), "withdraw_payout")
    winner_bal1 = wait_balance(winner.address, winner_bal0 + payout, "winner balance after payout")
    assert winner_bal1 >= winner_bal0 + payout, "winner did not receive the payout!"
    print(f"    fund transfer CONFIRMED: contract -> winner EOA (+{winner_bal1 - winner_bal0})")

    # ---- access control: only owner can withdraw fees ----
    fee = read("get_protocol_fees")
    print(f"\n[2] get_protocol_fees = {fee}  (owner {owner} can withdraw it with their own key)")
    assert not send_tx(client, write("withdraw_protocol_fees", charlie, args=[]), "outsider fee withdrawal")
    print("    access control OK: non-owner fee withdrawal rejected")

    # ---- mutual settlement (dispute d{base+1}) ----
    print(f"\n[3] request_settle / settle (d{base+1})")
    d_settle = f"d{base+1}"
    args = [
        bob.address,
        "Ambiguous delivery dispute",
        "I delivered the work on Friday.",
        "The work was delivered on Monday.",
        "Rule for the claimant if delivery happened before the weekend; otherwise respondent.",
        evidence(RESP_URL),
        evidence(CLAIM_URL),
        DEPOSIT,
        FEE_BPS,
    ]
    assert send_tx(client, write("create_dispute", alice, args=args), "create d_settle")
    assert send_tx(client, write("deposit", alice, args=[d_settle], value=DEPOSIT), "dep alice")
    assert send_tx(client, write("deposit", bob, args=[d_settle], value=DEPOSIT), "dep bob")
    assert send_tx(client, write("request_settle", alice, args=[d_settle]), "req_settle alice")
    assert send_tx(client, write("request_settle", bob, args=[d_settle]), "req_settle bob")
    assert send_tx(client, write("settle", bob, args=[d_settle]), "settle")
    dispute = read("get_dispute", d_settle)
    assert dispute["status"] == "adjudicated" and dispute["verdict"] == "split"
    assert dispute["claimant_payout"] == DEPOSIT and dispute["respondent_payout"] == DEPOSIT
    print(f"    status={dispute['status']} verdict={dispute['verdict']} payouts={dispute['claimant_payout']}/{dispute['respondent_payout']} (no fee)")

    # ---- cancel + refund (dispute d{base+2}) ----
    print(f"\n[4] cancel / refund (d{base+2})")
    d_cancel = f"d{base+2}"
    args = [
        bob.address,
        "Dispute that never proceeds",
        "Statement from claimant.",
        "Statement from respondent.",
        "Any reasonable rule.",
        evidence(CLAIM_URL),
        evidence(RESP_URL),
        DEPOSIT,
        FEE_BPS,
    ]
    assert send_tx(client, write("create_dispute", alice, args=args), "create d_cancel")
    assert send_tx(client, write("deposit", alice, args=[d_cancel], value=DEPOSIT), "dep alice")
    assert send_tx(client, write("cancel", bob, args=[d_cancel]), "cancel")
    assert send_tx(client, write("refund", alice, args=[d_cancel]), "refund")
    dispute = read("get_dispute", d_cancel)
    assert dispute["status"] == "cancelled" and dispute["claimant_withdrawn"] is True
    print(f"    status={dispute['status']} claimant_withdrawn={dispute['claimant_withdrawn']}")

    # ---- final summary ----
    print("\n=== ALL METHODS RE-TESTED SUCCESSFULLY ===")
    print(f"contract: {addr}")
    print(f"owner (withdraw fees with your key): {owner}")
    print(f"accrued protocol fees: {read('get_protocol_fees')}")
    print(f"disputes now: {sorted((read('get_disputes') or {}).keys())}")


if __name__ == "__main__":
    main()
