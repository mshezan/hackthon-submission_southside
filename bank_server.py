"""
FastAPI Mock Bank Server
Simulates a bank's Account Aggregator API (identity / PAN-style fetch).
Runs on: http://127.0.0.1:8000
"""

from __future__ import annotations

import hashlib
import re
from calendar import monthrange
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

import random
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

app = FastAPI(title="Mock Bank API", version="2.0.0")

# account_id -> { "identity_norm": str, "bank_name": str, "account": dict }
_ACCOUNT_REGISTRY: Dict[int, Dict[str, Any]] = {}

_BANK_TEMPLATES = (
    ("HDFC Bank", (("Savings", 0.55), ("Current", 0.35))),
    ("ICICI Bank", (("Savings", 0.50),)),
    ("State Bank of India", (("Savings", 0.60), ("Current", 0.25))),
)


class Account(BaseModel):
    """Single account as returned under a bank."""

    id: int
    name: str
    type: str
    balance: float
    account_number_masked: str = Field(..., description="Masked account number")
    bank_name: Optional[str] = None


class Transaction(BaseModel):
    date: str = Field(..., description="YYYY-MM-DD")
    mode: str
    merchant: str = Field(..., description="Counterparty / payee (maps to description in app)")
    description: str = Field(
        default="",
        description="Same as merchant for clients that expect description",
    )
    amount: float
    type: str = Field(..., description="debit or credit")
    narration: Optional[str] = None

    class Config:
        schema_extra = {
            "example": {
                "date": "2024-11-01",
                "mode": "UPI",
                "merchant": "Swiggy",
                "description": "Swiggy",
                "amount": 450.00,
                "type": "debit",
                "narration": "Food delivery",
            }
        }


_SALARY_MERCHANTS = (
    "Salary Credit — Apex Software Company Pvt Ltd",
    "Payroll Credit Northwind Tech Pvt Ltd",
    "Monthly Salary — Galaxy Ventures Company Pvt Ltd",
)
_UNCATEG_DEBIT_LABELS = (
    "ATM Cash Withdrawal",
    "POS Purchase Local Vendor",
    "Misc Bank Charges",
)


