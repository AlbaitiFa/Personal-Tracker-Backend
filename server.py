#!/usr/bin/env python3
"""
Minimal standalone backend for the "I Am Money — Personal Tracker" page.
Zero dependencies - Python 3's standard library only. Run with:

    SYNC_TOKEN=your-long-random-secret \
    UPSTASH_REDIS_REST_URL=... UPSTASH_REDIS_REST_TOKEN=... \
    python3 server.py

It does two jobs:

1. Cloud save for the tracker HTML page itself:
     GET  /api/state   -> the full app state (same shape the app used to
                           keep in localStorage)
     PUT  /api/state   -> merge-write the full app state

2. An MCP server (Streamable HTTP, JSON-RPC 2.0) so Claude can log Albaiti's
   personal transactions during normal chat (e.g. for Prudy's backfilling):
     POST /mcp

Both require `Authorization: Bearer <SYNC_TOKEN>`. The HTML page embeds
this same token (see the sync block near the top of Personal-tracker.html) -
paste it into Claude's connector setup as the Authorization header when
adding /mcp as a custom connector.

Storage is a single key in an Upstash Redis database (free tier, REST API,
no SDK needed) - a host like Render rebuilds the filesystem from scratch on
every deploy, which silently wipes a local file. This deliberately reuses
the SAME Upstash database as the company tracker (company-tracker-backend),
just under a different key (STATE_KEY below), so no second database needs
provisioning - two more env vars, UPSTASH_REDIS_REST_URL and
UPSTASH_REDIS_REST_TOKEN, copy-pasteable from that same Upstash console
page.

NOTE ON AUTH: same MVP tradeoff as the company tracker - SYNC_TOKEN is a
shared secret embedded in client-side HTML, acceptable for a small trusted
team, not real per-user auth.
"""

import json
import math
import os
import random
import string
import sys
import time
import urllib.request
import urllib.error
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", 8788))
TRACKER_HTML_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Personal-tracker.html")
TOKEN = os.environ.get("SYNC_TOKEN")
UPSTASH_URL = os.environ.get("UPSTASH_REDIS_REST_URL")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN")
STATE_KEY = "personal_tracker_state"

if not TOKEN:
    print("Set SYNC_TOKEN before starting, e.g.:")
    print("  SYNC_TOKEN=$(python3 -c \"import secrets; print(secrets.token_hex(32))\") python3 server.py")
    sys.exit(1)

if not UPSTASH_URL or not UPSTASH_TOKEN:
    print("Set UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN before starting")
    print("(same values as the company tracker's Upstash database - this reuses it under a different key).")
    sys.exit(1)

# ---- state storage (Upstash Redis REST API) -----------------------------

