"""
Retirement intelligence for FinTrack: transaction-driven analysis + goal-based planning.

All spending/income metrics use Transaction rows scoped by user_id (all accounts).
Manual goal fields (RetirementGoal) power corpus math and retirement age for simulations.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy import func
from sqlalchemy.orm import joinedload

from models import (
    BankAccount,
    Category,
    LinkedAccount,
    RetirementGoal,
    RetirementPlan,
    Transaction,
    db,
)

# Default labels (matched against live Category.name from DB)
_ESSENTIAL_LABELS = frozenset(
    {"Rent/EMI", "Groceries", "Utilities", "Fuel", "Transport"}
)
_NON_ESSENTIAL_LABELS = frozenset(
    {
        "Food & Drink",
        "Shopping",
        "Entertainment",
        "Subscriptions",
    }
)

# Income detection
_SMALL_CREDIT_CEILING = 100.0  # ignore tiny credits (UPI rewards / cashback noise)
_LARGE_INFLOW_FLOOR = 5000.0
_REFUND_RE = re.compile(
    r"refund|cash\s*back|cashback|reward\s*point|reversal|reversed|cb\s*",
    re.I,
)
_SALARY_LIKE_RE = re.compile(
    r"salary|payroll|stipend|neft\s*cr|imps\s*cr|credit\s*by\s*transfer|"
    r"sal\s|monthly\s*pay|pf\s*from|employer",
    re.I,
)
_EMI_RE = re.compile(
    r"\bemi\b|loan\s*repay|ach\s*dr\s*loan|personal\s*loan|home\s*loan|car\s*loan",
    re.I,
)
_SUBSCRIPTION_RE = re.compile(
    r"netflix|spotify|hotstar|prime\s*video|youtube\s*premium|apple\s*icloud|"
    r"google\s*one|sonyliv|zee5|jiocinema",
    re.I,
)
_FOOD_DELIVERY_RE = re.compile(
    r"swiggy|zomato|uber\s*eats|dominos|pizza\s*hut",
    re.I,
)

# Portfolio scenarios (annual nominal returns) — user goal inflation used where applicable
PORTFOLIOS = {
    "safe": {"name": "FD + PPF", "return": 0.065},
    "balanced": {"name": "Mutual Funds SIP", "return": 0.10},
    "aggressive": {"name": "Equity (Nifty)", "return": 0.12},
}

DEFAULT_INFLATION = 0.06
ANALYSIS_MONTHS = 6
TREND_MONTHS_MIN = 3
_RECURRING_MIN_MONTHS = 2
_RECURRING_MIN_AMOUNT = 3000.0
_LARGE_CREDIT_THRESHOLD = 8000.0

_INTERNAL_TRANSFER_RE = re.compile(
    r"self\s*transfer|to\s*self|internal\s*transfer|between\s*(my\s*)?accounts|"
    r"from\s+savings\s+to\s+current|wallet\s+to\s+bank|transfer\s+to\s+own|"
    r"own\s*a/c|own\s*account",
    re.I,
)

# Merchant hints for uncategorized debits (description match)
MERCHANT_BUCKETS = {
    "food_delivery": (
        "swiggy",
        "zomato",
        "dominos",
        "pizza hut",
        "mcdonald",
        "kfc",
        "burger king",
    ),
    "shopping": ("amazon", "flipkart", "myntra", "ajio", "nykaa", "dmart", "bigbasket"),
    "entertainment": ("netflix", "prime video", "hotstar", "spotify", "gaana", "zee5"),
    "transport": ("ola", "uber", "rapido"),
}


def _category_role_from_db_name(name: Optional[str]) -> str:
    """
    essential | non_essential | unknown — uses live Category names plus keyword fallback.
    Unknown defaults to non-essential (conservative for non-essential spending warnings).
    """
    if not name:
        return "non_essential"
    n = name.strip()
    if n in _ESSENTIAL_LABELS:
        return "essential"
    if n in _NON_ESSENTIAL_LABELS:
        return "non_essential"
    low = n.lower()
    if any(k in low for k in ("rent", "emi", "grocer", "utility", "electric", "fuel", "petrol", "diesel", "metro", "transport")):
        return "essential"
    if any(k in low for k in ("food", "drink", "shop", "entertain", "subscription", "movie", "game")):
        return "non_essential"
    return "non_essential"


def _txn_excluded_as_income(desc: str) -> bool:
    return bool(_REFUND_RE.search(desc or ""))


def _txn_counts_as_income(
    txn: Transaction,
    cat_name: Optional[str],
    typical_spending: frozenset,
) -> bool:
    """Robust income: credit, Income category, or large non-spending inflow (India transfers)."""
    amt = float(txn.amount or 0)
    desc = txn.description or ""
    ttype = (txn.transaction_type or "").lower()

    if _txn_excluded_as_income(desc):
        return False

    if cat_name and cat_name.strip() == "Income":
        return amt > 0

    if ttype == "credit":
        if amt < _SMALL_CREDIT_CEILING:
            return False
        return True

    # Mis-labeled salary / transfer: debit or null but looks like salary & not shopping
    if amt >= _LARGE_INFLOW_FLOOR and _SALARY_LIKE_RE.search(desc):
        c = (cat_name or "").strip()
        if c not in typical_spending and c not in ("Income",):
            return True
        if not c or c in ("Uncategorized", "Other", "Payments"):
            return True

    return False


def _txn_counts_as_expense(txn: Transaction, is_income: bool) -> bool:
    if is_income:
        return False
    ttype = (txn.transaction_type or "").lower()
    if ttype == "credit":
        return False
    return True


def _normalize_income_source(desc: str) -> str:
    s = re.sub(r"[^a-z0-9]+", " ", (desc or "").lower())[:48]
    return s.strip() or "unknown"


def _account_key_txn(txn: Transaction) -> str:
    if txn.account_id is not None:
        return f"linked:{txn.account_id}"
    if txn.bank_account_id is not None:
        return f"legacy:{txn.bank_account_id}"
    return "unknown"


def _account_labels_for_user(user_id: int) -> Dict[str, str]:
    labels: Dict[str, str] = {}
    for la in LinkedAccount.query.filter_by(user_id=user_id).all():
        labels[f"linked:{la.id}"] = la.account_nickname or f"Linked #{la.id}"
    for ba in BankAccount.query.filter_by(user_id=user_id).all():
        labels[f"legacy:{ba.id}"] = ba.account_name or f"Account #{ba.id}"
    labels["unknown"] = "Unassigned"
    return labels


def _detect_transfer_transaction_ids(txns: List[Transaction]) -> Set[int]:
    """Same-day amount-matched debit/credit pairs + explicit self-transfer narrations."""
    transfer_ids: Set[int] = set()
    for txn in txns:
        blob = f"{txn.description or ''} {txn.narration or ''}"
        if _INTERNAL_TRANSFER_RE.search(blob):
            transfer_ids.add(txn.id)

    buckets: Dict[Tuple[date, float], List[Transaction]] = defaultdict(list)
    for txn in txns:
        if not txn.date:
            continue
        amt = round(float(txn.amount or 0), 2)
        if amt <= 0:
            continue
        buckets[(txn.date.date(), amt)].append(txn)

    for group in buckets.values():
        if len(group) < 2:
            continue
        credits = [t for t in group if (t.transaction_type or "").lower() == "credit"]
        debits = [t for t in group if (t.transaction_type or "").lower() != "credit"]
        if not credits or not debits:
            continue
        keys = {_account_key_txn(t) for t in group}
        if len(keys) >= 2 or (len(credits) == 1 and len(debits) == 1 and len(group) == 2):
            for t in group:
                transfer_ids.add(t.id)
    return transfer_ids


def _build_recurring_source_months(
    txns: List[Transaction], transfer_ids: Set[int]
) -> Dict[str, Set[str]]:
    """source_key -> set of YYYY-MM where a large credit appeared (non-transfer)."""
    src_months: Dict[str, Set[str]] = defaultdict(set)
    for txn in txns:
        if txn.id in transfer_ids:
            continue
        if (txn.transaction_type or "").lower() != "credit":
            continue
        if _txn_excluded_as_income(txn.description or ""):
            continue
        amt = float(txn.amount or 0)
        if amt < _SMALL_CREDIT_CEILING:
            continue
        cat_name = txn.category.name if txn.category else None
        if cat_name and cat_name.strip() == "Income":
            src = "category:income"
        else:
            src = _normalize_income_source(txn.description or "")
        mk = _month_key(txn.date) if txn.date else None
        if not mk:
            continue
        if amt >= _RECURRING_MIN_AMOUNT or (cat_name and cat_name.strip() == "Income"):
            src_months[src].add(mk)
    return dict(src_months)


def _is_recurring_source(src: str, months_with_credit: Set[str]) -> bool:
    return len(months_with_credit) >= _RECURRING_MIN_MONTHS


def _advanced_income_flags(
    txn: Transaction,
    cat_name: Optional[str],
    typical: frozenset,
    transfer_ids: Set[int],
    recurring_sources: Set[str],
) -> Tuple[bool, bool, str]:
    """
    Returns (is_income, is_expense, income_bucket) where income_bucket is
    primary | secondary | none.
    """
    if txn.id in transfer_ids:
        # Exclude both legs of internal transfers from income and expense totals.
        return False, False, "none"

    desc = txn.description or ""
    narr = txn.narration or ""
    ttype = (txn.transaction_type or "").lower()
    amt = float(txn.amount or 0)

    if _txn_excluded_as_income(desc):
        if ttype != "credit":
            return False, True, "none"
        return False, False, "none"

    if cat_name and cat_name.strip() == "Income" and amt > 0:
        return True, False, "primary"

    if ttype == "credit" and amt >= _SMALL_CREDIT_CEILING:
        src = _normalize_income_source(desc)
        if "category:income" in recurring_sources or src in recurring_sources:
            return True, False, "primary"
        if _SALARY_LIKE_RE.search(desc) or _SALARY_LIKE_RE.search(narr):
            return True, False, "primary"
        if amt >= _LARGE_CREDIT_THRESHOLD:
            return True, False, "secondary"
        return True, False, "secondary"

    if amt >= _LARGE_INFLOW_FLOOR and _SALARY_LIKE_RE.search(desc):
        c = (cat_name or "").strip()
        if c not in typical and c not in ("Income",):
            return True, False, "primary"
        if not c or c in ("Uncategorized", "Other", "Payments"):
            return True, False, "primary"

    is_expense = ttype != "credit"
    return False, is_expense, "none"


def _advanced_monthly_and_accounts(
    user_id: int, since: datetime, typical: frozenset
) -> Tuple[
    Dict[str, Tuple[float, float, float, float]],
    List[Transaction],
    Dict[str, Any],
    Dict[str, Any],
    Set[int],
]:
    """
    month -> (total_income, primary_income, secondary_income, expense).
    Also builds account_analysis and income metadata.
    """
    txns = (
        Transaction.query.options(joinedload(Transaction.category))
        .filter(Transaction.user_id == user_id, Transaction.date >= since)
        .order_by(Transaction.date)
        .all()
    )
    transfer_ids = _detect_transfer_transaction_ids(txns)
    src_months = _build_recurring_source_months(txns, transfer_ids)
    recurring_sources = {s for s, m in src_months.items() if _is_recurring_source(s, m)}
    if any((txn.category and txn.category.name == "Income") for txn in txns):
        recurring_sources.add("category:income")

    monthly: Dict[str, Tuple[float, float, float, float]] = {}
    acct_income_credits: Dict[str, float] = defaultdict(float)
    acct_debit: Dict[str, float] = defaultdict(float)
    acct_activity: Dict[str, int] = defaultdict(int)

    for txn in txns:
        cat_name = txn.category.name if txn.category else None
        inc, exp, bucket = _advanced_income_flags(
            txn, cat_name, typical, transfer_ids, recurring_sources
        )
        mk = _month_key(txn.date) if txn.date else None
        if not mk:
            continue
        key = _account_key_txn(txn)
        amt = float(txn.amount or 0)
        acct_activity[key] += 1
        if exp:
            acct_debit[key] += amt
        if inc:
            acct_income_credits[key] += amt
        cur = monthly.get(mk, (0.0, 0.0, 0.0, 0.0))
        tot_i, p_i, s_i, e = cur
        if inc:
            tot_i += amt
            if bucket == "primary":
                p_i += amt
            elif bucket == "secondary":
                s_i += amt
        if exp:
            e += amt
        monthly[mk] = (tot_i, p_i, s_i, e)

    labels = _account_labels_for_user(user_id)
    known_keys = [k for k in acct_activity if k != "unknown"]

    def _pick_max(d: Dict[str, float], keys: List[str]) -> Optional[str]:
        best = None
        best_v = -1.0
        for k in keys:
            v = d.get(k, 0.0)
            if v > best_v:
                best_v = v
                best = k
        return best

    salary_key = _pick_max(acct_income_credits, known_keys) or (
        _pick_max(acct_income_credits, list(acct_income_credits.keys()))
    )
    spend_key = _pick_max(acct_debit, known_keys) or _pick_max(
        acct_debit, list(acct_debit.keys())
    )

    def _activity_score(k: str) -> float:
        return acct_activity.get(k, 0) + acct_debit.get(k, 0.0) / 10000.0

    savings_key = None
    if len(known_keys) >= 2:
        savings_key = min(known_keys, key=_activity_score)
    elif len(known_keys) == 1:
        savings_key = None

    account_analysis = {
        "salary_account": labels.get(salary_key, salary_key or "—") if salary_key else "—",
        "high_spend_account": labels.get(spend_key, spend_key or "—") if spend_key else "—",
        "savings_account": labels.get(savings_key, savings_key or "—")
        if savings_key
        else (labels.get("unknown", "—") if not known_keys else "—"),
    }

    month_keys = sorted(monthly.keys())
    if month_keys:
        n = len(month_keys)
        tot_pri = sum(monthly[m][1] for m in month_keys)
        tot_sec = sum(monthly[m][2] for m in month_keys)
        primary_avg = tot_pri / n
        secondary_avg = tot_sec / n
    else:
        primary_avg = secondary_avg = 0.0

    inc_conf = "low"
    if len(month_keys) >= 3:
        inc_conf = "high"
    elif len(month_keys) == 2:
        inc_conf = "medium"

    income_analysis = {
        "primary_income": round(primary_avg, 0),
        "secondary_income": round(secondary_avg, 0),
        "confidence": inc_conf,
        "transfer_exclusions": len(transfer_ids),
    }

    return monthly, txns, income_analysis, account_analysis, transfer_ids


def _month_key(d: datetime) -> str:
    return d.strftime("%Y-%m")


def _monthly_income_and_expense(
    user_id: int, since: datetime
) -> Tuple[Dict[str, Tuple[float, float]], List[Transaction], frozenset]:
    """
    Returns (month -> (income_sum, expense_sum)), transactions list, typical_spending set.
    Income uses credit OR Income category OR salary-like large inflows; expenses are remaining debits.
    """
    typical = frozenset(_ESSENTIAL_LABELS | _NON_ESSENTIAL_LABELS | {"Payments", "Other", "Uncategorized"})

    txns = (
        Transaction.query.options(joinedload(Transaction.category))
        .filter(Transaction.user_id == user_id, Transaction.date >= since)
        .order_by(Transaction.date)
        .all()
    )

    monthly: Dict[str, Tuple[float, float]] = {}
    for txn in txns:
        cat_name = txn.category.name if txn.category else None
        inc = _txn_counts_as_income(txn, cat_name, typical)
        exp = _txn_counts_as_expense(txn, inc)
        mk = _month_key(txn.date) if txn.date else None
        if not mk:
            continue
        inc_amt = float(txn.amount or 0) if inc else 0.0
        exp_amt = float(txn.amount or 0) if exp else 0.0
        cur_i, cur_e = monthly.get(mk, (0.0, 0.0))
        monthly[mk] = (cur_i + inc_amt, cur_e + exp_amt)

    return monthly, txns, typical


def _estimated_income_from_top_credits(
    txns: List[Transaction], exclude_ids: Optional[Set[int]] = None
) -> float:
    """Fallback when detected monthly income is ~0: avg of top 3 credit amounts (ex-refund)."""
    excl = exclude_ids or set()
    credits: List[float] = []
    for txn in txns:
        if txn.id in excl:
            continue
        if (txn.transaction_type or "").lower() != "credit":
            continue
        if _txn_excluded_as_income(txn.description or ""):
            continue
        amt = float(txn.amount or 0)
        if amt < _SMALL_CREDIT_CEILING:
            continue
        credits.append(amt)
    credits.sort(reverse=True)
    top = credits[:3]
    if not top:
        return 0.0
    return sum(top) / len(top)


def _india_behavior_signals(
    txns: List[Transaction], exclude_ids: Optional[Set[int]] = None
) -> Dict[str, Any]:
    """Lightweight India-specific signals from descriptions (no schema change)."""
    excl = exclude_ids or set()
    emi = sub = food = 0.0
    small_upi = 0
    for txn in txns:
        if txn.id in excl:
            continue
        if (txn.transaction_type or "").lower() == "credit":
            continue
        amt = float(txn.amount or 0)
        desc = txn.description or ""
        dlow = desc.lower()
        if _EMI_RE.search(desc):
            emi += amt
        if _SUBSCRIPTION_RE.search(desc):
            sub += amt
        if _FOOD_DELIVERY_RE.search(desc):
            food += amt
        if "upi" in dlow and amt < 500:
            small_upi += 1
    return {
        "emi_like_total": round(emi, 0),
        "subscription_like_total": round(sub, 0),
        "food_delivery_like_total": round(food, 0),
        "small_upi_debit_count": small_upi,
    }


def _classify_uncategorized_merchants(
    user_id: int,
    since: datetime,
    exclude_transaction_ids: Optional[Set[int]] = None,
) -> Dict[str, float]:
    """Bucket uncategorized debits by merchant substring (optional signal)."""
    excl = exclude_transaction_ids or set()
    q = Transaction.query.filter(
        Transaction.user_id == user_id,
        Transaction.date >= since,
        Transaction.category_id.is_(None),
        (Transaction.transaction_type == "debit") | (Transaction.transaction_type.is_(None)),
    )
    if excl:
        q = q.filter(~Transaction.id.in_(excl))
    txns = q.all()
    buckets: Dict[str, float] = {}
    for txn in txns:
        desc = (txn.description or "").lower()
        for bucket, needles in MERCHANT_BUCKETS.items():
            if any(n in desc for n in needles):
                buckets[bucket] = buckets.get(bucket, 0.0) + float(txn.amount or 0)
                break
    return buckets


def get_expense_breakdown(
    user_id: int,
    since: datetime,
    exclude_transaction_ids: Optional[Set[int]] = None,
) -> Dict[str, Any]:
    """Category totals from debits; essential vs non-essential by category name."""
    excl = exclude_transaction_ids or set()
    base_cat = [
        Transaction.user_id == user_id,
        Transaction.date >= since,
        (Transaction.transaction_type == "debit")
        | (Transaction.transaction_type.is_(None)),
    ]
    if excl:
        base_cat.append(~Transaction.id.in_(excl))

    rows = (
        db.session.query(Category.name, func.sum(Transaction.amount).label("total"))
        .join(Transaction, Transaction.category_id == Category.id)
        .filter(*base_cat)
        .group_by(Category.id, Category.name)
        .all()
    )
    cat_totals = {row.name: float(row.total or 0) for row in rows}

    uncat_filters = [
        Transaction.user_id == user_id,
        Transaction.date >= since,
        Transaction.category_id.is_(None),
        (Transaction.transaction_type == "debit")
        | (Transaction.transaction_type.is_(None)),
    ]
    if excl:
        uncat_filters.append(~Transaction.id.in_(excl))
    uncat = (
        db.session.query(func.coalesce(func.sum(Transaction.amount), 0))
        .filter(*uncat_filters)
        .scalar()
        or 0
    )
    uncat_f = float(uncat)

    merchant_extra = _classify_uncategorized_merchants(user_id, since, excl)
    merchant_sum = sum(merchant_extra.values())

    essential: Dict[str, float] = {}
    non_essential: Dict[str, float] = {}
    for name, amt in cat_totals.items():
        role = _category_role_from_db_name(name)
        if role == "essential":
            essential[name] = amt
        else:
            non_essential[name] = amt

    if uncat_f > 0:
        # Merchant buckets are a subset of uncategorized debits—avoid double-counting.
        non_essential["Uncategorized"] = non_essential.get("Uncategorized", 0.0) + max(
            0.0, uncat_f - merchant_sum
        )

    for bucket, amt in merchant_extra.items():
        key = f"{bucket.replace('_', ' ').title()} (detected)"
        non_essential[key] = non_essential.get(key, 0.0) + amt

    def top_items(d: Dict[str, float], limit: int = 8) -> List[List[Any]]:
        items = sorted(d.items(), key=lambda x: -x[1])[:limit]
        return [[k, round(v, 0)] for k, v in items]

    ess_total = sum(essential.values())
    non_total = sum(non_essential.values())

    return {
        "essential": top_items(essential),
        "non_essential": top_items(non_essential),
        "essential_total": round(ess_total, 0),
        "non_essential_total": round(non_total, 0),
        "uncategorized": round(uncat_f, 0),
    }


def analyze_user_finances(user_id: int, months_back: int = ANALYSIS_MONTHS) -> Dict[str, Any]:
    """
    Income: multi-layer detection (accounts, recurring sources, transfer exclusions).
    Expenses: debits excluding internal transfer legs.
    """
    since = datetime.now() - timedelta(days=months_back * 30)
    typical = frozenset(
        _ESSENTIAL_LABELS | _NON_ESSENTIAL_LABELS | {"Payments", "Other", "Uncategorized"}
    )
    monthly, txns, income_analysis, account_analysis, transfer_ids = (
        _advanced_monthly_and_accounts(user_id, since, typical)
    )
    month_keys = sorted(monthly.keys())

    if not month_keys:
        return {
            "has_data": False,
            "message": "No transactions in the analysis window — link accounts and sync.",
        }

    incomes = [monthly[m][0] for m in month_keys]
    debits = [monthly[m][3] for m in month_keys]
    avg_income = sum(incomes) / len(month_keys)
    avg_expense = sum(debits) / len(month_keys)

    estimated_income: Optional[float] = None
    if avg_income <= 0:
        est = _estimated_income_from_top_credits(txns, transfer_ids)
        if est > 0:
            estimated_income = round(est, 0)
            avg_income = float(estimated_income)

    if len(month_keys) == 1:
        confidence = "low"
    elif len(month_keys) == 2:
        confidence = "medium"
    else:
        confidence = "high"

    inc_conf = income_analysis.get("confidence") or confidence
    if confidence == "low" or inc_conf == "low":
        confidence = "low"
    elif confidence == "medium" or inc_conf == "medium":
        confidence = "medium"
    else:
        confidence = "high"

    # Salary-account signal: recurring credits on one named account → slightly higher income confidence
    sa = account_analysis.get("salary_account") or ""
    if sa and str(sa).strip() not in ("—", "Unassigned", ""):
        if confidence == "low" and len(month_keys) >= 2:
            confidence = "medium"
        elif confidence == "medium" and len(month_keys) >= 3:
            confidence = "high"

    cash_flow = avg_income - avg_expense
    surplus = max(0.0, cash_flow)
    savings_rate = (cash_flow / avg_income * 100.0) if avg_income > 0 else 0.0

    breakdown = get_expense_breakdown(user_id, since, transfer_ids)
    india = _india_behavior_signals(txns, transfer_ids)

    investable = {
        "conservative": surplus * 0.30,
        "moderate": surplus * 0.50,
        "aggressive": surplus * 0.70,
        "optimized": surplus
        + (breakdown["non_essential_total"] * 0.25 if breakdown["non_essential_total"] else 0),
    }

    classification = "balanced"
    if savings_rate < 10:
        classification = "high_spender"
    elif savings_rate > 25:
        classification = "saver"

    return {
        "has_data": True,
        "confidence": confidence,
        "months_sampled": len(month_keys),
        "avg_monthly_income": round(avg_income, 0),
        "avg_monthly_expense": round(avg_expense, 0),
        "surplus": round(surplus, 0),
        "savings_rate": round(savings_rate, 1),
        "classification": classification,
        "estimated_income": estimated_income,
        "india_signals": india,
        "income_analysis": income_analysis,
        "account_analysis": account_analysis,
        "expenses": {
            "essential": breakdown["essential"],
            "non_essential": breakdown["non_essential"],
            "essential_total": breakdown["essential_total"],
            "non_essential_total": breakdown["non_essential_total"],
            "uncategorized": breakdown["uncategorized"],
        },
        "investable_surplus": investable,
        "trend_series": {
            "months": month_keys,
            "monthly_income": [round(monthly[m][0], 0) for m in month_keys],
            "monthly_expense": [round(monthly[m][3], 0) for m in month_keys],
        },
    }


def generate_behavioral_insights(
    finances: Dict[str, Any], trends: Optional[Dict[str, Any]] = None
) -> List[Dict[str, Any]]:
    if not finances.get("has_data"):
        return []

    trends = trends or {}
    insights: List[Dict[str, Any]] = []
    surplus = float(finances["surplus"] or 0)
    income = float(finances["avg_monthly_income"] or 0)
    rate = float(finances["savings_rate"] or 0)
    expenses = finances["expenses"]
    ess_total = float(expenses.get("essential_total") or 0)
    non_ess_total = float(expenses.get("non_essential_total") or 0)
    avg_exp = float(finances["avg_monthly_expense"] or 1)
    india = finances.get("india_signals") or {}
    conf = finances.get("confidence", "medium")

    if finances.get("estimated_income"):
        est = _round_monthly_display(float(finances["estimated_income"]))
        insights.append(
            {
                "emoji": "",
                "text": f"Reported salary-like inflows were thin, so income is estimated at about "
                f"₹{est:,.0f} per month from your largest credits—tag salary as Income when you can "
                "so this view stays accurate.",
            }
        )

    food = float(india.get("food_delivery_like_total") or 0)
    if food > 0:
        insights.append(
            {
                "emoji": "",
                "text": "Food-delivery and quick-service merchants show up regularly—cap that bucket "
                "before chasing smaller cuts elsewhere.",
                "action": "Cap weekly food delivery",
            }
        )

    inc_trend = trends.get("income") or "unknown"
    sav_trend = trends.get("savings") or "unknown"

    if income > 0 and non_ess_total > income * 1.02:
        insights.append(
            {
                "emoji": "",
                "text": "Measured non-essential outflows exceed income in this window—confirm salary "
                "tagging and transfer noise before you size cuts.",
                "priority": "high",
            }
        )

    if inc_trend == "moderate":
        insights.append(
            {
                "emoji": "",
                "text": "Your income has varied from month to month—schedule discretionary purchases "
                "after you confirm this month’s inflow, not before.",
            }
        )

    if income > 0 and rate >= 0:
        if rate < 15:
            insights.append(
                {
                    "emoji": "",
                    "text": f"At about {round(rate, 1)}% of income kept after expenses, your savings rate is "
                    "on the low side for many retirement plans, which slows how quickly wealth builds.",
                    "priority": "high",
                }
            )
        elif rate < 22:
            insights.append(
                {
                    "emoji": "",
                    "text": f"Your savings rate is about {round(rate, 1)}% of income—healthy, but there may "
                    "still be room to reach common planning bands without major lifestyle shocks.",
                }
            )
        else:
            insights.append(
                {
                    "emoji": "",
                    "text": f"Your savings rate is about {round(rate, 1)}% of income, which supports "
                    "steady progress if you keep investing consistently.",
                    "type": "positive",
                }
            )

    emi = float(india.get("emi_like_total") or 0)
    if emi > 0:
        insights.append(
            {
                "emoji": "",
                "text": "Loan or EMI-style wording appears often in debits—label those separately "
                "from daily spend so fixed costs do not hide inside generic categories.",
            }
        )

    sub = float(india.get("subscription_like_total") or 0)
    if sub > 0:
        insights.append(
            {
                "emoji": "",
                "text": "Streaming and app renewals show up in the ledger—calendar a quarterly review "
                "so unused plans do not auto-renew quietly.",
            }
        )

    if india.get("small_upi_debit_count", 0) > 35:
        insights.append(
            {
                "emoji": "",
                "text": "Very small UPI purchases appear in high volume—check weekly totals; "
                "the pattern matters more than any single tap.",
            }
        )

    if avg_exp > 0 and ess_total / avg_exp > 0.50:
        insights.append(
            {
                "emoji": "",
                "text": "Rent, EMIs, utilities, and transport make up a large share of spending, so "
                "non-essential cuts tend to be the safer first lever than trying to shrink fixed bills.",
            }
        )

    if surplus > 0 and sav_trend not in ("consistent", "mixed"):
        insights.append(
            {
                "emoji": "",
                "text": "You usually keep some cash after core bills—route it to investments the same "
                "day income lands, before optional spending expands.",
                "type": "positive",
            }
        )

    aa = finances.get("account_analysis") or {}
    sal = (aa.get("salary_account") or "").strip()
    spend_acct = (aa.get("high_spend_account") or "").strip()
    if (
        sal
        and spend_acct
        and sal not in ("—", "Unassigned")
        and spend_acct not in ("—", "Unassigned")
        and sal != spend_acct
    ):
        insights.append(
            {
                "emoji": "",
                "text": f"Money tends to arrive in “{sal}” while “{spend_acct}” sees the heaviest "
                "outflows—a separate savings account can keep investable cash from blending with daily spend.",
            }
        )
    elif spend_acct and spend_acct not in ("—", "Unassigned"):
        insights.append(
            {
                "emoji": "",
                "text": f"Most outflows run through “{spend_acct}”; splitting everyday spend from "
                "long-term savings accounts can make habits easier to track.",
            }
        )

    if conf == "low":
        insights.append(
            {
                "emoji": "",
                "text": "Based on limited data: treat these patterns as directional until more months "
                "sync, then revisit income tags and categories.",
            }
        )

    if len(insights) < 2:
        insights.append(
            {
                "emoji": "",
                "text": "Clear categories for income, rent or EMI, food, and transfers improve how "
                "precise this guidance can be.",
            }
        )

    out: List[Dict[str, Any]] = []
    seen_text: Set[str] = set()
    for item in insights:
        t = item.get("text") or ""
        if t in seen_text:
            continue
        seen_text.add(t)
        out.append(item)
    return out

def _humanize_spend_label(category_name: str) -> str:
    if not category_name:
        return "non-essential spending"
    base = category_name.replace(" (detected)", "").strip()
    if base == "Uncategorized":
        return "miscellaneous expenses"
    return base


def _round_monthly_display(n: float) -> float:
    """Round monthly amounts for display copy only; calculations stay exact elsewhere."""
    n = float(n)
    if n <= 0:
        return 0.0
    if n < 1000:
        return round(n, -1)
    return round(n / 1000) * 1000


def _format_inr_monthly_phrase(n: float) -> str:
    v = _round_monthly_display(n)
    return f"approximately ₹{v:,.0f} per month"


def _format_inr_future_lump(n: float, with_investing_note: bool = False) -> str:
    """Format long-term totals as lakh or crore for readability."""
    n = float(n)
    if n <= 0:
        base = "approximately ₹0"
    else:
        an = abs(n)
        if an >= 1e7:
            x = n / 1e7
            s = f"{x:.1f}".rstrip("0").rstrip(".")
            base = f"approximately ₹{s} crore"
        elif an >= 1e5:
            x = n / 1e5
            s = f"{x:.1f}".rstrip("0").rstrip(".")
            base = f"approximately ₹{s} lakh"
        else:
            v = round(n / 1000) * 1000 if an >= 1000 else round(n)
            base = f"approximately ₹{v:,.0f}"
    if with_investing_note and n > 0:
        return base + " over the long term (assuming consistent investing)"
    return base


def _action_headline_for_category(top_raw: str, human_label: str) -> str:
    if not top_raw:
        return "Clarify spending categories"
    base = (top_raw or "").replace(" (detected)", "").strip().lower()
    if base == "uncategorized" or human_label.lower().startswith("miscellaneous"):
        return "Reduce miscellaneous expenses"
    if "food" in human_label.lower() or "delivery" in human_label.lower():
        return "Reduce food delivery and dining-out spending"
    if "subscription" in human_label.lower():
        return "Review subscription and recurring charges"
    if "shop" in human_label.lower():
        return "Reduce shopping and retail spending"
    return f"Lower spending on {human_label}"


def _confidence_rank(conf: Optional[str]) -> int:
    return {"high": 3, "medium": 2, "low": 1}.get((conf or "medium").lower(), 2)


def _min_retire_age_for_monthly(
    current_age: int,
    goal_retire_age: int,
    monthly_sip: float,
    target_corpus: float,
    ann: float,
) -> int:
    if monthly_sip <= 0 or target_corpus <= 0:
        return goal_retire_age
    optimized_age = goal_retire_age
    for age in range(current_age + 1, goal_retire_age + 1):
        y = age - current_age
        if y <= 0:
            continue
        if sip_future_value(monthly_sip, ann, y) >= target_corpus * 0.995:
            optimized_age = age
            break
    return max(current_age, min(optimized_age, goal_retire_age))


def sip_future_value(monthly_sip: float, annual_rate: float, years: int) -> float:
    monthly_rate = annual_rate / 12.0
    months = years * 12
    if monthly_sip <= 0:
        return 0.0
    if monthly_rate <= 0:
        return monthly_sip * months
    return (
        monthly_sip
        * ((math.pow(1 + monthly_rate, months) - 1) / monthly_rate)
        * (1 + monthly_rate)
    )


def simulate_indian_portfolios(
    finances: Dict[str, Any],
    current_age: int,
    retire_age: int,
    inflation_rate: float,
) -> Dict[str, Any]:
    years = max(1, retire_age - current_age)
    monthly_invest = float(finances["investable_surplus"]["moderate"])
    opt_invest = float(finances["investable_surplus"]["optimized"])

    scenarios: Dict[str, Any] = {}
    for key, portfolio in PORTFOLIOS.items():
        ann = portfolio["return"]
        corpus = sip_future_value(monthly_invest, ann, years)
        real_corpus = corpus / ((1 + inflation_rate) ** years) if inflation_rate >= 0 else corpus
        monthly_income = real_corpus * 0.04 / 12.0
        scenarios[key] = {
            "name": portfolio["name"],
            "return_pct": round(ann * 100, 1),
            "corpus": round(corpus, 0),
            "monthly_income": round(monthly_income, 0),
            "years": years,
        }

    ann_opt = 0.10
    opt_corpus = sip_future_value(opt_invest, ann_opt, years)
    real_opt = opt_corpus / ((1 + inflation_rate) ** years) if inflation_rate >= 0 else opt_corpus
    scenarios["optimized"] = {
        "name": "Moderate surplus plus a modest cut in non-essential spending (long-term estimate)",
        "return_pct": 10.0,
        "corpus": round(opt_corpus, 0),
        "monthly_income": round(real_opt * 0.04 / 12.0, 0),
        "years": years,
    }
    return scenarios


def compute_trends(finances: Dict[str, Any]) -> Dict[str, Any]:
    ts = finances.get("trend_series") or {}
    months = ts.get("months") or []
    exp = [float(x) for x in (ts.get("monthly_expense") or [])]
    inc = [float(x) for x in (ts.get("monthly_income") or [])]
    n = min(6, len(exp), len(inc))
    if n < TREND_MONTHS_MIN:
        return {
            "spending": "unknown",
            "income": "unknown",
            "savings": "unknown",
            "months_used": n,
        }
    exp_n = exp[-n:]
    inc_n = inc[-n:]
    x = list(range(n))
    exp_mean = sum(exp_n) / n
    mx = sum(x) / n
    my_exp = sum(exp_n) / n

    def _slope(y_vals: List[float]) -> float:
        my = sum(y_vals) / n
        num = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y_vals))
        den = sum((xi - mx) ** 2 for xi in x) or 1.0
        return num / den

    se = _slope(exp_n)
    thr = max(500.0, exp_mean * 0.04)
    if se > thr:
        spend_trend = "increasing"
    elif se < -thr:
        spend_trend = "decreasing"
    else:
        spend_trend = "stable"

    inc_mean = sum(inc_n) / n
    if inc_mean > 0 and n >= 3:
        var = sum((x - inc_mean) ** 2 for x in inc_n) / (n - 1)
        cv = math.sqrt(var) / inc_mean
        if cv > 0.28:
            income_vol = "volatile"
        elif cv > 0.14:
            income_vol = "moderate"
        else:
            income_vol = "stable"
    elif inc_mean > 0:
        income_vol = "moderate"
    else:
        income_vol = "unknown"

    sur = [inc_n[i] - exp_n[i] for i in range(n)]
    if sur:
        pos = sum(1 for s in sur if s > 0)
        neg_ratio = 1 - pos / n
        if neg_ratio > 0.34:
            sav_cons = "inconsistent"
        elif neg_ratio > 0.12:
            sav_cons = "mixed"
        else:
            sav_cons = "consistent"
    else:
        sav_cons = "unknown"

    return {
        "spending": spend_trend,
        "income": income_vol,
        "savings": sav_cons,
        "months_used": n,
    }


def compute_financial_health(
    finances: Dict[str, Any], trends: Dict[str, Any]
) -> Dict[str, Any]:
    score = 52
    reasons: List[str] = []
    rate = float(finances.get("savings_rate") or 0)
    if rate >= 28:
        score += 18
    elif rate >= 20:
        score += 12
    elif rate >= 12:
        score += 6
    else:
        score -= 12
        reasons.append(
            "non-essential spending leaves little room to save"
            if rate < 10
            else "savings rate is below a comfortable planning range"
        )

    income = float(finances.get("avg_monthly_income") or 0)
    non_ess = float(finances.get("expenses", {}).get("non_essential_total") or 0)
    if income > 0:
        disc_pct = non_ess / income * 100
        if disc_pct > 38:
            score -= 14
            reasons.append("non-essential spending is a large share of income")
        elif disc_pct > 28:
            score -= 6

    if trends.get("spending") == "increasing":
        score -= 8
        reasons.append("spending is trending up")
    if trends.get("income") == "volatile":
        score -= 6
        reasons.append("income swings month to month")
    if trends.get("savings") == "inconsistent":
        score -= 8
        reasons.append("surplus is uneven across months")

    score = int(max(0, min(100, score)))
    if score >= 72:
        level = "good"
    elif score >= 52:
        level = "average"
    else:
        level = "needs_attention"

    reason = reasons[0] if reasons else "cash flow is workable; small habit shifts help"
    return {"score": score, "level": level, "reason": reason}


def compute_retirement_projection(
    finances: Dict[str, Any],
    current_age: int,
    baseline_retire_age: int,
) -> Dict[str, Any]:
    years = max(1, baseline_retire_age - current_age)
    monthly_mod = max(0.0, float(finances["investable_surplus"]["moderate"]))
    monthly_opt = max(0.0, float(finances["investable_surplus"]["optimized"]))
    non_ess_total = float(finances.get("expenses", {}).get("non_essential_total") or 0)
    ann = 0.10
    target_corpus = sip_future_value(monthly_mod, ann, years)

    # When “optimized” equals baseline SIP, synthesize a modest uplift from discretionary trim
    if monthly_opt <= monthly_mod and non_ess_total > 0:
        monthly_opt = monthly_mod + min(non_ess_total * 0.12, max(2000.0, non_ess_total * 0.08))

    monthly_moderate = monthly_mod + max(0.0, (monthly_opt - monthly_mod) * 0.55)

    base: Dict[str, Any] = {
        "current_age": current_age,
        "baseline_retirement_age": baseline_retire_age,
        "optimized_age": baseline_retire_age,
        "years_gained": 0,
        "years_gained_moderate": 0,
        "years_gained_best": 0,
        "retirement_age_range_text": "",
        "user_age_now": current_age,
    }

    if target_corpus <= 0:
        base["retirement_age_range_text"] = (
            "Build a small monthly surplus (and categorize spending) to see how many years earlier you could retire."
        )
        return base

    if monthly_opt <= monthly_mod:
        base["retirement_age_range_text"] = (
            "Your investable surplus matches the baseline path—freeing even a small amount from "
            "non-essential spending or adding a regular investment can widen the range of outcomes."
        )
        return base

    best_age = _min_retire_age_for_monthly(
        current_age, baseline_retire_age, monthly_opt, target_corpus, ann
    )
    mod_age = _min_retire_age_for_monthly(
        current_age, baseline_retire_age, monthly_moderate, target_corpus, ann
    )

    years_best = max(0, baseline_retire_age - best_age)
    years_mod = max(0, baseline_retire_age - mod_age)

    base["optimized_age"] = best_age
    base["years_gained"] = years_best
    base["years_gained_best"] = years_best
    base["years_gained_moderate"] = years_mod

    if years_mod >= 1 and years_best >= 1:
        lo, hi = (years_mod, years_best) if years_mod <= years_best else (years_best, years_mod)
        if lo == hi:
            yunit = "year" if lo == 1 else "years"
            base["retirement_age_range_text"] = (
                f"You may retire about {lo} {yunit} earlier if you stay consistent with these moves."
            )
        else:
            base["retirement_age_range_text"] = (
                f"You may retire {lo}–{hi} years earlier depending on how consistently you free up and invest that money."
            )
    elif years_best >= 1:
        base["retirement_age_range_text"] = (
            f"In a strong scenario you could retire up to {years_best} years earlier — moderate habits may land in between."
        )
    else:
        base["retirement_age_range_text"] = ""

    return base


def _retirement_timing_line(
    card_index: int, years_for_card: int, retirement_projection: Dict[str, Any]
) -> str:
    ym = int(retirement_projection.get("years_gained_moderate") or 0)
    yb = int(
        retirement_projection.get("years_gained_best")
        or retirement_projection.get("years_gained")
        or 0
    )
    if card_index == 0 and ym >= 1 and yb >= 1 and ym != yb:
        lo, hi = min(ym, yb), max(ym, yb)
        return f"Retire up to {lo}–{hi} years earlier"
    if years_for_card >= 1:
        return f"Retire up to about {years_for_card} years earlier"
    if yb >= 1:
        return f"Retire up to about {yb} years earlier"
    return "Adds flexibility to your retirement timeline as monthly investments increase"


def _action_card_description(
    monthly_impact: float,
    long_term_gain: float,
    card_index: int,
    years_for_card: int,
    retirement_projection: Dict[str, Any],
) -> str:
    mo_r = _round_monthly_display(monthly_impact)
    lt = _format_inr_future_lump(long_term_gain, with_investing_note=True)
    out = _retirement_timing_line(card_index, years_for_card, retirement_projection)
    return (
        f"Impact: about ₹{mo_r:,.0f}/mo; {lt}.\n"
        f"Outcome: {out}."
    )


def generate_action_plan(
    finances: Dict[str, Any],
    scenarios: Dict[str, Any],
    trends: Dict[str, Any],
    current_age: int,
    retire_age: int,
    retirement_projection: Dict[str, Any],
) -> Dict[str, Any]:
    income = float(finances.get("avg_monthly_income") or 0)
    years = max(1, retire_age - current_age)
    non_ess = finances.get("expenses", {}).get("non_essential") or []
    conf = finances.get("confidence", "medium")

    top_raw = non_ess[0][0] if non_ess else ""
    top_name = _humanize_spend_label(top_raw)
    top_amt = float(non_ess[0][1]) if non_ess else 0.0
    cut_pct = 0.15
    cut_disc = round(top_amt * cut_pct, 0)
    lt1 = sip_future_value(cut_disc, 0.10, min(25, years + 5))
    headline1 = (
        _action_headline_for_category(top_raw, top_name)
        if top_amt > 0
        else "Clarify categories, then trim your largest non-essential bucket"
    )

    invest_bump = round(max(0.0, income * 0.05 - float(finances.get("surplus") or 0) * 0.1), 0)
    if invest_bump < 500 and income > 0:
        invest_bump = round(income * 0.03, 0)
    lt2 = sip_future_value(invest_bump, 0.10, min(25, years + 5))
    headline2 = (
        "Automate monthly long-term investments"
        if income > 0 and invest_bump > 0
        else "Plan to automate investing when cash flow steadies"
    )

    non_total = float(finances.get("expenses", {}).get("non_essential_total") or 0)
    multi_cut = round(non_total * 0.12, 0)
    lt3 = sip_future_value(multi_cut, 0.10, min(25, years + 5))
    headline3 = (
        "Reduce non-essential spending across categories"
        if non_total > 0
        else "Improve labels so multi-category trims can be targeted"
    )

    ra_best = int(retirement_projection.get("years_gained") or retirement_projection.get("years_gained_best") or 0)
    ra_mod = int(retirement_projection.get("years_gained_moderate") or 0)
    ra_cap = max(0, min(5, ra_best))
    ra_cap_soft = max(0, min(4, ra_mod or max(0, ra_cap - 1)))
    ra_invest = max(0, min(3, ra_cap + 1)) if income > 0 else 0

    inv_conf = "high" if income > 50000 else "medium"

    raw_actions: List[Dict[str, Any]] = [
        {
            "title": headline1,
            "description": _action_card_description(
                cut_disc,
                lt1,
                0,
                ra_cap_soft if ra_cap_soft else ra_cap,
                retirement_projection,
            )
            if top_amt > 0
            else (
                "Impact: clearer surplus once income, transfers, and EMIs are labeled.\n"
                "Outcome: a precise first cut you can size on the next sync."
            ),
            "monthly_impact": cut_disc,
            "long_term_gain": round(lt1, 0),
            "retirement_age_impact": ra_cap_soft if ra_cap_soft else ra_cap,
            "confidence": conf,
            "type": "reduce_top_discretionary",
        },
        {
            "title": headline2,
            "description": (
                _action_card_description(
                    invest_bump, lt2, 1, ra_invest, retirement_projection
                )
                if income > 0 and invest_bump > 0
                else (
                    "Impact: investing starts before lifestyle spend expands.\n"
                    "Outcome: steadier progress toward your corpus."
                )
            ),
            "monthly_impact": invest_bump,
            "long_term_gain": round(lt2, 0),
            "retirement_age_impact": ra_invest,
            "confidence": inv_conf,
            "type": "increase_investment_pct",
        },
        {
            "title": headline3,
            "description": (
                _action_card_description(multi_cut, lt3, 2, ra_cap, retirement_projection)
                if non_total > 0
                else (
                    "Impact: safer trims once food, shopping, and subscriptions are visible.\n"
                    "Outcome: higher investable surplus without lifestyle shock."
                )
            ),
            "monthly_impact": multi_cut,
            "long_term_gain": round(lt3, 0),
            "retirement_age_impact": ra_cap,
            "confidence": "medium",
            "type": "optimize_multi_category",
        },
    ]

    for a in raw_actions:
        lt = float(a.get("long_term_gain") or 0)
        mo = float(a.get("monthly_impact") or 0)
        clarity = _confidence_rank(str(a.get("confidence")))
        a["impact_score"] = round(lt + mo * 2.0 + clarity * 800.0, 1)

    raw_actions.sort(key=lambda x: (-float(x.get("impact_score", 0)), -_confidence_rank(str(x.get("confidence")))))

    for a in raw_actions:
        a.pop("impact_score", None)

    return {"actions": raw_actions}


def _hero_trend_takeaway(trends: Dict[str, Any]) -> str:
    sp = trends.get("spending") or "unknown"
    sav = trends.get("savings") or "unknown"
    inc = trends.get("income") or "unknown"
    if sp == "increasing":
        return (
            "Your spending has been increasing over recent months—pause fresh non-essential "
            "commitments until the pattern steadies."
        )
    if sav == "inconsistent":
        return (
            "Your end-of-month cushion has been uneven—set one automatic investment you can hit every month."
        )
    if inc == "volatile":
        return (
            "Your income has varied month to month—confirm inflows before you add new fixed costs."
        )
    if sp == "decreasing":
        return (
            "Your spending has been decreasing over recent months—invest the freed cash before "
            "it drifts back into old habits."
        )
    if sp == "unknown":
        return "Sync another month or two so we can describe your spending trend with confidence."
    if sav == "consistent" and sp == "stable":
        return "Cash flow looks steady—nudge your monthly investment up while conditions stay calm."
    return "Revisit categories quarterly so quiet leakage does not undo progress."


def build_summary(
    finances: Dict[str, Any],
    health: Dict[str, Any],
    trends: Dict[str, Any],
    action_plan: Dict[str, Any],
    retirement_projection: Dict[str, Any],
) -> Dict[str, Any]:
    actions = action_plan.get("actions") or []
    best_title = (
        actions[0]["title"]
        if actions
        else "Automate one recurring investment, then address the largest non-essential category"
    )
    surplus = float(finances.get("surplus") or 0)
    rate = float(finances.get("savings_rate") or 0)
    income = float(finances.get("avg_monthly_income") or 0)
    non_ess_tot = float(finances.get("expenses", {}).get("non_essential_total") or 0)
    expense_avg = float(finances.get("avg_monthly_expense") or 0)
    non_ess_cats = finances.get("expenses", {}).get("non_essential") or []

    h_reason = health.get("reason") or (
        "cash flow has room to direct a bit more toward long-term savings"
    )
    diagnosis = h_reason
    if trends.get("spending") == "increasing":
        diagnosis = (
            "Your spending has been increasing over recent months; "
            + (health.get("reason") or "that leaves less to invest each month.")
        )

    if non_ess_tot > 0 and income > 0 and non_ess_tot / income > 0.35:
        overview = (
            "You are current on essentials—cut non-essential spending now and you can materially "
            "improve long-term savings without touching rent or EMIs."
        )
    elif rate >= 22 and trends.get("spending") != "increasing":
        overview = (
            "You are on a stable path—keep automating investments so today’s surplus keeps compounding."
        )
    elif rate >= 12:
        overview = (
            "You run a workable margin most months—attack the largest non-essential bucket first "
            "to widen what you invest each month."
        )
    else:
        overview = (
            "Bills are absorbing most of income—prioritize non-essential cuts before chasing higher returns."
        )

    key_observation = ""
    if non_ess_tot > 0:
        key_observation = (
            "Non-essential categories remain the fastest place to free cash you can invest without "
            "risking missed fixed payments."
        )
    elif expense_avg > 0:
        key_observation = (
            "Tag more debits to specific categories so the next review can name the lowest-risk cuts."
        )
    else:
        key_observation = "Add more categorized months to reveal which spending blocks matter most."

    m0 = float(actions[0].get("monthly_impact") or 0) if actions else 0.0
    outcome_corpus = 0.0
    if len(actions) >= 2:
        outcome_corpus = float(actions[0].get("long_term_gain") or 0) + 0.35 * float(
            actions[1].get("long_term_gain") or 0
        )
    elif actions:
        outcome_corpus = float(actions[0].get("long_term_gain") or 0)

    long_term_impact = ""
    if outcome_corpus > 0:
        long_term_impact = (
            f"Invest freed cash on a steady schedule and you could build "
            f"{_format_inr_future_lump(outcome_corpus, with_investing_note=True)}."
        )
    else:
        long_term_impact = (
            "Even modest monthly amounts, invested steadily, can build meaningful savings over time."
        )

    recommended_actions: List[str] = []
    if non_ess_cats:
        human = _humanize_spend_label(str(non_ess_cats[0][0]))
        hl = human.lower()
        if "miscellaneous" in hl:
            recommended_actions.append(
                "Review uncategorized transactions and assign recurring merchants to clear categories."
            )
        elif "food" in hl or "drink" in hl:
            recommended_actions.append("Reduce frequent food delivery and set a simple weekly dining budget.")
        elif "subscription" in hl:
            recommended_actions.append("Audit subscriptions and cancel services you no longer use.")
        elif "shop" in hl or "retail" in hl:
            recommended_actions.append("Limit impulse and online shopping with a fixed monthly non-essential cap.")
        else:
            recommended_actions.append(
                f"Lower {human} by a small, sustainable percentage before touching fixed bills."
            )
    recommended_actions.append("Review repeating bank debits at least once a quarter.")
    recommended_actions.append(
        "Move a fixed amount to investments on the same day income arrives, before day-to-day spending."
    )
    if len(recommended_actions) < 4:
        recommended_actions.append(
            "Keep emergency balances in an account separate from everyday spending."
        )
    recommended_actions = recommended_actions[:5]

    prescription = "; ".join(recommended_actions[:3]) if recommended_actions else best_title
    impact_25 = sip_future_value(_round_monthly_display(m0), 0.10, 25)
    impact_str = (
        f"Long-term trajectory: {_format_inr_future_lump(impact_25, with_investing_note=True)}."
        if m0 > 0
        else "Long-term trajectory: small steady amounts still compound meaningfully."
    )

    future_self_line = long_term_impact if outcome_corpus > 0 else ""

    hero_key_takeaway = _hero_trend_takeaway(trends)
    hero_future_line = long_term_impact
    health_level = health.get("level") or "average"
    level_label = {
        "good": "Good",
        "average": "Average",
        "needs_attention": "Needs attention",
    }.get(health_level, "Average")

    return {
        "overview": overview,
        "key_observation": key_observation,
        "long_term_impact": long_term_impact,
        "recommended_actions": recommended_actions,
        "hero_overview": overview,
        "hero_key_takeaway": hero_key_takeaway,
        "hero_future_impact": hero_future_line,
        "financial_health_rating_label": level_label,
        "status": overview,
        "biggest_issue": key_observation,
        "best_action": best_title,
        "impact": impact_str,
        "optimized_retirement_years_gained": retirement_projection.get("years_gained", 0),
        "diagnosis": diagnosis,
        "prescription": prescription,
        "future_self_impact": future_self_line or long_term_impact,
    }


def build_data_quality_warnings(finances: Dict[str, Any]) -> Dict[str, Any]:
    """
    UI guardrails: flags incomplete income, heavy uncategorized spend, weak savings signal,
    and whether savings rate should be hidden (extremes distort the story).
    """
    empty: Dict[str, Any] = {"warnings": [], "display_savings_rate_pct": None}
    if not finances.get("has_data"):
        return empty

    warnings: List[Dict[str, str]] = []
    inc = float(finances.get("avg_monthly_income") or 0)
    exp = float(finances.get("avg_monthly_expense") or 0)
    if inc > 0 and (exp / inc) > 1.5:
        warnings.append(
            {
                "code": "income_incomplete",
                "text": "Your income data may be incomplete",
            }
        )

    ex = finances.get("expenses") or {}
    ess = float(ex.get("essential_total") or 0)
    non = float(ex.get("non_essential_total") or 0)
    uncat = float(ex.get("uncategorized") or 0)
    denom = ess + non
    if denom > 0 and (uncat / denom) > 0.30:
        warnings.append(
            {
                "code": "uncategorized_high",
                "text": "Large uncategorized spending may affect accuracy",
            }
        )

    sr = float(finances.get("savings_rate") or 0)
    if sr < 5.0:
        warnings.append(
            {
                "code": "savings_unreliable",
                "text": "Savings data may be unreliable",
            }
        )

    display_sr: Optional[float]
    if abs(sr) > 200:
        display_sr = None
    else:
        display_sr = round(sr, 1)

    return {"warnings": warnings, "display_savings_rate_pct": display_sr}


def get_retirement_analysis(user_id: int) -> Dict[str, Any]:
    """JSON for /api/retirement/analysis — safe when empty or missing goal."""
    goal = RetirementGoal.query.filter_by(user_id=user_id).first()
    plan = RetirementPlan.query.filter_by(user_id=user_id).first()

    current_age = goal.current_age if goal else 30
    retire_age = goal.retirement_age if goal else 60
    inflation = (goal.inflation_rate / 100.0) if goal and goal.inflation_rate else DEFAULT_INFLATION

    finances = analyze_user_finances(user_id)

    income_analysis = finances.get("income_analysis") if finances.get("has_data") else None
    account_analysis = finances.get("account_analysis") if finances.get("has_data") else None

    base: Dict[str, Any] = {
        "has_data": False,
        "finances": finances,
        "insights": [],
        "portfolios": {},
        "scenarios": [],
        "income_analysis": income_analysis,
        "account_analysis": account_analysis,
        "action_plan": {"actions": []},
        "retirement_projection": {},
        "trends": {},
        "financial_health": {},
        "summary": {},
        "current_age": current_age,
        "retirement_age": retire_age,
        "readiness_score": int(plan.readiness_score) if plan else None,
        "message": finances.get("message"),
        "confidence": None,
        "data_quality": build_data_quality_warnings(finances),
    }

    if not finances.get("has_data"):
        base["message"] = finances.get("message") or "Insufficient transaction history."
        return base

    trends = compute_trends(finances)
    insights = generate_behavioral_insights(finances, trends)
    insights = insights[:5]
    portfolios = simulate_indian_portfolios(finances, current_age, retire_age, inflation)
    financial_health = compute_financial_health(finances, trends)
    retirement_projection = compute_retirement_projection(finances, current_age, retire_age)
    action_plan = generate_action_plan(
        finances,
        portfolios,
        trends,
        current_age,
        retire_age,
        retirement_projection,
    )
    summary = build_summary(
        finances, financial_health, trends, action_plan, retirement_projection
    )

    scenarios_list: List[Dict[str, Any]] = []
    for key, pdata in portfolios.items():
        row = {"key": key, **pdata}
        scenarios_list.append(row)

    base.update(
        {
            "has_data": True,
            "insights": insights,
            "portfolios": portfolios,
            "scenarios": scenarios_list,
            "income_analysis": income_analysis,
            "account_analysis": account_analysis,
            "action_plan": action_plan,
            "retirement_projection": retirement_projection,
            "trends": trends,
            "financial_health": financial_health,
            "summary": summary,
            "message": None,
            "confidence": finances.get("confidence"),
        }
    )
    return base


# --- Goal-based planning (persisted RetirementPlan) ---


def calculate_retirement_plan(goal: RetirementGoal) -> Dict[str, Any]:
    years_to_retire = max(0, goal.retirement_age - goal.current_age)
    years_in_retirement = max(0, goal.life_expectancy - goal.retirement_age)

    monthly_return = goal.expected_return / 100.0 / 12.0
    months_to_retire = years_to_retire * 12
    months_in_retirement = years_in_retirement * 12

    real_monthly_income = float(goal.target_monthly_income) * (
        (1 + goal.inflation_rate / 100.0) ** years_to_retire
    )

    real_return = (
        (1 + goal.expected_return / 100.0) / (1 + goal.inflation_rate / 100.0)
    ) - 1.0
    real_monthly_return = real_return / 12.0

    if real_monthly_return > 0 and months_in_retirement > 0:
        target_corpus = real_monthly_income * (
            (1 - (1 + real_monthly_return) ** (-months_in_retirement)) / real_monthly_return
        )
    else:
        target_corpus = real_monthly_income * max(months_in_retirement, 0)

    current_contribution = float(goal.current_monthly_contribution or 0)
    if monthly_return > 0 and months_to_retire > 0:
        projected_corpus = current_contribution * (
            ((1 + monthly_return) ** months_to_retire - 1) / monthly_return
        ) * (1 + monthly_return)
    else:
        projected_corpus = current_contribution * max(months_to_retire, 0)

    if monthly_return > 0 and months_to_retire > 0:
        required_monthly = target_corpus * monthly_return / (
            ((1 + monthly_return) ** months_to_retire - 1) * (1 + monthly_return)
        )
    else:
        required_monthly = (
            target_corpus / max(months_to_retire, 1) if months_to_retire else 0.0
        )

    readiness_score = (
        min(100, int((projected_corpus / target_corpus) * 100)) if target_corpus > 0 else 0
    )

    return {
        "target_corpus": round(target_corpus, 2),
        "projected_corpus": round(projected_corpus, 2),
        "required_monthly_contribution": round(required_monthly, 2),
        "readiness_score": readiness_score,
        "years_to_retirement": years_to_retire,
        "inflation_adjusted_income": round(real_monthly_income, 2),
    }


def save_retirement_plan(user_id: int, goal: RetirementGoal, calc: Dict[str, Any]) -> RetirementPlan:
    plan = RetirementPlan.query.filter_by(user_id=user_id).first()
    if not plan:
        plan = RetirementPlan(user_id=user_id, goal_id=goal.id)
        db.session.add(plan)

    plan.goal_id = goal.id
    plan.target_corpus = calc["target_corpus"]
    plan.projected_corpus = calc["projected_corpus"]
    plan.required_monthly_contribution = calc["required_monthly_contribution"]
    plan.readiness_score = calc["readiness_score"]
    plan.years_to_retirement = calc["years_to_retirement"]
    plan.last_calculated = datetime.utcnow()

    db.session.commit()
    return plan


def check_and_award_milestones(user_id: int, goal: RetirementGoal, plan: RetirementPlan) -> List[Any]:
    return []


def simulate_scenario(
    goal: RetirementGoal, monthly_investment: float, retire_age: int
) -> Dict[str, Any]:
    original_contribution = goal.current_monthly_contribution
    original_retire_age = goal.retirement_age

    goal.current_monthly_contribution = monthly_investment
    goal.retirement_age = retire_age

    result = calculate_retirement_plan(goal)

    goal.current_monthly_contribution = original_contribution
    goal.retirement_age = original_retire_age

    return result


def get_spending_insights(user_id: int, goal: Optional[RetirementGoal]) -> List[Dict[str, Any]]:
    fin = analyze_user_finances(user_id)
    if not fin.get("has_data"):
        return [
            {
                "emoji": "📊",
                "text": "Link accounts and sync transactions to see personalized retirement guidance.",
            }
        ]
    tr = compute_trends(fin)
    rows = generate_behavioral_insights(fin, tr)
    return rows[:5]