def _normalize_identity(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    s = raw.strip().upper()
    if len(s) < 5 or len(s) > 32:
        return None
    if not re.match(r"^[A-Z0-9]+$", s):
        return None
    return s


def _identity_seed(norm: str) -> int:
    """
    Deterministic int from identity string (SHA-256).
    Do not use Python's built-in hash() — it is salted per process and not stable.
    """
    return int(hashlib.sha256(norm.encode()).hexdigest()[:12], 16)


def _stable_account_id(norm: str, bank_name: str, index: int) -> int:
    """
    Globally unique per (identity, bank, account index) without relying on registry
    collision resolution or call order. Stays within 31-bit signed int for JSON/SQLite.
    """
    h = hashlib.sha256(f"{norm}|{bank_name}|{index}".encode()).digest()
    span = (2**31) - 1_000_000 - 1
    return 1_000_000 + (int.from_bytes(h[:8], "big") % span)


def _masked_suffix(norm: str, bank_name: str, index: int) -> str:
    h = hashlib.sha256(f"mask|{norm}|{bank_name}|{index}".encode()).hexdigest()
    digits = "".join(c for c in h if c.isdigit())[:4]
    if len(digits) < 4:
        digits = (digits + "0248")[:4]
    return digits


def _holder_name_from_seed(seed: int) -> str:
    """Deterministic holder name from integer seed (from SHA-256 of identity, not random())."""
    first = ["Rohan", "Priya", "Amit", "Ananya", "Vikram", "Kavya", "Neha", "Arjun"]
    last = ["Gupta", "Sharma", "Patel", "Reddy", "Iyer", "Singh", "Nair", "Kapoor"]
    return f"{first[seed % len(first)]} {last[(seed // 7) % len(last)]}"


def build_identity_catalog(norm: str) -> Dict[str, Any]:
    """Deterministic banks + accounts for one identity (one real person)."""
    seed = _identity_seed(norm)
    holder = _holder_name_from_seed(seed)
    n_banks = 2 + (seed % 2)  # 2 or 3 banks

    banks_out: List[Dict[str, Any]] = []
    for bi in range(n_banks):
        bank_name, types_weights = _BANK_TEMPLATES[bi % len(_BANK_TEMPLATES)]
        accounts: List[Dict[str, Any]] = []
        for idx, (acc_type, w) in enumerate(types_weights):
            aid = _stable_account_id(norm, bank_name, idx)
            bal = round(15_000 + (seed % 10_000) + (aid % 7000) * float(w) * 12, 2)
            masked = f"XXXXXX{_masked_suffix(norm, bank_name, idx)}"
            acc = {
                "id": aid,
                "name": holder,
                "type": acc_type,
                "balance": bal,
                "account_number_masked": masked,
                "bank_name": bank_name,
            }
            accounts.append(acc)
            _ACCOUNT_REGISTRY[aid] = {"identity_norm": norm, "bank_name": bank_name, "account": acc}
        banks_out.append({"name": bank_name, "accounts": accounts})

    return {"holder_name": holder, "banks": banks_out}


@app.get("/", tags=["Health"])
def root():
    return {
        "status": "online",
        "service": "Mock Bank API",
        "version": "2.0.0",
        "endpoints": {
            "accounts": "/accounts?identity_id=YOUR_ID",
            "account": "/accounts/{account_id}",
            "transactions": "/accounts/{account_id}/transactions",
        },
    }


@app.get("/accounts", tags=["Accounts"])
def get_accounts_by_identity(
    identity_id: Optional[str] = Query(None, description="PAN-like identity (alphanumeric, 5–32 chars)"),
):
    """
    Identity-based fetch: returns banks, each with accounts for one holder.
    """
    if identity_id is None or not str(identity_id).strip():
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": "identity_id is required"},
        )
    norm = _normalize_identity(identity_id)
    if not norm:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": "Invalid identity format"},
        )

    # Clear stale registry entries for this identity (re-build)
    stale = [k for k, v in _ACCOUNT_REGISTRY.items() if v.get("identity_norm") == norm]
    for k in stale:
        del _ACCOUNT_REGISTRY[k]

    catalog = build_identity_catalog(norm)
    return {
        "status": "ok",
        "holder_name": catalog["holder_name"],
        "banks": catalog["banks"],
    }


@app.get("/accounts/{account_id}", tags=["Accounts"])
def get_account(account_id: int):
    row = _ACCOUNT_REGISTRY.get(account_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"Account {account_id} not found")
    a = dict(row["account"])
    return a