def _upstash_request(method, path, body=None):
    req = urllib.request.Request(
        UPSTASH_URL.rstrip("/") + path,
        data=body,
        method=method,
        headers={"Authorization": "Bearer " + UPSTASH_TOKEN},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def read_state():
    try:
        result = _upstash_request("GET", "/get/" + STATE_KEY)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        print("Upstash read failed:", e, file=sys.stderr)
        return None
    value = result.get("result")
    if value is None:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


# Fields that travel together as one bundle - whichever side has the newer
# stateUpdatedAt wins the whole bundle. Mirrors Personal-tracker.html's own
# SETTINGS_FIELDS - keep both in sync.
_SETTINGS_FIELDS = [
    "version", "person", "currency", "defaultAccountId", "accounts", "buckets",
    "incomeSources", "debts", "installments",
]


def reconcile_state(base, incoming):
    """Merges two full state snapshots into one, symmetrically. Mirrors
    Personal-tracker.html's reconcileState() - keep both in sync."""
    result = dict(base)

    base_deleted = base.get("deletedTransactionIds") or []
    incoming_deleted = incoming.get("deletedTransactionIds") or []
    tombstones = set(base_deleted) | set(incoming_deleted)
    result["deletedTransactionIds"] = list(tombstones)

    merged = {}
    for t in base.get("transactions") or []:
        merged[t["id"]] = t
    for t in incoming.get("transactions") or []:
        existing = merged.get(t["id"])
        if not existing:
            merged[t["id"]] = t
            continue
        existing_time = existing.get("updatedAt") or existing.get("createdAt") or 0
        incoming_time = t.get("updatedAt") or t.get("createdAt") or 0
        if incoming_time > existing_time:
            merged[t["id"]] = t
    result["transactions"] = [t for tid, t in merged.items() if tid not in tombstones]

    base_stamp = base.get("stateUpdatedAt") or 0
    incoming_stamp = incoming.get("stateUpdatedAt") or 0
    settings_source = incoming if incoming_stamp > base_stamp else base
    for key in _SETTINGS_FIELDS:
        if key in settings_source:
            result[key] = settings_source[key]
    result["stateUpdatedAt"] = max(base_stamp, incoming_stamp)

    return result


def write_state(state):
    # Always reconcile against whatever's currently stored rather than
    # blindly overwriting - makes every PUT safe regardless of which
    # browser/device pushed last.
    current = read_state()
    merged = reconcile_state(current, state) if current else state
    body = json.dumps(merged).encode("utf-8")
    result = _upstash_request("POST", "/set/" + STATE_KEY, body=body)
    if result.get("result") != "OK":
        raise RuntimeError(f"Upstash write did not confirm OK: {result}")


# Python's round() uses banker's rounding (.5 -> nearest even); JS's
# Math.round() always rounds .5 up. These mirror Personal-tracker.html's
# round2()/Math.round() exactly, including computeSplit()'s per-bucket
# rounding to whole SAR.
_JS_EPSILON = 2.220446049250313e-16


def js_round(x):
    if x >= 0:
        return math.floor(x + 0.5)
    return -math.floor(-x + 0.5)


def round2(v):
    v = float(v)
    return js_round((v + _JS_EPSILON) * 100) / 100


def bump_state_updated_at(state):
    # max(), not a raw clock read - two tool calls seconds apart can still
    # land in the same millisecond bucket under real usage (a backfill
    # script firing several log_installment_payment calls back to back),
    # and reconcile_state()'s tie-break is a strict ">" - an exact tie
    # silently keeps the OLD stored settings bundle, discarding this
    # write's installment counter change. This guarantees each bump is
    # strictly newer than whatever this state copy already carried.
    state["stateUpdatedAt"] = max(int(time.time() * 1000), (state.get("stateUpdatedAt") or 0) + 1)


def uid():
    return "ptx_" + format(int(time.time() * 1000), "x") + "".join(random.choices(string.ascii_lowercase + string.digits, k=6))


def today_str():
    return date.today().isoformat()


def default_account_id(state):
    default_id = state.get("defaultAccountId")
    if default_id and any(a["id"] == default_id for a in state["accounts"]):
        return default_id
    return state["accounts"][0]["id"] if state["accounts"] else None


def default_income_source_id(state):
    sources = state.get("incomeSources") or []
    return sources[0]["id"] if sources else None


def find_account_id(state, name):
    for a in state["accounts"]:
        if a["id"] == name or a["name"].lower() == str(name).lower():
            return a["id"]
    return None


def resolve_account_id(state, name, tool_error_context=None):
    if not name:
        return default_account_id(state)
    found = find_account_id(state, name)
    if found:
        return found
    names = ", ".join(a["name"] for a in state["accounts"])
    raise ToolError(f'Unknown bank account "{name}" for {tool_error_context}. Available accounts: {names}')


def resolve_bucket_id(state, name):
    for b in state["buckets"]:
        if b["id"] == name or b["name"].lower() == str(name or "").lower():
            return b["id"]
    return None


def bucket_label(state, bucket_id):
    for b in state["buckets"]:
        if b["id"] == bucket_id:
            return b["name"]
    return bucket_id


def resolve_debt_id(state, name):
    for d in state.get("debts") or []:
        if d["id"] == name or d["name"].lower() == str(name or "").lower():
            return d["id"]
    return None


def debt_by_id(state, debt_id):
    return next((d for d in state.get("debts") or [] if d["id"] == debt_id), None)


def resolve_installment_id(state, name):
    for i in state.get("installments") or []:
        if i["id"] == name or i["name"].lower() == str(name or "").lower():
            return i["id"]
    return None


def installment_by_id(state, inst_id):
    return next((i for i in state.get("installments") or [] if i["id"] == inst_id), None)


def income_source_label(state, source_id):
    for s in state.get("incomeSources") or []:
        if s["id"] == source_id:
            return s["name"]
    return source_id


# Mirrors Personal-tracker.html's computeSplit(): each bucket except the
# last gets its CAP share rounded to a whole SAR (no cents), the last
# bucket absorbs whatever fractional remainder is left.
def compute_split(state, amount):
    buckets = state["buckets"]
    split = {}
    running = 0
    for i, b in enumerate(buckets):
        if i == len(buckets) - 1:
            split[b["id"]] = round2(amount - running)
        else:
            amt = js_round(amount * (float(b.get("cap") or 0)) / 100)
            split[b["id"]] = amt
            running += amt
    return split


def month_start_str():
    today = date.today()
    return f"{today.year:04d}-{today.month:02d}-01"


# Faithful port of Personal-tracker.html's computeDerived() (~line 842) -
# both sides need to agree on this, or a chat-logged transaction renders
# differently than one made by hand in the app. Mirror any change there
# here too.
def compute_derived(state):
    account_balances = {a["id"]: 0.0 for a in state["accounts"]}
    bucket_balances = {b["id"]: 0.0 for b in state["buckets"]}
    debt_balances = {d["id"]: 0.0 for d in state.get("debts") or []}
    installment_balances = {i["id"]: 0.0 for i in state.get("installments") or []}
    received_by_source = {s["id"]: 0.0 for s in state.get("incomeSources") or []}
    month_start = month_start_str()

    for tx in state["transactions"]:
        ttype = tx.get("type")
        if ttype == "draw":
            src_id = tx.get("sourceId") or default_income_source_id(state)
            if not tx.get("historical") and tx.get("date", "") >= month_start and src_id in received_by_source:
                received_by_source[src_id] = round2(received_by_source[src_id] + tx["amount"])
            if not tx.get("emergency"):
                if tx.get("accountId") in account_balances:
                    account_balances[tx["accountId"]] = round2(account_balances[tx["accountId"]] + tx["amount"])
                if not tx.get("historical"):
                    for bid, amt in (tx.get("split") or {}).items():
                        if bid in bucket_balances:
                            bucket_balances[bid] = round2(bucket_balances[bid] + amt)
        elif ttype == "expense":
            if not tx.get("historical"):
                if tx.get("bucketId") in bucket_balances:
                    bucket_balances[tx["bucketId"]] = round2(bucket_balances[tx["bucketId"]] - tx["amount"])
                if tx.get("accountId") in account_balances:
                    account_balances[tx["accountId"]] = round2(account_balances[tx["accountId"]] - tx["amount"])
        elif ttype == "debt_entry":
            if tx.get("debtId") in debt_balances:
                debt_balances[tx["debtId"]] = round2(debt_balances[tx["debtId"]] + tx["amount"])
            # Mirrors Personal-tracker.html's computeDerived() - most debt
            # entries are ledger-only, but when tx.accountId is set, real
            # cash moved through one of my own accounts too. Sign depends
            # on which way the debt points (see debt_by_id's direction).
            if tx.get("accountId") in account_balances:
                deb_for_acct = debt_by_id(state, tx.get("debtId"))
                sign = -1 if (deb_for_acct and deb_for_acct.get("direction") == "owed_to_me") else 1
                account_balances[tx["accountId"]] = round2(account_balances[tx["accountId"]] + sign * tx["amount"])
        elif ttype == "installment_payment":
            if tx.get("installmentId") in installment_balances:
                installment_balances[tx["installmentId"]] = round2(installment_balances[tx["installmentId"]] + tx["amount"])
            # An installment is always money I owe, so its raw signed
            # amount already matches cash-flow direction - no sign flip.
            if tx.get("accountId") in account_balances:
                account_balances[tx["accountId"]] = round2(account_balances[tx["accountId"]] + tx["amount"])
        elif ttype == "opening_balance":
            if tx.get("accountId") in account_balances:
                account_balances[tx["accountId"]] = round2(account_balances[tx["accountId"]] + tx["amount"])
            for bid, amt in (tx.get("allocation") or {}).items():
                if bid in bucket_balances:
                    bucket_balances[bid] = round2(bucket_balances[bid] + amt)

    return {
        "accountBalances": account_balances,
        "bucketBalances": bucket_balances,
        "debtBalances": debt_balances,
        "installmentBalances": installment_balances,
        "receivedBySource": received_by_source,
    }


# ---- MCP tool implementations ------------------------------------------

class ToolError(Exception):
    pass


def log_income(state, args):
    try:
        amount = round2(args.get("amount"))
    except (TypeError, ValueError):
        amount = 0
    if not amount or amount <= 0:
        raise ToolError("amount must be a positive number.")
    description = str(args.get("description") or "").strip()
    if not description:
        raise ToolError("description is required.")
    tx_date = args.get("date") or today_str()
    emergency = bool(args.get("emergency", False))
    historical = bool(args.get("historical", False))
    account_id = None if emergency else resolve_account_id(state, args.get("account"), tool_error_context="log_income's account")
    split = None if emergency else compute_split(state, amount)
    source_id = args.get("source")
    if source_id:
        found = next((s["id"] for s in state.get("incomeSources") or [] if s["id"] == source_id or s["name"].lower() == str(source_id).lower()), None)
        if not found:
            names = ", ".join(s["name"] for s in state.get("incomeSources") or [])
            raise ToolError(f'Unknown income source "{source_id}". Available sources: {names}')
        source_id = found
    else:
        source_id = default_income_source_id(state)

    tx = {
        "id": uid(),
        "type": "draw",
        "date": tx_date,
        "description": description,
        "amount": amount,
        "emergency": emergency,
        "historical": historical,
        "accountId": account_id,
        "split": split,
        "sourceId": source_id,
        "createdAt": int(time.time() * 1000),
        "updatedAt": int(time.time() * 1000),
    }
    state["transactions"].append(tx)

    currency = state.get("currency", "")
    if emergency:
        note = " (emergency — never landed in a bank account, still counts toward this month's entitlement)"
    elif historical:
        note = " (historical — hit the account, no Account split, doesn't count toward this month's entitlement)"
    else:
        bucket_lines = ", ".join(f"{bucket_label(state, bid)}: {amt:.2f}" for bid, amt in (split or {}).items())
        note = f". Split -> {bucket_lines}"
    return f'Income logged: {amount:.2f} {currency} - "{description}" on {tx_date}{note}.'


def log_expense(state, args):
    try:
        amount = round2(args.get("amount"))
    except (TypeError, ValueError):
        amount = 0
    if not amount or amount <= 0:
        raise ToolError("amount must be a positive number.")
    description = str(args.get("description") or "").strip()
    if not description:
        raise ToolError("description is required.")
    if not args.get("bucket"):
        raise ToolError("bucket is required (e.g. Give, FFA, Investment, Lifestyle).")
    bucket_id = resolve_bucket_id(state, args["bucket"])
    if not bucket_id:
        names = ", ".join(b["name"] for b in state["buckets"])
        raise ToolError(f'Unknown Account "{args["bucket"]}". Available: {names}')
    tx_date = args.get("date") or today_str()
    account_id = resolve_account_id(state, args.get("account"), tool_error_context="log_expense's account")
    historical = bool(args.get("historical", False))

    tx = {
        "id": uid(),
        "type": "expense",
        "date": tx_date,
        "description": description,
        "amount": amount,
        "bucketId": bucket_id,
        "accountId": account_id,
        "historical": historical,
        "createdAt": int(time.time() * 1000),
        "updatedAt": int(time.time() * 1000),
    }
    state["transactions"].append(tx)
    currency = state.get("currency", "")
    note = " (historical — record only, doesn't touch the Account or bank account balance)" if historical else ""
    return f'Expense logged: {amount:.2f} {currency} from {bucket_label(state, bucket_id)} - "{description}" on {tx_date}{note}.'


def log_debt_entry(state, args):
    try:
        amount = round2(args.get("amount"))
    except (TypeError, ValueError):
        amount = 0
    if not amount or amount <= 0:
        raise ToolError("amount must be a positive number.")
    description = str(args.get("description") or "").strip()
    if not description:
        raise ToolError("description is required.")
    if not args.get("debt"):
        raise ToolError("debt is required - which debt this affects, by name.")
    debt_id = resolve_debt_id(state, args["debt"])
    if not debt_id:
        names = ", ".join(d["name"] for d in state.get("debts") or [])
        raise ToolError(f'Unknown debt "{args["debt"]}". Available: {names}')
    direction = args.get("direction", "increase")
    if direction not in ("increase", "decrease"):
        raise ToolError('direction must be "increase" or "decrease".')
    tx_date = args.get("date") or today_str()
    signed_amount = -amount if direction == "decrease" else amount
    account_id = resolve_account_id(state, args.get("account"), tool_error_context="log_debt_entry's account") if args.get("account") else None

    tx = {
        "id": uid(),
        "type": "debt_entry",
        "date": tx_date,
        "description": description,
        "amount": signed_amount,
        "debtId": debt_id,
        "accountId": account_id,
        "createdAt": int(time.time() * 1000),
        "updatedAt": int(time.time() * 1000),
    }
    state["transactions"].append(tx)
    currency = state.get("currency", "")
    deb = debt_by_id(state, debt_id)
    verb = "added to" if direction == "increase" else "paid off against"
    acct_note = ""
    if account_id:
        acct = next((a for a in state["accounts"] if a["id"] == account_id), None)
        acct_note = f' via {acct["name"] if acct else account_id} — also shown in the main Ledger'
    return f'Debt entry logged: {amount:.2f} {currency} {verb} "{deb["name"] if deb else debt_id}" - "{description}" on {tx_date}{acct_note}.'


def log_installment_payment(state, args):
    try:
        amount = round2(args.get("amount"))
    except (TypeError, ValueError):
        amount = 0
    if not amount or amount <= 0:
        raise ToolError("amount must be a positive number.")
    description = str(args.get("description") or "").strip()
    if not description:
        raise ToolError("description is required.")
    if not args.get("installment"):
        raise ToolError("installment is required - which loan this payment is against, by name.")
    inst_id = resolve_installment_id(state, args["installment"])
    if not inst_id:
        names = ", ".join(i["name"] for i in state.get("installments") or [])
        raise ToolError(f'Unknown installment "{args["installment"]}". Available: {names}')
    direction = args.get("direction", "decrease")
    if direction not in ("increase", "decrease"):
        raise ToolError('direction must be "increase" or "decrease" (default "decrease" for a real payment).')
    tx_date = args.get("date") or today_str()
    late = bool(args.get("late", False))
    signed_amount = -amount if direction == "decrease" else amount
    account_id = resolve_account_id(state, args.get("account"), tool_error_context="log_installment_payment's account") if args.get("account") else None

    tx = {
        "id": uid(),
        "type": "installment_payment",
        "date": tx_date,
        "description": description,
        "amount": signed_amount,
        "installmentId": inst_id,
        "accountId": account_id,
        "createdAt": int(time.time() * 1000),
        "updatedAt": int(time.time() * 1000),
    }

    counted_note = ""
    if direction == "decrease":
        # A real payment also updates the installment's own left/late
        # counters - mirrors Personal-tracker.html's submitTx() (a "new
        # payment", not an edit or a setup/correction "increase").
        tx["late"] = late
        tx["countedAsPayment"] = True
        inst = installment_by_id(state, inst_id)
        if inst:
            inst["installmentsLeft"] = max(0, (int(inst.get("installmentsLeft") or 0)) - 1)
            if late:
                inst["lateCount"] = int(inst.get("lateCount") or 0) + 1
            counted_note = f' ({inst.get("installmentsLeft")} left{", late" if late else ""})'
            # installments is part of the settings bundle reconcile_state()
            # picks by stateUpdatedAt - without bumping it, this counter
            # change loses to whatever's already stored the moment it's
            # merged, since neither side's timestamp would've moved.
            bump_state_updated_at(state)

    state["transactions"].append(tx)
    currency = state.get("currency", "")
    inst = installment_by_id(state, inst_id)
    acct_note = ""
    if account_id:
        acct = next((a for a in state["accounts"] if a["id"] == account_id), None)
        acct_note = f' via {acct["name"] if acct else account_id} — also shown in the main Ledger'
    return f'Installment payment logged: {amount:.2f} {currency} against "{inst["name"] if inst else inst_id}" - "{description}" on {tx_date}{counted_note}{acct_note}.'


def list_transactions(state, args):
    limit = args.get("limit")
    try:
        limit = int(limit) if limit is not None else 20
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 200))

    type_filter = args.get("type")
    since = args.get("since")

    def matches(t):
        if type_filter and t.get("type") != type_filter:
            return False
        if since and t.get("date", "") < since:
            return False
        return True

    txs = [t for t in state["transactions"] if matches(t)]
    txs.sort(key=lambda t: (t.get("date", ""), t.get("createdAt", 0)), reverse=True)
    total_matches = len(txs)
    shown = txs[:limit]
    if not shown:
        return "No transactions match."

    currency = state.get("currency", "")

    def account_name(acc_id):
        return next((a["name"] for a in state["accounts"] if a["id"] == acc_id), acc_id or "")

    lines = []
    for t in shown:
        tid = t.get("id", "")
        ttype = t.get("type")
        desc = t.get("description", "")
        if ttype == "draw":
            flags = []
            if t.get("emergency"):
                flags.append("emergency")
            if t.get("historical"):
                flags.append("historical")
            flag_note = f" ({', '.join(flags)})" if flags else ""
            acc = account_name(t.get("accountId")) if t.get("accountId") else "(no account)"
            lines.append(f'[{tid}] {t.get("date")} | income | +{t.get("amount", 0):.2f} {currency}{flag_note} | "{desc}" | into {acc}')
        elif ttype == "expense":
            b = bucket_label(state, t.get("bucketId"))
            acc = account_name(t.get("accountId"))
            hist = " (historical)" if t.get("historical") else ""
            lines.append(f'[{tid}] {t.get("date")} | expense | -{t.get("amount", 0):.2f} {currency} | {b} | "{desc}" | from {acc}{hist}')
        elif ttype == "debt_entry":
            deb = debt_by_id(state, t.get("debtId"))
            acct_note = f' | via {account_name(t.get("accountId"))}' if t.get("accountId") else ""
            lines.append(f'[{tid}] {t.get("date")} | debt_entry | {t.get("amount", 0):.2f} {currency} | {deb["name"] if deb else t.get("debtId")} | "{desc}"{acct_note}')
        elif ttype == "installment_payment":
            inst = installment_by_id(state, t.get("installmentId"))
            late_note = " | late" if t.get("late") else ""
            acct_note = f' | via {account_name(t.get("accountId"))}' if t.get("accountId") else ""
            lines.append(f'[{tid}] {t.get("date")} | installment_payment | {t.get("amount", 0):.2f} {currency} | {inst["name"] if inst else t.get("installmentId")} | "{desc}"{late_note}{acct_note}')
        elif ttype == "opening_balance":
            acc = account_name(t.get("accountId"))
            lines.append(f'[{tid}] {t.get("date")} | opening_balance | +{t.get("amount", 0):.2f} {currency} | "{desc}" | into {acc}')
        else:
            lines.append(f'[{tid}] {t.get("date")} | {ttype} | {t.get("amount", 0):.2f} {currency} | "{desc}"')

    header = f"Showing {len(shown)} of {total_matches} matching transaction(s), newest first. [id] is what delete_transaction needs:"
    return header + "\n" + "\n".join(lines)


