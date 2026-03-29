"""
Bank API Integration Module
Connects Flask app to FastAPI mock bank server
"""

import hashlib
import os
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv

load_dotenv()

API_BASE_URL = os.environ.get("API_BASE_URL", "http://127.0.0.1:8000")
REQUEST_TIMEOUT = 10

_FALLBACK_IDENTITY = "DEMOUSER1AA"


def linked_transaction_import_hash(
    linked_account_id: int,
    tx_date: datetime,
    amount: Decimal,
    description: str,
) -> str:
    """sha256(account_id + date + amount + description) per integration spec."""
    d = tx_date.strftime("%Y-%m-%d") if isinstance(tx_date, datetime) else str(tx_date)
    if not isinstance(amount, Decimal):
        amount = Decimal(str(amount))
    a = str(amount.quantize(Decimal("0.01")))
    desc = (description or "").strip()
    raw = f"{linked_account_id}{d}{a}{desc}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _safe_response_json(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def check_api_server() -> bool:
    try:
        response = requests.get(f"{API_BASE_URL}/", timeout=REQUEST_TIMEOUT)
        if response.status_code == 200:
            data = _safe_response_json(response)
            if isinstance(data, dict):
                print(f"API Server status: {data.get('status')}")
            return True
        return False
    except requests.exceptions.ConnectionError:
        print(f"Cannot connect to API server at {API_BASE_URL}")
        print("   Make sure the FastAPI server is running:")
        print("   python bank_server.py")
        return False
    except requests.exceptions.Timeout:
        print(f"API health check timed out ({REQUEST_TIMEOUT}s)")
        return False
    except Exception as e:
        print(f"API health check error: {e}")
        return False


def _unwrap_accounts_response(data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict):
        return {"status": "error", "message": "Invalid response from bank"}
    return data


def fetch_accounts_by_identity(identity_id: str) -> Dict[str, Any]:
    if not identity_id or not str(identity_id).strip():
        return {"status": "error", "message": "identity_id is required"}

    try:
        response = requests.get(
            f"{API_BASE_URL}/accounts",
            params={"identity_id": str(identity_id).strip()},
            timeout=REQUEST_TIMEOUT,
        )
        data = _safe_response_json(response)
        if data is None:
            return {"status": "error", "message": "Invalid response from bank"}

        data = _unwrap_accounts_response(data)

        if response.status_code == 400 and data.get("status") == "error":
            return data

        if response.status_code != 200:
            return {
                "status": "error",
                "message": data.get("message") or "Bank returned an error",
            }

        if data.get("status") != "ok":
            return {
                "status": "error",
                "message": data.get("message") or "Unexpected bank response",
            }

        if not isinstance(data.get("banks"), list):
            return {"status": "error", "message": "Invalid accounts payload from bank"}

        return data

    except requests.exceptions.ConnectionError:
        return {"status": "error", "message": "Bank unreachable"}
    except requests.exceptions.Timeout:
        return {"status": "error", "message": "Bank request timed out"}
    except requests.RequestException as e:
        print(f"fetch_accounts_by_identity: {e}")
        return {"status": "error", "message": "Bank request failed"}
    except Exception as e:
        print(f"fetch_accounts_by_identity: {e}")
        return {"status": "error", "message": "Could not load accounts from bank"}


def fetch_all_api_accounts() -> List[Dict]:
    data = fetch_accounts_by_identity(_FALLBACK_IDENTITY)
    if data.get("status") != "ok":
        return []
    out: List[Dict] = []
    for bank in data.get("banks") or []:
        bname = bank.get("name") or ""
        for acc in bank.get("accounts") or []:
            row = dict(acc)
            row["bank_name"] = row.get("bank_name") or bname
            out.append(row)
    return out