@app.get("/accounts/{account_id}/transactions", response_model=List[Transaction], tags=["Transactions"])
def get_account_transactions(
    account_id: int,
    start_date: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
):
    row = _ACCOUNT_REGISTRY.get(account_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"Account {account_id} not found")

    if start_date:
        try:
            filter_date = datetime.strptime(start_date, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")
    else:
        filter_date = datetime.now() - timedelta(days=180)

    txs = generate_transactions_for_account(account_id, row["identity_norm"], filter_date)
    return txs


def generate_transactions_for_account(
    account_id: int,
    identity_norm: str,
    start_date: datetime,
) -> List[dict]:
    """Deterministic monthly household-style data (no unseeded randomness)."""
    transactions: List[dict] = []
    today = datetime.now().date()
    start_d = start_date.date() if isinstance(start_date, datetime) else start_date

    seed = int.from_bytes(
        hashlib.sha256(f"{identity_norm}|{account_id}".encode()).digest()[:8],
        "big",
    )
    base_salary = round(30_000 + (seed % 50) * 900, 2)
    salary_day_pref = 1 + (seed % 5)

    def push(
        d: date,
        mode: str,
        merchant: str,
        amount: float,
        tx_type: str,
        narration: Optional[str] = None,
    ) -> None:
        if d < start_d or d > today or amount <= 0:
            return
        amt = round(float(amount), 2)
        transactions.append(
            {
                "date": d.isoformat(),
                "mode": mode,
                "merchant": merchant,
                "description": merchant,
                "amount": amt,
                "type": tx_type,
                "narration": narration,
            }
        )

    y, mth = start_d.year, start_d.month
    while True:
        month_first = date(y, mth, 1)
        if month_first > today:
            break
        last = monthrange(y, mth)[1]
        month_last = date(y, mth, last)
        if month_last < start_d:
            if mth == 12:
                y, mth = y + 1, 1
            else:
                mth += 1
            continue

        rng = random.Random(seed + y * 10_000 + mth * 100 + account_id)
        salary = round(base_salary * rng.uniform(0.992, 1.008), 2)

        spend_cap = salary * rng.uniform(0.72, 0.88)
        micro_share = rng.uniform(0.045, 0.095)
        uncat_amt_share = rng.uniform(0.018, 0.045)
        bucket_budget = max(spend_cap * (1.0 - micro_share - uncat_amt_share), salary * 0.50)

        parts = {
            "rent": rng.uniform(0.28, 0.42),
            "groceries": rng.uniform(0.14, 0.24),
            "utilities": rng.uniform(0.07, 0.12),
            "transport_fuel": rng.uniform(0.10, 0.18),
            "food_delivery": rng.uniform(0.10, 0.20),
            "shopping": rng.uniform(0.08, 0.14),
            "subscriptions": rng.uniform(0.03, 0.08),
        }
        psum = sum(parts.values())
        amounts = {k: round(parts[k] / psum * bucket_budget, 2) for k in parts}
        drift = bucket_budget - sum(amounts.values())
        amounts["groceries"] = round(amounts["groceries"] + drift, 2)

        micro_budget = max(round(spend_cap * micro_share, 2), 0)
        uncat_budget = max(round(spend_cap * uncat_amt_share, 2), 0)

        salary_day = min(salary_day_pref, last)
        push(
            date(y, mth, salary_day),
            "NEFT",
            rng.choice(_SALARY_MERCHANTS),
            salary,
            "credit",
            "Payroll Credit",
        )

        push(
            date(y, mth, min(salary_day + 2, last)),
            "NEFT",
            "Rent Transfer NEFT",
            amounts["rent"],
            "debit",
            "Monthly rent",
        )

        u_util = amounts["utilities"]
        elec = round(u_util * rng.uniform(0.58, 0.72), 2)
        tel = round(u_util - elec, 2)
        push(
            date(y, mth, rng.randint(10, min(22, last))),
            "NEFT",
            "Electricity Bill Tata Power Mumbai",
            elec,
            "debit",
            "Utility bill",
        )
        if tel > 40:
            push(
                date(y, mth, rng.randint(8, min(24, last))),
                "UPI",
                "Jio Postpaid Bill",
                tel,
                "debit",
                "Mobile bill",
            )

        g_tot = amounts["groceries"]
        n_g = rng.randint(2, 4)
        weights = [rng.uniform(0.2, 1.0) for _ in range(n_g)]
        gw = sum(weights)
        g_labels = ["DMart Thane", "BigBasket Delivery", "DMart Borivali", "Nature Basket"]
        for i in range(n_g):
            d_g = min(6 + i * 5 + rng.randint(0, 3), last)
            push(
                date(y, mth, d_g),
                rng.choice(["Card", "UPI"]),
                rng.choice(g_labels),
                round(g_tot * weights[i] / gw, 2),
                "debit",
                "Grocery shopping",
            )

        tf = amounts["transport_fuel"]
        fuel_amt = round(tf * rng.uniform(0.35, 0.55), 2)
        ride_pool = round(tf - fuel_amt, 2)
        push(
            date(y, mth, rng.randint(4, min(20, last))),
            "UPI",
            "Indian Oil Petrol Mumbai",
            fuel_amt,
            "debit",
            "Fuel",
        )
        n_rides = rng.randint(3, 7)
        if ride_pool > 0 and n_rides > 0:
            per = round(ride_pool / n_rides, 2)
            for _ in range(n_rides):
                push(
                    date(y, mth, rng.randint(4, max(4, last - 1))),
                    "UPI",
                    rng.choice(["Uber Ride Delhi", "Ola Cabs Pune"]),
                    per,
                    "debit",
                    "Ride sharing",
                )

        fd = amounts["food_delivery"]
        n_fd = rng.randint(5, 11)
        if n_fd > 0:
            fd_each = round(fd / n_fd, 2)
            for _ in range(n_fd):
                push(
                    date(y, mth, rng.randint(5, max(5, last - 1))),
                    "UPI",
                    rng.choice(["Swiggy", "Zomato Order", "Swiggy Instamart"]),
                    fd_each,
                    "debit",
                    "Food delivery",
                )

        sh = amounts["shopping"]
        if sh > 400:
            a_amt = round(sh * rng.uniform(0.45, 0.62), 2)
            a_amt = max(150.0, min(a_amt, sh - 150.0))
        else:
            a_amt = round(sh * rng.uniform(0.48, 0.58), 2)
        push(
            date(y, mth, rng.randint(7, min(26, last))),
            "UPI",
            "Amazon.in",
            a_amt,
            "debit",
            "Online shopping",
        )
        push(
            date(y, mth, rng.randint(7, min(26, last))),
            "Card",
            "Flipkart India",
            round(sh - a_amt, 2),
            "debit",
            "Online shopping",
        )

        sub = amounts["subscriptions"]
        sub_netflix = round(sub * rng.uniform(0.52, 0.68), 2)
        sub_netflix = min(sub_netflix, sub * 0.75)
        sub_netflix = max(1.0, min(sub_netflix, sub - 1.0))
        push(
            date(y, mth, rng.randint(2, min(12, last))),
            "Card",
            "Netflix Subscription India",
            sub_netflix,
            "debit",
            "OTT subscription",
        )
        push(
            date(y, mth, rng.randint(2, min(14, last))),
            "UPI",
            "Spotify Premium India",
            round(sub - sub_netflix, 2),
            "debit",
            "Music",
        )

        if micro_budget > 0:
            micro_merchants = [
                "Swiggy",
                "Zomato",
                "Uber Ride",
                "DMart Express",
                "Blinkit Quick",
                "McDonalds India",
                "Starbucks Mumbai",
            ]
            n_micro = rng.randint(10, 30)
            per_floor = max(35.0, min(90.0, micro_budget / max(n_micro, 1)))
            n_micro = max(5, min(n_micro, int(micro_budget / per_floor) + 4))
            raw = [rng.uniform(50, 500) for _ in range(n_micro)]
            rsum = sum(raw) or 1.0
            micro_amts = [
                max(50.0, min(500.0, round(micro_budget * (w / rsum), 2)))
                for w in raw
            ]
            adj = round(micro_budget - sum(micro_amts), 2)
            if micro_amts and abs(adj) > 0.01:
                micro_amts[-1] = max(50.0, min(500.0, round(micro_amts[-1] + adj, 2)))
            for amt in micro_amts:
                if amt <= 0:
                    continue
                push(
                    date(y, mth, rng.randint(3, max(3, last - 1))),
                    "UPI",
                    rng.choice(micro_merchants),
                    amt,
                    "debit",
                    "UPI payment",
                )

        prefix = f"{y}-{mth:02d}-"
        month_debits = [t for t in transactions if t["type"] == "debit" and t["date"].startswith(prefix)]
        n_debit_so_far = len(month_debits)
        if n_debit_so_far > 0 and uncat_budget > 0:
            frac = rng.uniform(0.10, 0.15)
            n_uncat = max(1, int(round(n_debit_so_far * frac)))
            n_uncat = min(n_uncat, max(2, int(n_debit_so_far * 0.16) + 1))
            if n_uncat > 0:
                ua = [rng.uniform(0.6, 1.0) for _ in range(n_uncat)]
                uus = sum(ua) or 1.0
                for j in range(n_uncat):
                    push(
                        date(y, mth, rng.randint(2, max(2, last - 1))),
                        rng.choice(["Card", "ATM", "UPI"]),
                        rng.choice(_UNCATEG_DEBIT_LABELS),
                        max(80.0, round(uncat_budget * (ua[j] / uus), 2)),
                        "debit",
                        "Misc",
                    )

        if mth == 12:
            y, mth = y + 1, 1
        else:
            mth += 1

    transactions.sort(key=lambda x: (x["date"], x.get("merchant", "")), reverse=True)
    return transactions


if __name__ == "__main__":
    import uvicorn

    print("🚀 Starting Mock Bank API Server...")
    print("📍 Server URL: http://127.0.0.1:8000")
    print("📖 API Docs: http://127.0.0.1:8000/docs")
    uvicorn.run(app, host="127.0.0.1", port=8000)