def get_summary(state):
    derived = compute_derived(state)
    currency = state.get("currency", "")
    lines = ["Accounts:"]
    for b in state["buckets"]:
        bal = round2(derived["bucketBalances"].get(b["id"], 0))
        lines.append(f'  {b["name"]}: {bal:.2f} {currency} ({b.get("cap")}% share)')

    lines.append("\nBank accounts:")
    for a in state["accounts"]:
        bal = round2(derived["accountBalances"].get(a["id"], 0))
        lines.append(f'  {a["name"]}: {bal:.2f} {currency}')

    sources = state.get("incomeSources") or []
    if sources:
        lines.append("\nThis month's income entitlement:")
        for s in sources:
            received = round2(derived["receivedBySource"].get(s["id"], 0))
            lines.append(f'  {s["name"]}: {received:.2f} {currency} received so far this month')

    debts = state.get("debts") or []
    if debts:
        net = 0.0
        for d in debts:
            bal = round2(derived["debtBalances"].get(d["id"], 0))
            net = round2(net + (-bal if d.get("direction") == "owed_by_me" else bal))
        lines.append(f"\nNet personal position (debts): {net:.2f} {currency}")
        for d in debts:
            bal = round2(derived["debtBalances"].get(d["id"], 0))
            lines.append(f'  {d["name"]} ({d.get("direction")}): {bal:.2f} {currency}')

    installments = state.get("installments") or []
    if installments:
        total_monthly = round2(sum(float(i.get("monthlyAmount") or 0) for i in installments))
        total_remaining = round2(sum(derived["installmentBalances"].get(i["id"], 0) for i in installments))
        lines.append(f"\nInstallments: {currency} {total_monthly:.2f}/mo total, {currency} {total_remaining:.2f} remaining across {len(installments)} loan(s)")
        for i in installments:
            bal = round2(derived["installmentBalances"].get(i["id"], 0))
            left = i.get("installmentsLeft")
            total = i.get("installmentsTotal")
            late = i.get("lateCount") or 0
            progress = f' - {left}/{total} left' if total else ""
            late_note = f", {late} late" if late else ""
            lines.append(f'  {i["name"]}: {bal:.2f} {currency}{progress}{late_note}')

    return "\n".join(lines)