def fetch_account_details_result(
    api_account_id: int,
) -> Tuple[Optional[Dict], Optional[str]]:
    """
    Returns (payload, error_kind).
    error_kind is None on success, 'not_found' for HTTP 404, 'transport_error' for timeouts/network.
    """
    try:
        response = requests.get(
            f"{API_BASE_URL}/accounts/{api_account_id}",
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code == 404:
            return None, "not_found"
        if response.status_code != 200:
            return None, "transport_error"
        data = _safe_response_json(response)
        if isinstance(data, dict):
            return data, None
        return None, "transport_error"
    except requests.exceptions.Timeout:
        print("Account details: timed out")
        return None, "transport_error"
    except requests.exceptions.ConnectionError:
        print("Account details: unreachable")
        return None, "transport_error"
    except Exception as e:
        print(f"❌ Error fetching account details: {e}")
        return None, "transport_error"


def fetch_account_details(api_account_id: int) -> Optional[Dict]:
    data, _err = fetch_account_details_result(api_account_id)
    return data


def transaction_description(tx_data: Dict[str, Any]) -> str:
    d = (tx_data.get("description") or tx_data.get("merchant") or "").strip()
    return d or "Transaction"


def transaction_amount(tx_data: Dict[str, Any]) -> Decimal:
    raw = tx_data.get("amount")
    if raw is None:
        return Decimal("0")
    return Decimal(str(round(abs(float(raw)), 2)))


def fetch_transactions_for_account(linked_account) -> Tuple[List[Dict], Optional[str]]:
    """
    Returns (normalized_transactions, error_message).
    error_message is set on transport/HTTP/parse failures; empty list with no error means no rows.
    """
    from models import Transaction

    try:
        api_account_id = linked_account.api_account_id
        flask_account_id = linked_account.id

        latest_transaction = Transaction.query.filter_by(
            account_id=flask_account_id
        ).order_by(Transaction.date.desc()).first()

        params: Dict[str, str] = {}
        if latest_transaction:
            next_date = latest_transaction.date + timedelta(days=1)
            params["start_date"] = next_date.strftime("%Y-%m-%d")

        response = requests.get(
            f"{API_BASE_URL}/accounts/{api_account_id}/transactions",
            params=params,
            timeout=REQUEST_TIMEOUT,
        )

    except requests.exceptions.Timeout:
        return [], "Bank request timed out"
    except requests.exceptions.ConnectionError:
        return [], "Bank unreachable"
    except requests.RequestException as e:
        return [], str(e) or "Bank request failed"
    except Exception as e:
        print(f"Error fetching transactions: {e}")
        return [], "Could not load transactions"

    try:
        raw_list = response.json()
    except ValueError:
        return [], "Invalid response from bank"

    if response.status_code != 200:
        msg = "Bank returned an error"
        if isinstance(raw_list, dict):
            msg = raw_list.get("detail") or raw_list.get("message") or msg
        return [], str(msg)

    if not isinstance(raw_list, list):
        return [], "Invalid transaction list from bank"

    normalized: List[Dict] = []
    for tx in raw_list:
        if not isinstance(tx, dict):
            continue
        desc = transaction_description(tx)
        amt = float(transaction_amount(tx))
        row = dict(tx)
        row["description"] = desc
        row["merchant"] = row.get("merchant") or desc
        row["amount"] = amt
        normalized.append(row)

    return normalized, None


def initiate_connection(user):
    print("⚠️  Using legacy connection method - consider updating to FastAPI")
    return "/accounts"


def handle_api_callback(args, user):
    print("⚠️  Using legacy callback method - consider updating to FastAPI")
    return True


def generate_monthly_statement(user, year, month):
    print("⚠️  WARNING: Using deprecated mock data generation")
    return []


def test_api_connection():
    print("\n" + "=" * 60)
    print("Testing FastAPI Server Connection")
    print("=" * 60)

    if check_api_server():
        print("\n✅ API Server is online and reachable")
        data = fetch_accounts_by_identity(_FALLBACK_IDENTITY)
        if data.get("status") == "ok":
            n = sum(len(b.get("accounts") or []) for b in (data.get("banks") or []))
            print(f"\nDemo identity: {n} account(s) available")
        return True
    print("\nAPI Server connection failed")
    return False


if __name__ == "__main__":
    test_api_connection()