def delete_transaction(state, args):
    tx_id = args.get("id")
    if not tx_id:
        raise ToolError("id is required - get it from list_transactions' [id] prefix.")
    confirm_description = str(args.get("confirm_description") or "").strip().lower()
    if not confirm_description:
        raise ToolError(
            "confirm_description is required - pass the transaction's exact description back, "
            "as a safety check that this is really the entry meant to be deleted."
        )
    match = next((t for t in state["transactions"] if t.get("id") == tx_id), None)
    if not match:
        raise ToolError(f'No transaction found with id "{tx_id}".')
    actual_description = str(match.get("description") or "").strip().lower()
    if confirm_description != actual_description:
        raise ToolError(
            f'confirm_description doesn\'t match - this transaction\'s description is "{match.get("description", "")}". '
            "Pass it back exactly to confirm you have the right one."
        )

    # Reverse the installment left/late counters a real payment bumped -
    # mirrors Personal-tracker.html's own delete handler.
    if match.get("countedAsPayment"):
        inst = installment_by_id(state, match.get("installmentId"))
        if inst:
            inst["installmentsLeft"] = int(inst.get("installmentsLeft") or 0) + 1
            if match.get("late"):
                inst["lateCount"] = max(0, int(inst.get("lateCount") or 0) - 1)
            bump_state_updated_at(state)

    state["transactions"] = [t for t in state["transactions"] if t.get("id") != tx_id]
    if state.get("deletedTransactionIds") is None:
        state["deletedTransactionIds"] = []
    if tx_id not in state["deletedTransactionIds"]:
        state["deletedTransactionIds"].append(tx_id)
    currency = state.get("currency", "")
    return f'Deleted: {match.get("amount", 0):.2f} {currency} - "{match.get("description", "")}" on {match.get("date", "")}.'


def edit_transaction(state, args):
    tx_id = args.get("id")
    if not tx_id:
        raise ToolError("id is required - get it from list_transactions' [id] prefix.")
    confirm_description = str(args.get("confirm_description") or "").strip().lower()
    if not confirm_description:
        raise ToolError(
            "confirm_description is required - pass the transaction's current exact description "
            "back, as a safety check that this is really the entry meant to be edited."
        )
    tx = next((t for t in state["transactions"] if t.get("id") == tx_id), None)
    if not tx:
        raise ToolError(f'No transaction found with id "{tx_id}".')
    actual_description = str(tx.get("description") or "").strip().lower()
    if confirm_description != actual_description:
        raise ToolError(
            f'confirm_description doesn\'t match - this transaction\'s current exact description is "{tx.get("description", "")}". '
            "Pass it back exactly to confirm you have the right one."
        )

    changes = []

    if "amount" in args and args["amount"] is not None:
        try:
            new_amount = round2(args["amount"])
        except (TypeError, ValueError):
            new_amount = 0
        if not new_amount or new_amount <= 0:
            raise ToolError("amount must be a positive number.")
        signed = new_amount
        if tx.get("type") in ("debt_entry", "installment_payment") and (tx.get("amount") or 0) < 0:
            signed = -new_amount
        tx["amount"] = signed
        changes.append("amount")
        if tx.get("type") == "draw" and not tx.get("emergency") and not tx.get("historical"):
            tx["split"] = compute_split(state, new_amount)
            changes.append("split")

    if "description" in args and args["description"] is not None:
        new_desc = str(args["description"]).strip()
        if not new_desc:
            raise ToolError("description can't be blank.")
        tx["description"] = new_desc
        changes.append("description")

    if "date" in args and args["date"]:
        tx["date"] = args["date"]
        changes.append("date")

    if "bucket" in args and args["bucket"] and tx.get("type") == "expense":
        bucket_id = resolve_bucket_id(state, args["bucket"])
        if not bucket_id:
            names = ", ".join(b["name"] for b in state["buckets"])
            raise ToolError(f'Unknown Account "{args["bucket"]}". Available: {names}')
        tx["bucketId"] = bucket_id
        changes.append("bucket")

    if "account" in args and args["account"] and tx.get("type") in ("draw", "expense"):
        tx["accountId"] = resolve_account_id(state, args["account"], tool_error_context="edit_transaction's account")
        changes.append("account")

    if not changes:
        raise ToolError("Nothing to change - pass at least one field to update (amount, description, date, bucket, account).")

    tx["updatedAt"] = int(time.time() * 1000)
    currency = state.get("currency", "")
    msg = f'Updated: {abs(tx.get("amount", 0)):.2f} {currency} - "{tx.get("description", "")}" on {tx.get("date", "")}. Changed: {", ".join(c for c in changes if c != "split")}.'
    return msg


# ---- MCP JSON-RPC plumbing ----------------------------------------------

TOOLS = [
    {
        "name": "log_income",
        "description": "Log a new personal income/draw transaction. Splits it across the tracker's "
        "Accounts (Give/FFA/Investment/Lifestyle) using their current % share, same as logging it by "
        "hand in the app. Use emergency=true for money that never landed in a bank account (spent "
        "directly on a bill) - it still counts toward this month's entitlement but doesn't split. Use "
        "historical=true for real money that predates the Account-split system - it hits the account "
        "balance but doesn't split and doesn't count toward entitlement. Ask if unsure which applies.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "amount": {"type": "number", "description": "Positive amount received"},
                "description": {"type": "string", "description": "What this income was for"},
                "date": {"type": "string", "description": "YYYY-MM-DD, defaults to today"},
                "account": {"type": "string", "description": "Bank account it landed in, by name. Defaults to the app's default account. Ignored if emergency=true."},
                "source": {"type": "string", "description": "Income source name, e.g. \"Owner's Pay\". Defaults to the app's first income source."},
                "emergency": {"type": "boolean", "description": "Default false. True if this never landed in a bank account (spent directly on a bill)."},
                "historical": {"type": "boolean", "description": "Default false. True if this predates the Account-split system - real money, no split, doesn't count toward entitlement."},
            },
            "required": ["amount", "description"],
            "additionalProperties": False,
        },
    },
    {
        "name": "log_expense",
        "description": "Log a new personal expense drawn from one Account (Give, FFA, Investment, or "
        "Lifestyle - or any custom Account name in this tracker). Set historical=true only for an "
        "expense that predates a later opening balance which already reflects it (record-only, no "
        "balance change) - ask if unsure.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "amount": {"type": "number", "description": "Positive amount spent"},
                "description": {"type": "string", "description": "What this expense was for / vendor"},
                "bucket": {"type": "string", "description": 'Which Account to draw from, by name (e.g. "Lifestyle")'},
                "date": {"type": "string", "description": "YYYY-MM-DD, defaults to today"},
                "account": {"type": "string", "description": "Bank account it was paid from. Defaults to the app's default account."},
                "historical": {"type": "boolean", "description": "Default false. True only if this predates a later opening balance that already reflects it."},
            },
            "required": ["amount", "description", "bucket"],
            "additionalProperties": False,
        },
    },
    {
        "name": "log_debt_entry",
        "description": "Log a change to one of the tracker's debts (money owed to Albaiti or owed by "
        "him). direction=\"increase\" grows the debt in whatever direction it's already tracked in "
        "(more owed); direction=\"decrease\" is a payment against it (less owed). Pass account only "
        "when real cash actually moved through one of Albaiti's own bank accounts (e.g. a Jawaher "
        "repayment paid out of Barq) - that also shows this entry in the main Ledger, not just the "
        "Debt section. Leave it out for a ledger-only record (nothing left a tracked account).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "amount": {"type": "number", "description": "Positive amount"},
                "description": {"type": "string", "description": "What this entry was for"},
                "debt": {"type": "string", "description": 'Which debt, by name (e.g. "Owed to Jawaher")'},
                "date": {"type": "string", "description": "YYYY-MM-DD, defaults to today"},
                "direction": {"type": "string", "enum": ["increase", "decrease"], "description": 'Default "increase".'},
                "account": {"type": "string", "description": "Bank account this really moved through, by name - only if real cash moved. Omit for a ledger-only debt record."},
            },
            "required": ["amount", "description", "debt"],
            "additionalProperties": False,
        },
    },
    {
        "name": "log_installment_payment",
        "description": "Log a payment (or a setup/correction) against one of the tracker's installment "
        "loans. direction=\"decrease\" (the default) is a real payment - it also reduces that "
        "installment's installments-left count and bumps its late count if late=true. "
        "direction=\"increase\" is a correction/setup adjustment and does NOT touch those counters. "
        "Pass account only when real cash actually moved through one of Albaiti's own bank accounts "
        "- that also shows this entry in the main Ledger, not just the Installments section.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "amount": {"type": "number", "description": "Positive amount"},
                "description": {"type": "string", "description": "What this payment was for"},
                "installment": {"type": "string", "description": 'Which loan, by name (e.g. "Tamam Loan (via Maryam)")'},
                "date": {"type": "string", "description": "YYYY-MM-DD, defaults to today"},
                "direction": {"type": "string", "enum": ["increase", "decrease"], "description": 'Default "decrease" (a real payment).'},
                "late": {"type": "boolean", "description": "Default false. True if this payment was late - only meaningful with direction=\"decrease\"."},
                "account": {"type": "string", "description": "Bank account this really moved through, by name - only if real cash moved. Omit for a ledger-only record."},
            },
            "required": ["amount", "description", "installment"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_summary",
        "description": "Get current Account balances, bank account balances, this month's income "
        "entitlement, net personal debt position, and installment obligations/remaining balances - "
        "the same math the app itself uses, not an approximation.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "list_transactions",
        "description": "List individual transactions (newest first), optionally filtered by type or a "
        "start date. Use this for specific questions like \"what did Albaiti spend on X\" or \"show the "
        "last few entries\" - get_summary only gives current totals, not line items.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "number", "description": "Max transactions to return, default 20, max 200"},
                "type": {"type": "string", "enum": ["draw", "expense", "debt_entry", "installment_payment", "opening_balance"], "description": "Filter to just one transaction type"},
                "since": {"type": "string", "description": "YYYY-MM-DD - only transactions on or after this date"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "delete_transaction",
        "description": "Permanently delete one transaction by id (from list_transactions' [id] prefix) - "
        "for an entry that shouldn't exist at all (a duplicate, something logged by mistake). For "
        "fixing a wrong field on an otherwise-real entry, use edit_transaction instead. Requires "
        "echoing back the transaction's exact description as confirm_description - a safety check "
        "against deleting the wrong entry. Confirm with Albaiti before deleting anything real.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "The transaction id, from list_transactions"},
                "confirm_description": {"type": "string", "description": "The transaction's exact description, echoed back to confirm this is the right one"},
            },
            "required": ["id", "confirm_description"],
            "additionalProperties": False,
        },
    },
    {
        "name": "edit_transaction",
        "description": "Correct a field on an existing transaction (amount, description, date, and for "
        "an expense its Account/bank account) by id, from list_transactions' [id] prefix. Requires "
        "echoing back the transaction's current exact description as confirm_description - a safety "
        "check against editing the wrong entry. Editing an income transaction's amount recomputes its "
        "Account split, same as the app itself. Only pass the fields you want to change.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "The transaction id, from list_transactions"},
                "confirm_description": {"type": "string", "description": "The transaction's current exact description, echoed back to confirm this is the right one"},
                "amount": {"type": "number", "description": "New amount (positive - sign is inferred), if changing it"},
                "description": {"type": "string", "description": "New description, if changing it"},
                "date": {"type": "string", "description": "New date (YYYY-MM-DD), if changing it"},
                "bucket": {"type": "string", "description": "New Account, by name - expense only"},
                "account": {"type": "string", "description": "New bank account, by name - income/expense only"},
            },
            "required": ["id", "confirm_description"],
            "additionalProperties": False,
        },
    },
]


def handle_mcp(body):
    method = body.get("method")
    req_id = body.get("id")
    params = body.get("params") or {}

    if method == "notifications/initialized":
        return 202, None

    if method == "initialize":
        return 200, {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "personal-tracker", "version": "1.0.0"},
            },
        }

    if method == "tools/list":
        return 200, {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}

    if method == "tools/call":
        state = read_state()
        if not state:
            return 200, {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": "No tracker data found yet - open the app once first."}],
                    "isError": True,
                },
            }
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            if name == "log_income":
                text = log_income(state, args)
            elif name == "log_expense":
                text = log_expense(state, args)
            elif name == "log_debt_entry":
                text = log_debt_entry(state, args)
            elif name == "log_installment_payment":
                text = log_installment_payment(state, args)
            elif name == "get_summary":
                text = get_summary(state)
            elif name == "list_transactions":
                text = list_transactions(state, args)
            elif name == "delete_transaction":
                text = delete_transaction(state, args)
            elif name == "edit_transaction":
                text = edit_transaction(state, args)
            else:
                raise ToolError(f"Unknown tool: {name}")
            write_state(state)
            return 200, {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": text}], "isError": False}}
        except ToolError as e:
            return 200, {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": str(e)}], "isError": True}}
        except Exception as e:
            return 200, {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": f"Storage error, transaction not saved: {e}"}], "isError": True}}

    return 400, {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}


# ---- HTTP server ---------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, status, body):
        payload = b"" if body is None else json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, PUT, POST, OPTIONS")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def _authorized(self):
        auth = self.headers.get("Authorization", "")
        return auth == f"Bearer {TOKEN}"

    def _body(self):
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    def do_OPTIONS(self):
        self._send(204, None)

    def do_GET(self):
        # The page itself is served with no auth check - a plain page load
        # has no way to send a Bearer header, and it embeds the same
        # SYNC_TOKEN whether hosted here or opened as a local file, so this
        # doesn't expose anything new. Everything else (the actual data)
        # stays behind the token.
        if self.path in ("/", "/index.html"):
            try:
                with open(TRACKER_HTML_FILE, "rb") as f:
                    html = f.read()
            except FileNotFoundError:
                return self._send(500, {"error": "Personal-tracker.html not found next to server.py"})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
            return

        if not self._authorized():
            return self._send(401, {"error": "Unauthorized"})
        if self.path == "/api/state":
            state = read_state()
            return self._send(200 if state else 404, state or {"error": "No state saved yet"})
        self._send(404, {"error": "Not found"})

    def do_PUT(self):
        if not self._authorized():
            return self._send(401, {"error": "Unauthorized"})
        if self.path == "/api/state":
            try:
                state = json.loads(self._body())
            except json.JSONDecodeError:
                return self._send(400, {"error": "Invalid JSON body"})
            try:
                write_state(state)
            except Exception as e:
                return self._send(502, {"error": f"Storage write failed: {e}"})
            return self._send(200, {"ok": True})
        self._send(404, {"error": "Not found"})

    def do_POST(self):
        if not self._authorized():
            return self._send(401, {"error": "Unauthorized"})
        if self.path == "/mcp":
            try:
                body = json.loads(self._body())
            except json.JSONDecodeError:
                return self._send(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
            status, resp_body = handle_mcp(body)
            return self._send(status, resp_body)
        self._send(404, {"error": "Not found"})


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Personal tracker sync server listening on :{PORT}")
    server.serve_forever()
