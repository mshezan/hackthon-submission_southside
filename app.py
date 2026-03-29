import re
from typing import Optional

from flask import Flask, render_template, request, jsonify, redirect, url_for, flash, current_app
from flask_login import LoginManager, login_required, current_user
from sqlalchemy.exc import IntegrityError

from models import (
    db,
    User,
    BankAccount,
    LinkedAccount,
    Transaction,
    Category,
    RetirementGoal,
    RetirementPlan,
    RetirementMilestone,
)
from services import (
    categorize_transaction,
    initialize_categories,
    initialize_banks,
    get_or_create_bank_by_name,
    get_user_accounts,
    get_active_account,
    set_active_account,
    get_account_stats,
)
import bank_api
from config import Config
from auth import auth_bp
from datetime import datetime
from sqlalchemy import extract, func, or_, text
from dotenv import load_dotenv
from retirement_service import (
    get_retirement_analysis,
    calculate_retirement_plan,
    save_retirement_plan,
    check_and_award_milestones,
    simulate_scenario,
    get_spending_insights,
)
# Load environment variables
load_dotenv()

app = Flask(__name__)
app.config.from_object(Config)

db.init_app(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'auth.login'
login_manager.login_message = 'Please log in to access this page.'
login_manager.login_message_category = 'info'


def _debit_only_filter():
    """Match retirement analytics: outflows are debit or unset type (legacy rows)."""
    return or_(
        Transaction.transaction_type == "debit",
        Transaction.transaction_type.is_(None),
    )


@login_manager.user_loader
def load_user(user_id):
    try:
        return User.query.get(int(user_id))
    except Exception:
        return None


app.register_blueprint(auth_bp)


# ============================================================================
# ERROR HANDLERS
# ============================================================================

@app.errorhandler(404)
def not_found_error(error):
    if current_user.is_authenticated:
        return render_template('404.html', 
                             page_name='error',
                             user_accounts=get_user_accounts(current_user),
                             linked_accounts=get_linked_accounts(current_user)), 404
    return redirect(url_for('auth.login'))


@app.errorhandler(500)
def internal_error(error):
    db.session.rollback()
    if current_user.is_authenticated:
        flash('An internal error occurred. Please try again.', 'error')
        return redirect(url_for('dashboard'))
    return jsonify({'status': 'error', 'message': 'Internal server error'}), 500


def _ensure_sqlite_user_columns():
    """ALTER TABLE for SQLite DBs created before identity_id / full_name existed."""
    if db.engine.dialect.name != "sqlite":
        return
    with db.engine.begin() as conn:
        rows = conn.execute(text("PRAGMA table_info(users)")).fetchall()
        col_names = {r[1] for r in rows}
        if "identity_id" not in col_names:
            conn.execute(text("ALTER TABLE users ADD COLUMN identity_id VARCHAR(64)"))
        if "full_name" not in col_names:
            conn.execute(text("ALTER TABLE users ADD COLUMN full_name VARCHAR(255)"))
    with db.engine.begin() as conn:
        conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_users_identity_id ON users (identity_id)")
        )


def _ensure_sqlite_linked_account_columns():
    """ALTER TABLE for SQLite DBs created before bank_id / masked_account_number."""
    if db.engine.dialect.name != "sqlite":
        return
    with db.engine.begin() as conn:
        rows = conn.execute(text("PRAGMA table_info(linked_accounts)")).fetchall()
        if not rows:
            return
        col_names = {r[1] for r in rows}
        if "bank_id" not in col_names:
            conn.execute(text("ALTER TABLE linked_accounts ADD COLUMN bank_id INTEGER"))
        if "masked_account_number" not in col_names:
            conn.execute(
                text(
                    "ALTER TABLE linked_accounts ADD COLUMN masked_account_number VARCHAR(32)"
                )
            )
        if "api_link_valid" not in col_names:
            conn.execute(
                text("ALTER TABLE linked_accounts ADD COLUMN api_link_valid INTEGER DEFAULT 1")
            )


def _ensure_sqlite_transaction_columns():
    """ALTER TABLE for SQLite DBs missing linked-import / API columns on transactions."""
    if db.engine.dialect.name != "sqlite":
        return
    with db.engine.begin() as conn:
        rows = conn.execute(text("PRAGMA table_info(transactions)")).fetchall()
        if not rows:
            return
        col_names = {r[1] for r in rows}
        if "mode" not in col_names:
            conn.execute(text("ALTER TABLE transactions ADD COLUMN mode VARCHAR(50)"))
        if "transaction_type" not in col_names:
            conn.execute(
                text("ALTER TABLE transactions ADD COLUMN transaction_type VARCHAR(50)")
            )
        if "narration" not in col_names:
            conn.execute(text("ALTER TABLE transactions ADD COLUMN narration VARCHAR(500)"))
        if "transaction_hash" not in col_names:
            conn.execute(text("ALTER TABLE transactions ADD COLUMN transaction_hash VARCHAR(64)"))
    with db.engine.begin() as conn:
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_transactions_transaction_hash "
                "ON transactions (transaction_hash)"
            )
        )


def normalize_identity_id(raw: str):
    """PAN-like: 5–32 alphanumeric, uppercase (aligned with mock bank)."""
    if not raw:
        return None
    s = str(raw).strip().upper()
    if len(s) < 5 or len(s) > 32 or not re.match(r"^[A-Z0-9]+$", s):
        return None
    return s


def enforce_identity_match(user: User, requested_norm: Optional[str]) -> tuple[bool, Optional[str]]:
    """
    One user = one identity_id. Reject only when the client sends a non-empty identity
    that differs from the one stored on the profile (409 scenarios).
    """
    if requested_norm and user.identity_id and requested_norm != user.identity_id:
        return (
            False,
            'This profile is already linked to a different identity. Sign in with the correct identity or clear linked data in development.',
        )
    return True, None


def resolve_identity_norm(user: User, identity_raw: str) -> Optional[str]:
    """Prefer explicit form/query value; otherwise use the identity already stored on the user."""
    if identity_raw and str(identity_raw).strip():
        return normalize_identity_id(str(identity_raw).strip())
    return normalize_identity_id((user.identity_id or '').strip()) if user.identity_id else None


def _catalog_api_account_ids(bank_payload: dict) -> set:
    """Flatten API account ids from a successful fetch_accounts_by_identity response."""
    ids = set()
    for bank in bank_payload.get("banks") or []:
        for acc in bank.get("accounts") or []:
            if isinstance(acc, dict) and acc.get("id") is not None:
                try:
                    ids.add(int(acc["id"]))
                except (TypeError, ValueError):
                    continue
    return ids


def _repair_identity_consistency():
    """
    One-time alignment after startup: holder name + linked account display names match bank for identity.
    """
    try:
        users = User.query.filter(User.identity_id.isnot(None)).all()
        for u in users:
            norm = normalize_identity_id((u.identity_id or "").strip())
            if not norm:
                continue
            data = bank_api.fetch_accounts_by_identity(norm)
            if data.get("status") != "ok":
                continue
            holder = (data.get("holder_name") or "").strip()
            if holder:
                u.full_name = holder
            allowed = _catalog_api_account_ids(data)
            for la in list(u.linked_accounts):
                if la.api_account_id not in allowed:
                    la.api_link_valid = False
                    continue
                la.api_link_valid = True
                if holder and (la.api_account_name or "").strip() != holder:
                    la.api_account_name = holder
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        app.logger.warning("identity repair skipped: %s", e)


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def get_linked_accounts(user):
    """Get all linked accounts for a user, ordered by creation date"""
    return LinkedAccount.query.filter_by(user_id=user.id).order_by(LinkedAccount.creation_date.asc()).all()


def _build_account_cards_data(user_accounts, linked_accounts):
    """
    Build a list of account dicts for the balance-card switcher, and determine
    which account is the 'primary' one (salary > highest credit activity).

    Returns (cards, primary_key) where:
      cards      = list of dicts with keys id, name, masked, balance, label
      primary_key = the 'id' value of the chosen primary card (or 'all')
    """
    cards = []

    for la in linked_accounts:
        actype = (la.api_account_type or '').lower()
        label = la.api_account_type or 'Account'
        bank_name = la.bank.name if la.bank else ''
        display_name = bank_name + (' ' + label.title() if label else '') if bank_name else (la.account_nickname or label)
        masked = la.masked_account_number or ''
        # Format masked: show last 4 digits only if we have them
        if masked and len(masked) >= 4:
            masked_display = '**** **** **** ' + masked[-4:]
        elif masked:
            masked_display = '**** ' + masked
        else:
            masked_display = ''
        balance = float(la.api_balance) if la.api_balance is not None else 0.0
        cards.append({
            'id': f'linked_{la.id}',
            'name': display_name.strip(),
            'nickname': la.account_nickname or display_name.strip(),
            'masked': masked_display,
            'balance': balance,
            'label': label.title() if label else 'Account',
            'actype': actype,
            'tx_count': len(la.transactions),
        })

    for ba in user_accounts:
        label = ba.account_type or 'Account'
        masked_display = ''
        balance = float(ba.balance) if ba.balance is not None else 0.0
        cards.append({
            'id': str(ba.id),
            'name': ba.account_name or 'Account',
            'nickname': ba.account_name or 'Account',
            'masked': masked_display,
            'balance': balance,
            'label': label.title(),
            'actype': label.lower(),
            'tx_count': len(ba.transactions),
        })

    if not cards:
        return [], 'all'

    # Determine primary: prefer salary/current account, else highest tx_count
    salary_keywords = ('salary', 'current', 'savings')
    primary_card = None
    for kw in salary_keywords:
        for c in cards:
            if kw in c['actype'] or kw in c['name'].lower() or kw in c['label'].lower():
                primary_card = c
                break
        if primary_card:
            break

    if primary_card is None:
        # Fall back to highest transaction count
        primary_card = max(cards, key=lambda c: c['tx_count'])

    return cards, primary_card['id']


RECONNECT_LINK_MSG = "This account needs to be reconnected."


def _refresh_linked_accounts_state(user: User) -> None:
    """Reconcile api_link_valid, balances, and holder name from bank; does not commit."""
    norm = normalize_identity_id((user.identity_id or "").strip())
    allowed: Optional[set] = None
    catalog_ok = False
    holder = ""
    if norm:
        data = bank_api.fetch_accounts_by_identity(norm)
        if data.get("status") == "ok":
            allowed = _catalog_api_account_ids(data)
            catalog_ok = True
            holder = (data.get("holder_name") or "").strip()
            if holder:
                user.full_name = holder

    for la in get_linked_accounts(user):
        if catalog_ok and allowed is not None and la.api_account_id not in allowed:
            la.api_link_valid = False
            if holder:
                la.api_account_name = holder
            continue
        det, err = bank_api.fetch_account_details_result(la.api_account_id)
        if err == "not_found":
            la.api_link_valid = False
        elif err == "transport_error":
            pass
        elif det is not None:
            la.api_link_valid = True
        else:
            la.api_link_valid = False
        if det:
            la.api_balance = det.get("balance")
        if holder:
            la.api_account_name = holder


def _apply_bank_holder_name_to_linked(user: User) -> None:
    """Set user.full_name and every linked account api_account_name from bank catalog."""
    norm = normalize_identity_id((user.identity_id or "").strip())
    if not norm:
        return
    data = bank_api.fetch_accounts_by_identity(norm)
    if data.get("status") != "ok":
        return
    holder = (data.get("holder_name") or "").strip()
    if not holder:
        return
    user.full_name = holder
    for la in get_linked_accounts(user):
        la.api_account_name = holder


def _sync_linked_account_transactions(user, linked_account: LinkedAccount) -> dict:
    """
    Pull from bank API and insert new transactions. Does not commit.
    Returns dict with keys: added, skipped_duplicates, skipped_invalid, fetched_count, error (str or None).
    """
    out = {
        "added": 0,
        "skipped_duplicates": 0,
        "skipped_invalid": 0,
        "fetched_count": 0,
        "error": None,
    }

    if linked_account.api_link_valid is False:
        out["error"] = RECONNECT_LINK_MSG
        current_app.logger.warning(
            "sync: skipped invalid link linked_account_id=%s",
            linked_account.id,
        )
        return out

    api_transactions, fetch_err = bank_api.fetch_transactions_for_account(linked_account)
    if fetch_err:
        out["error"] = str(fetch_err)
        fe = str(fetch_err).lower()
        if "404" in fe or "not found" in fe:
            linked_account.api_link_valid = False
        current_app.logger.warning(
            "sync: fetch failed linked_account_id=%s error=%s",
            linked_account.id,
            fetch_err,
        )
        return out

    if not isinstance(api_transactions, list):
        out["error"] = "Invalid transaction response"
        current_app.logger.warning(
            "sync: invalid payload type linked_account_id=%s type=%s",
            linked_account.id,
            type(api_transactions).__name__,
        )
        return out

    out["fetched_count"] = len(api_transactions)
    log = current_app.logger
    log.info(
        "sync: linked_account_id=%s fetched_transactions=%s",
        linked_account.id,
        out["fetched_count"],
    )

    for tx_data in api_transactions:
        if not isinstance(tx_data, dict):
            out["skipped_invalid"] += 1
            log.warning("sync: skipped non-dict transaction row for linked_account_id=%s", linked_account.id)
            continue
        try:
            tx_date = datetime.strptime(tx_data["date"], "%Y-%m-%d")
        except (KeyError, ValueError, TypeError) as e:
            out["skipped_invalid"] += 1
            log.warning("sync: skipped malformed date for linked_account_id=%s: %s", linked_account.id, e)
            continue

        desc = bank_api.transaction_description(tx_data)
        amt = bank_api.transaction_amount(tx_data)
        if amt <= 0:
            out["skipped_invalid"] += 1
            log.warning(
                "sync: skipped non-positive amount for linked_account_id=%s amount=%s",
                linked_account.id,
                amt,
            )
            continue

        tx_hash = bank_api.linked_transaction_import_hash(linked_account.id, tx_date, amt, desc)

        try:
            with db.session.begin_nested():
                new_transaction = Transaction(
                    user_id=user.id,
                    account_id=linked_account.id,
                    date=tx_date,
                    description=desc,
                    amount=amt,
                    mode=tx_data.get("mode"),
                    transaction_type=tx_data.get("type", "debit"),
                    narration=tx_data.get("narration"),
                    transaction_hash=tx_hash,
                )
                db.session.add(new_transaction)
                db.session.flush()
                categorize_transaction(new_transaction)
            out["added"] += 1
        except IntegrityError:
            out["skipped_duplicates"] += 1
            continue
        except Exception as e:
            out["skipped_invalid"] += 1
            log.warning(
                "sync: failed to persist transaction linked_account_id=%s: %s",
                linked_account.id,
                e,
                exc_info=True,
            )
            continue

    if out["added"] > 0:
        linked_account.last_synced = datetime.utcnow()

    det, err = bank_api.fetch_account_details_result(linked_account.api_account_id)
    if det:
        linked_account.api_balance = det.get("balance")
    if err == "not_found":
        linked_account.api_link_valid = False

    log.info(
        "auto-sync: linked_account_id=%s fetched_transactions=%s inserted_transactions=%s",
        linked_account.id,
        out["fetched_count"],
        out["added"],
    )
    log.info(
        "sync: linked_account_id=%s inserted=%s skipped_duplicates=%s skipped_invalid=%s",
        linked_account.id,
        out["added"],
        out["skipped_duplicates"],
        out["skipped_invalid"],
    )
    return out


def _filter_unlinked_banks(bank_payload: dict, user: User):
    """From a successful bank API payload, keep only accounts this user has not linked."""
    linked_ids = {a.api_account_id for a in get_linked_accounts(user)}
    banks_out = []
    for bank in bank_payload.get("banks") or []:
        bname = bank.get("name") or ""
        avail = []
        for acc in bank.get("accounts") or []:
            if not isinstance(acc, dict):
                continue
            aid = acc.get("id")
            if aid is None or aid in linked_ids:
                continue
            row = dict(acc)
            row["bank_name"] = row.get("bank_name") or bname
            avail.append(row)
        if avail:
            banks_out.append({"name": bname, "accounts": avail})
    return banks_out, linked_ids


def parse_account_param(account_param):
    """
    Parse account parameter and return (account_type, account_id)
    Returns: ('all', None) | ('legacy', id) | ('linked', id)
    """
    if account_param == 'all':
        return 'all', None
    
    if str(account_param).startswith('linked_'):
        try:
            linked_id = int(account_param.replace('linked_', ''))
            return 'linked', linked_id
        except:
            return 'all', None
    
    try:
        legacy_id = int(account_param)
        return 'legacy', legacy_id
    except:
        return 'all', None


def get_selected_account_and_month(user_id):
    """Get selected account and month from query parameters"""
    account_param = request.args.get('account', 'all')
    selected_month = request.args.get('month')
    
    user_accounts = get_user_accounts(current_user)
    linked_accounts = get_linked_accounts(current_user)
    
    if not user_accounts and not linked_accounts:
        return None, 'none', None, []
    
    account_type, account_id = parse_account_param(account_param)
    
    if not selected_month:
        selected_month = datetime.now().strftime('%Y-%m')
    
    try:
        year, month = map(int, selected_month.split('-'))
    except:
        year = datetime.now().year
        month = datetime.now().month
        selected_month = f"{year:04d}-{month:02d}"
    
    # Get all months with transactions
    if account_type == 'all':
        all_months_query = db.session.query(
            func.strftime('%Y-%m', Transaction.date).label('month')
        ).filter(
            Transaction.user_id == current_user.id
        ).distinct().order_by(
            func.strftime('%Y-%m', Transaction.date).desc()
        ).all()
    elif account_type == 'legacy':
        all_months_query = db.session.query(
            func.strftime('%Y-%m', Transaction.date).label('month')
        ).filter(
            Transaction.bank_account_id == account_id
        ).distinct().order_by(
            func.strftime('%Y-%m', Transaction.date).desc()
        ).all()
    else:  # linked
        all_months_query = db.session.query(
            func.strftime('%Y-%m', Transaction.date).label('month')
        ).filter(
            Transaction.account_id == account_id
        ).distinct().order_by(
            func.strftime('%Y-%m', Transaction.date).desc()
        ).all()
    
    all_months = [m[0] for m in all_months_query if m[0]]
    
    if not all_months:
        all_months = [selected_month]
    elif selected_month not in all_months:
        all_months.insert(0, selected_month)
        all_months.sort(reverse=True)
    
    return account_id, account_type, selected_month, all_months


# ============================================================================
# PAGE ROUTES
# ============================================================================

@app.route('/')
@login_required
def dashboard():
    """Dashboard with multi-account support"""
    try:
        user_accounts = get_user_accounts(current_user)
        linked_accounts = get_linked_accounts(current_user)
        account_cards, primary_account_key = _build_account_cards_data(user_accounts, linked_accounts)
        
        if not user_accounts and not linked_accounts:
            return render_template('dashboard.html',
                                 page_name='dashboard',
                                 user_accounts=[],
                                 linked_accounts=[],
                                 account_cards=[],
                                 primary_account_key='all',
                                 total_balance=0,
                                 selected_account_id='all',
                                 selected_month=datetime.now().strftime('%Y-%m'),
                                 all_months=[datetime.now().strftime('%Y-%m')],
                                 total_spending=0,
                                 total_income=0,
                                 avg_transaction=0,
                                 transaction_count=0,
                                 top_category='N/A',
                                 account_type='none',
                                 bank_linked=False)
        
        account_id, account_type, selected_month, all_months = get_selected_account_and_month(current_user.id)
        
        try:
            year, month = map(int, selected_month.split('-'))
        except:
            year = datetime.now().year
            month = datetime.now().month
        
        # Calculate stats based on account type
        base_filter = []
        if account_type == 'all':
            base_filter = [Transaction.user_id == current_user.id]
        elif account_type == 'legacy':
            base_filter = [Transaction.bank_account_id == account_id]
        else:  # linked
            base_filter = [Transaction.account_id == account_id]

        total_spending = db.session.query(
            func.sum(Transaction.amount)
        ).filter(
            *base_filter,
            extract('year', Transaction.date) == year,
            extract('month', Transaction.date) == month,
            _debit_only_filter(),
        ).scalar() or 0
        
        total_income = db.session.query(
            func.sum(Transaction.amount)
        ).filter(
            *base_filter,
            extract('year', Transaction.date) == year,
            extract('month', Transaction.date) == month,
            Transaction.transaction_type == 'credit',
        ).scalar() or 0

        transaction_count = Transaction.query.filter(
            *base_filter,
            extract('year', Transaction.date) == year,
            extract('month', Transaction.date) == month
        ).count()

        top_category = db.session.query(
            Category.name,
            func.sum(Transaction.amount).label('total')
        ).join(Transaction).filter(
            *base_filter,
            extract('year', Transaction.date) == year,
            extract('month', Transaction.date) == month,
            _debit_only_filter(),
        ).group_by(Category.name).order_by(func.sum(Transaction.amount).desc()).first()
        
        recent_transactions = Transaction.query.filter(
            *base_filter
        ).order_by(Transaction.date.desc()).limit(5).all()

        top_category_name = top_category[0] if top_category else 'N/A'
        avg_transaction = total_spending / transaction_count if transaction_count > 0 else 0
        
        # Format selected_account_id for template
        if account_type == 'linked':
            display_account_id = f'linked_{account_id}'
        elif account_type == 'legacy':
            display_account_id = account_id
        else:
            display_account_id = 'all'
        
        total_balance = sum(c['balance'] for c in account_cards)

        return render_template('dashboard.html',
                             page_name='dashboard',
                             user_accounts=user_accounts,
                             linked_accounts=linked_accounts,
                             account_cards=account_cards,
                             primary_account_key=primary_account_key,
                             total_balance=total_balance,
                             selected_account_id=display_account_id,
                             selected_month=selected_month,
                             all_months=all_months,
                             total_spending=total_spending,
                             total_income=total_income,
                             transaction_count=transaction_count,
                             avg_transaction=avg_transaction,
                             recent_transactions=recent_transactions,
                             top_category=top_category_name,
                             account_type=account_type,
                             bank_linked=True)
    
    except Exception as e:
        print(f"Dashboard error: {e}")
        import traceback
        traceback.print_exc()
        flash('Error loading dashboard.', 'error')
        return render_template('dashboard.html',
                             page_name='dashboard',
                             user_accounts=[],
                             linked_accounts=[],
                             account_cards=[],
                             primary_account_key='all',
                             total_balance=0,
                             selected_account_id='all',
                             selected_month=datetime.now().strftime('%Y-%m'),
                             all_months=[datetime.now().strftime('%Y-%m')],
                             total_spending=0,
                             total_income=0,
                             avg_transaction=0,
                             transaction_count=0,
                             top_category='N/A',
                             account_type='none',
                             bank_linked=False)


@app.route('/transactions')
@login_required
def transactions():
    """Transactions page with multi-account support"""
    try:
        user_accounts = get_user_accounts(current_user)
        linked_accounts = get_linked_accounts(current_user)
        
        if not user_accounts and not linked_accounts:
            return render_template('transactions.html',
                                 page_name='transactions',
                                 transactions=[],
                                 categories=Category.query.all(),
                                 user_accounts=[],
                                 linked_accounts=[],
                                 selected_account_id='all',
                                 all_months=[datetime.now().strftime('%Y-%m')],
                                 selected_month=datetime.now().strftime('%Y-%m'))
        
        account_id, account_type, selected_month, all_months = get_selected_account_and_month(current_user.id)
        
        try:
            year, month = map(int, selected_month.split('-'))
        except:
            year = datetime.now().year
            month = datetime.now().month
        
        # Get transactions based on account type
        if account_type == 'all':
            transactions_list = Transaction.query.filter(
                Transaction.user_id == current_user.id,
                extract('year', Transaction.date) == year,
                extract('month', Transaction.date) == month
            ).order_by(Transaction.date.desc()).all()
        elif account_type == 'legacy':
            transactions_list = Transaction.query.filter(
                Transaction.bank_account_id == account_id,
                extract('year', Transaction.date) == year,
                extract('month', Transaction.date) == month
            ).order_by(Transaction.date.desc()).all()
        else:  # linked
            transactions_list = Transaction.query.filter(
                Transaction.account_id == account_id,
                extract('year', Transaction.date) == year,
                extract('month', Transaction.date) == month
            ).order_by(Transaction.date.desc()).all()
        
        categories = Category.query.order_by(Category.name).all()
        
        # Format selected_account_id for template
        if account_type == 'linked':
            display_account_id = f'linked_{account_id}'
        elif account_type == 'legacy':
            display_account_id = account_id
        else:
            display_account_id = 'all'
        
        return render_template('transactions.html',
                             page_name='transactions',
                             transactions=transactions_list,
                             categories=categories,
                             user_accounts=user_accounts,
                             linked_accounts=linked_accounts,
                             selected_account_id=display_account_id,
                             all_months=all_months,
                             selected_month=selected_month)
    
    except Exception as e:
        print(f"Transactions error: {e}")
        import traceback
        traceback.print_exc()
        flash('Error loading transactions.', 'error')
        return render_template('transactions.html',
                             page_name='transactions',
                             transactions=[],
                             categories=Category.query.all(),
                             user_accounts=get_user_accounts(current_user),
                             linked_accounts=get_linked_accounts(current_user),
                             selected_account_id='all',
                             all_months=[datetime.now().strftime('%Y-%m')],
                             selected_month=datetime.now().strftime('%Y-%m'))


@app.route('/accounts')
@login_required
def accounts():
    """Enhanced accounts page with FastAPI integration"""
    try:
        try:
            _refresh_linked_accounts_state(current_user)
            db.session.commit()
        except Exception as refresh_err:
            db.session.rollback()
            current_app.logger.warning("accounts linked refresh: %s", refresh_err)

        linked_accounts = get_linked_accounts(current_user)
        legacy_accounts = get_user_accounts(current_user)

        legacy_accounts_data = []
        for account in legacy_accounts:
            stats = get_account_stats(account)
            account_info = account.to_dict()
            account_info.update(stats)
            legacy_accounts_data.append(account_info)

        linked_accounts_data = []
        for account in linked_accounts:
            account_info = account.to_dict()
            linked_accounts_data.append(account_info)

        return render_template('accounts.html',
                             page_name='accounts',
                             accounts=legacy_accounts_data,
                             linked_accounts=linked_accounts_data,
                             user_accounts=legacy_accounts)

    except Exception as e:
        print(f"Accounts error: {e}")
        import traceback
        traceback.print_exc()
        flash('Error loading accounts.', 'error')
        return render_template('accounts.html',
                             page_name='accounts',
                             accounts=[],
                             linked_accounts=[],
                             user_accounts=[])


@app.route('/api/bank/discover', methods=['GET'])
@login_required
def bank_discover():
    """
    Identity-based discovery: saves identity on the user and returns banks with linkable accounts.
    """
    try:
        identity_raw = request.args.get('identity_id', '').strip()
        norm = normalize_identity_id(identity_raw)
        if not norm:
            return jsonify({'status': 'error', 'message': 'Invalid identity format'}), 400

        ok_id, id_msg = enforce_identity_match(current_user, norm)
        if not ok_id:
            return jsonify({'status': 'error', 'message': id_msg}), 409

        data = bank_api.fetch_accounts_by_identity(norm)
        if data.get('status') != 'ok':
            return jsonify({
                'status': 'error',
                'message': data.get('message', 'Bank unreachable'),
            }), 503

        current_user.identity_id = norm
        if data.get('holder_name'):
            current_user.full_name = data.get('holder_name')
        db.session.commit()

        banks_out, linked_ids = _filter_unlinked_banks(data, current_user)
        payload = {
            'status': 'success',
            'message': 'Accounts loaded',
            'holder_name': data.get('holder_name'),
            'identity_id': norm,
            'banks': banks_out,
        }
        if not banks_out:
            payload['scenario'] = 'all_linked' if linked_ids else 'empty_source'
        return jsonify(payload)
    except Exception as e:
        db.session.rollback()
        print(f"discover error: {e}")
        return jsonify({
            'status': 'error',
            'message': 'We could not load your accounts. Please try again.',
        }), 500


@app.route('/api/bank/available-for-link')
@login_required
def bank_available_for_link():
    """
    Backward-compatible shim: same as discover when identity_id query or saved user identity is present.
    """
    query_raw = request.args.get('identity_id', '').strip()
    if query_raw:
        norm = normalize_identity_id(query_raw)
        if not norm:
            return jsonify({'status': 'error', 'message': 'Invalid identity format'}), 400
        ok_id, id_msg = enforce_identity_match(current_user, norm)
        if not ok_id:
            return jsonify({'status': 'error', 'message': id_msg}), 409
    else:
        norm = normalize_identity_id((current_user.identity_id or '').strip())
        if not norm:
            return jsonify({
                'status': 'error',
                'message': 'identity_id is required. Enter your PAN or ID and fetch accounts first.',
            }), 400

    data = bank_api.fetch_accounts_by_identity(norm)
    if data.get('status') != 'ok':
        return jsonify({
            'status': 'error',
            'message': data.get('message', 'Bank unreachable'),
        }), 503
    banks_out, linked_ids = _filter_unlinked_banks(data, current_user)
    payload = {
        'status': 'success',
        'message': 'Accounts loaded',
        'holder_name': data.get('holder_name'),
        'banks': banks_out,
    }
    if not banks_out:
        payload['scenario'] = 'all_linked' if linked_ids else 'empty_source'
    return jsonify(payload)


# ============================================================================
# API ROUTES - LINKED ACCOUNT MANAGEMENT (FastAPI Integration)
# ============================================================================

@app.route('/api/bank/connect', methods=['POST'])
@login_required
def bank_connect_new():
    """Link a new account from mock bank (identity + bank + API account id)."""
    try:
        api_account_id = request.form.get('api_account_id')
        account_nickname = request.form.get('account_nickname', '').strip()
        identity_raw = request.form.get('identity_id', '').strip()
        bank_name = request.form.get('bank_name', '').strip()
        masked = request.form.get('masked_account_number', '').strip()

        if not api_account_id or not account_nickname:
            flash('Please choose an account and enter a name.', 'error')
            return redirect(url_for('accounts'))

        try:
            api_account_id = int(api_account_id)
        except ValueError:
            flash('That account could not be used. Please pick another one.', 'error')
            return redirect(url_for('accounts'))

        norm = resolve_identity_norm(current_user, identity_raw)
        if not norm:
            flash('Identity is required. Fetch accounts with your PAN or ID first.', 'error')
            return redirect(url_for('accounts'))

        ok_id, id_msg = enforce_identity_match(current_user, norm)
        if not ok_id:
            flash(id_msg, 'error')
            return redirect(url_for('accounts'))

        catalog = bank_api.fetch_accounts_by_identity(norm)
        if catalog.get("status") != "ok":
            flash(catalog.get("message", "Could not verify identity with the bank."), 'error')
            return redirect(url_for('accounts'))

        allowed_ids = _catalog_api_account_ids(catalog)
        if api_account_id not in allowed_ids:
            flash('That account does not belong to this identity. Fetch accounts again and pick a listed account.', 'error')
            return redirect(url_for('accounts'))

        holder_name = (catalog.get("holder_name") or "").strip()
        current_user.identity_id = norm
        if holder_name:
            current_user.full_name = holder_name

        existing = LinkedAccount.query.filter_by(
            user_id=current_user.id,
            api_account_id=api_account_id,
        ).first()
        if existing:
            flash(f'Account is already linked as "{existing.account_nickname}".', 'warning')
            return redirect(url_for('accounts'))

        account_details = bank_api.fetch_account_details(api_account_id)
        if not account_details:
            flash('We could not verify that account. Please try again.', 'error')
            return redirect(url_for('accounts'))

        if not bank_name:
            bank_name = (account_details.get('bank_name') or '').strip()

        bank_row = get_or_create_bank_by_name(bank_name) if bank_name else None
        if not masked and account_details.get('account_number_masked'):
            masked = str(account_details.get('account_number_masked'))

        display_holder = holder_name or (account_details.get('name') or "").strip()
        new_account = LinkedAccount(
            user_id=current_user.id,
            api_account_id=api_account_id,
            account_nickname=account_nickname,
            api_account_name=display_holder or account_details.get('name'),
            api_account_type=account_details.get('type'),
            api_balance=account_details.get('balance'),
            bank_id=bank_row.id if bank_row else None,
            masked_account_number=masked or None,
            consent_status='active',
            is_active=True,
            api_link_valid=True,
            creation_date=datetime.utcnow(),
        )

        db.session.add(new_account)
        try:
            db.session.flush()
        except IntegrityError:
            db.session.rollback()
            flash('This bank account is already linked.', 'warning')
            return redirect(url_for('accounts'))

        sync_result = _sync_linked_account_transactions(current_user, new_account)
        _apply_bank_holder_name_to_linked(current_user)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash('This bank account is already linked.', 'warning')
            return redirect(url_for('accounts'))
        except Exception as sync_commit_err:
            db.session.rollback()
            print(f"Error committing after link/sync: {sync_commit_err}")
            import traceback
            traceback.print_exc()
            flash('Account was linked but saving failed. Please try Sync now.', 'error')
            return redirect(url_for('accounts'))

        flash('Account connected successfully', 'success')
        if sync_result.get('error'):
            flash(
                'Transactions could not be loaded yet: '
                + str(sync_result['error'])
                + '. Use Sync now to retry.',
                'warning',
            )
        return redirect(url_for('accounts'))

    except Exception as e:
        db.session.rollback()
        print(f"Error linking account: {e}")
        import traceback
        traceback.print_exc()
        flash('We could not connect that account. Please try again.', 'error')
        return redirect(url_for('accounts'))


def _perform_linked_accounts_sync():
    """Shared JSON handler for sync (single account or all). Returns Flask (response, status_code)."""
    data = request.get_json(silent=True) or {}
    sync_all = bool(data.get('sync_all'))
    account_id = data.get('account_id')

    if sync_all:
        targets = LinkedAccount.query.filter_by(user_id=current_user.id).order_by(
            LinkedAccount.id.asc()
        ).all()
    else:
        if account_id is None:
            return jsonify({
                'status': 'error',
                'message': 'Provide account_id or set sync_all to true',
            }), 400
        try:
            aid = int(account_id)
        except (TypeError, ValueError):
            return jsonify({'status': 'error', 'message': 'Invalid account_id'}), 400
        la = LinkedAccount.query.get(aid)
        if not la or la.user_id != current_user.id:
            return jsonify({'status': 'error', 'message': 'Account not found or unauthorized'}), 403
        targets = [la]

    if not targets:
        return jsonify({
            'status': 'success',
            'sync_result': 'full_success',
            'new_transactions': 0,
            'skipped_duplicates': 0,
            'synced_accounts': [],
            'failed_accounts': [],
            'message': 'No linked accounts to sync',
        }), 200

    synced_accounts = []
    failed_accounts = []
    grand_added = 0
    grand_skipped = 0

    for la in targets:
        if la.user_id != current_user.id:
            failed_accounts.append({
                'account_id': la.id,
                'account_nickname': la.account_nickname,
                'message': 'Unauthorized',
            })
            continue
        if la.api_link_valid is False:
            failed_accounts.append({
                'account_id': la.id,
                'account_nickname': la.account_nickname,
                'message': RECONNECT_LINK_MSG,
            })
            continue
        result = _sync_linked_account_transactions(current_user, la)
        if result.get('error'):
            failed_accounts.append({
                'account_id': la.id,
                'account_nickname': la.account_nickname,
                'message': result['error'],
            })
            continue
        added = int(result.get('added') or 0)
        skipped = int(result.get('skipped_duplicates') or 0)
        grand_added += added
        grand_skipped += skipped
        synced_accounts.append({
            'account_id': la.id,
            'account_nickname': la.account_nickname,
            'new_transactions': added,
            'skipped_duplicates': skipped,
        })

    try:
        _apply_bank_holder_name_to_linked(current_user)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({'status': 'error', 'message': str(e)}), 500

    n_synced = len(synced_accounts)
    n_failed = len(failed_accounts)
    multi = len(targets) > 1

    if n_synced == 0 and n_failed > 0:
        return jsonify({
            'status': 'error',
            'sync_result': 'failed',
            'new_transactions': 0,
            'skipped_duplicates': 0,
            'synced_accounts': [],
            'failed_accounts': failed_accounts,
            'message': failed_accounts[0]['message'] if n_failed == 1 else 'All account syncs failed',
        }), 503

    if multi and n_failed > 0:
        msg = (
            f'Synced {grand_added} new transaction(s); {n_failed} account(s) failed. '
            f'{grand_skipped} duplicate(s) skipped.'
        )
        return jsonify({
            'status': 'success',
            'sync_result': 'partial_success',
            'partial_success': True,
            'new_transactions': grand_added,
            'skipped_duplicates': grand_skipped,
            'synced_accounts': synced_accounts,
            'failed_accounts': failed_accounts,
            'message': msg,
        }), 200

    if grand_added == 0:
        msg = (
            'No new transactions found'
            if len(targets) == 1
            else 'No new transactions found for any account'
        )
        if grand_skipped:
            msg = f'No new transactions; {grand_skipped} duplicate(s) skipped'
    elif len(targets) == 1:
        msg = f'Synced {grand_added} new transaction(s) for {targets[0].account_nickname}'
        if grand_skipped:
            msg += f' ({grand_skipped} duplicate(s) skipped)'
    else:
        msg = f'Synced {grand_added} new transaction(s) across {len(targets)} account(s)'
        if grand_skipped:
            msg += f'; {grand_skipped} duplicate(s) skipped'

    return jsonify({
        'status': 'success',
        'sync_result': 'full_success',
        'new_transactions': grand_added,
        'skipped_duplicates': grand_skipped,
        'synced_accounts': synced_accounts,
        'failed_accounts': failed_accounts,
        'message': msg,
    }), 200


@app.route('/api/accounts/sync', methods=['POST'])
@login_required
def accounts_sync():
    """Sync one linked account or all linked accounts for the current user."""
    try:
        resp, code = _perform_linked_accounts_sync()
        return resp, code
    except Exception as e:
        db.session.rollback()
        print(f"Sync error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': 'Failed to sync transactions'}), 500


@app.route('/api/bank/sync', methods=['POST'])
@login_required
def bank_sync_account():
    """Legacy path; same body as POST /api/accounts/sync."""
    try:
        resp, code = _perform_linked_accounts_sync()
        return resp, code
    except Exception as e:
        db.session.rollback()
        print(f"Sync error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': 'Failed to sync transactions'}), 500


@app.route('/api/linked-accounts/<int:account_id>/delete', methods=['POST'])
@login_required
def delete_linked_account(account_id):
    """Delete a linked account"""
    try:
        account = LinkedAccount.query.get(account_id)
        
        if not account or account.user_id != current_user.id:
            return jsonify({'status': 'error', 'message': 'Unauthorized'}), 403
        
        account_name = account.account_nickname
        
        Transaction.query.filter_by(account_id=account_id).delete()
        db.session.delete(account)
        db.session.commit()
        
        return jsonify({
            'status': 'success',
            'message': f'Deleted {account_name} and associated transactions'
        })
    
    except Exception as e:
        db.session.rollback()
        print(f"Delete error: {e}")
        return jsonify({'status': 'error', 'message': 'Failed to delete account'}), 500


# ============================================================================
# API ROUTES - LEGACY ACCOUNT MANAGEMENT
# ============================================================================

@app.route('/api/accounts/<int:account_id>/set-active', methods=['POST'])
@login_required
def set_account_active(account_id):
    try:
        account = BankAccount.query.get(account_id)
        if not account or account.user_id != current_user.id:
            return jsonify({'status': 'error', 'message': 'Unauthorized'}), 403
        
        set_active_account(current_user, account_id)
        
        return jsonify({
            'status': 'success',
            'message': f'Switched to {account.account_name}'
        })
    except Exception as e:
        print(f"Set active error: {e}")
        return jsonify({'status': 'error', 'message': 'Failed to set active account'}), 500


@app.route('/api/accounts/<int:account_id>/rename', methods=['POST'])
@login_required
def rename_account(account_id):
    try:
        account = BankAccount.query.get(account_id)
        if not account or account.user_id != current_user.id:
            return jsonify({'status': 'error', 'message': 'Unauthorized'}), 403
        
        data = request.get_json()
        new_name = data.get('account_name', '').strip()
        
        if not new_name:
            return jsonify({'status': 'error', 'message': 'Account name cannot be empty'}), 400
        
        account.account_name = new_name
        db.session.commit()
        
        return jsonify({
            'status': 'success',
            'message': 'Account renamed successfully',
            'account_name': new_name
        })
    except Exception as e:
        print(f"Rename error: {e}")
        return jsonify({'status': 'error', 'message': 'Failed to rename account'}), 500


@app.route('/api/accounts/<int:account_id>/toggle', methods=['POST'])
@login_required
def toggle_account_active(account_id):
    try:
        account = BankAccount.query.get(account_id)
        if not account or account.user_id != current_user.id:
            return jsonify({'status': 'error', 'message': 'Unauthorized'}), 403
        
        account.is_active = not account.is_active
        db.session.commit()
        
        return jsonify({
            'status': 'success',
            'message': f'Account {("activated" if account.is_active else "deactivated")} successfully',
            'is_active': account.is_active
        })
    except Exception as e:
        print(f"Toggle error: {e}")
        return jsonify({'status': 'error', 'message': 'Failed to toggle account'}), 500


# ============================================================================
# API ROUTES - DATA ENDPOINTS
# ============================================================================

@app.route('/api/spending-by-category')
@login_required
def spending_by_category():
    """Get spending by category - supports LinkedAccounts"""
    try:
        selected_month = request.args.get('month')
        account_param = request.args.get('account', 'all')
        
        if not selected_month:
            selected_month = datetime.now().strftime('%Y-%m')
        
        try:
            year, month = map(int, selected_month.split('-'))
        except:
            year = datetime.now().year
            month = datetime.now().month
        
        print(f"Chart API: month={selected_month}, account={account_param}")
        
        # Parse account parameter
        account_type, account_id = parse_account_param(account_param)
        
        if account_type == 'all':
            spending_data = db.session.query(
                Category.name,
                func.sum(Transaction.amount).label('total')
            ).join(
                Transaction, Transaction.category_id == Category.id
            ).filter(
                Transaction.user_id == current_user.id,
                extract('month', Transaction.date) == month,
                extract('year', Transaction.date) == year,
                _debit_only_filter(),
            ).group_by(Category.name).all()
            
            uncategorized = db.session.query(
                func.sum(Transaction.amount).label('total')
            ).filter(
                Transaction.user_id == current_user.id,
                Transaction.category_id == None,
                extract('month', Transaction.date) == month,
                extract('year', Transaction.date) == year,
                _debit_only_filter(),
            ).scalar()
        
        elif account_type == 'legacy':
            account = BankAccount.query.get(account_id)
            if not account or account.user_id != current_user.id:
                print(f"Chart API: legacy account {account_id} not found")
                return jsonify({
                    'status': 'success',
                    'message': 'No data for this account',
                    'labels': [],
                    'data': [],
                }), 200
            
            spending_data = db.session.query(
                Category.name,
                func.sum(Transaction.amount).label('total')
            ).join(Transaction).filter(
                Transaction.bank_account_id == account_id,
                extract('month', Transaction.date) == month,
                extract('year', Transaction.date) == year,
                _debit_only_filter(),
            ).group_by(Category.name).all()
            
            uncategorized = db.session.query(
                func.sum(Transaction.amount).label('total')
            ).filter(
                Transaction.bank_account_id == account_id,
                Transaction.category_id == None,
                extract('month', Transaction.date) == month,
                extract('year', Transaction.date) == year,
                _debit_only_filter(),
            ).scalar()
        
        else:  # linked
            account = LinkedAccount.query.get(account_id)
            if not account or account.user_id != current_user.id:
                print(f"Chart API: linked account {account_id} not found")
                return jsonify({
                    'status': 'success',
                    'message': 'No data for this account',
                    'labels': [],
                    'data': [],
                }), 200
            
            print(f"Chart API: linked account filter {account.account_nickname}")
            
            spending_data = db.session.query(
                Category.name,
                func.sum(Transaction.amount).label('total')
            ).join(Transaction).filter(
                Transaction.account_id == account_id,
                extract('month', Transaction.date) == month,
                extract('year', Transaction.date) == year,
                _debit_only_filter(),
            ).group_by(Category.name).all()
            
            uncategorized = db.session.query(
                func.sum(Transaction.amount).label('total')
            ).filter(
                Transaction.account_id == account_id,
                Transaction.category_id == None,
                extract('month', Transaction.date) == month,
                extract('year', Transaction.date) == year,
                _debit_only_filter(),
            ).scalar()
        
        labels = [item[0] for item in spending_data]
        data = [float(item[1]) for item in spending_data]
        
        if uncategorized and uncategorized > 0:
            labels.append('Uncategorized')
            data.append(float(uncategorized))
        
        print(f"Chart API: {len(labels)} categories, total: {sum(data)}")
        
        return jsonify({
            'status': 'success',
            'message': 'OK',
            'labels': labels,
            'data': data,
        })
    
    except Exception as e:
        print(f"Chart API error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'status': 'error',
            'message': 'Could not load spending data',
            'labels': [],
            'data': [],
        }), 200


@app.route('/api/monthly-activity')
@login_required
def monthly_activity():
    """Get aggregated spending and income by month for the dashboard graph."""
    try:
        account_param = request.args.get('account', 'all')
        account_type, account_id = parse_account_param(account_param)
        
        base_filter = [Transaction.user_id == current_user.id]
        if account_type == 'legacy':
            base_filter = [Transaction.bank_account_id == account_id]
        elif account_type == 'linked':
            base_filter = [Transaction.account_id == account_id]
            
        data = db.session.query(
            func.strftime('%Y-%m', Transaction.date).label('month'),
            Transaction.transaction_type,
            func.sum(Transaction.amount).label('total')
        ).filter(
            *base_filter
        ).group_by(
            'month',
            Transaction.transaction_type
        ).order_by('month').all()
        
        months = sorted(list(set([row[0] for row in data if row[0]])))
        if len(months) > 6:
            months = months[-6:]
            
        income_map = {row[0]: float(row[2]) for row in data if row[0] and row[1] == 'credit'}
        expense_map = {row[0]: float(row[2]) for row in data if row[0] and (row[1] == 'debit' or row[1] is None)}
        
        income_data = [income_map.get(m, 0.0) for m in months]
        expense_data = [expense_map.get(m, 0.0) for m in months]
        
        labels = [datetime.strptime(m, '%Y-%m').strftime('%b %Y') for m in months] if months else []
        
        return jsonify({
            'status': 'success',
            'labels': labels,
            'income': income_data,
            'expense': expense_data
        })
    except Exception as e:
        print(f"Monthly activity API error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'status': 'error',
            'labels': [],
            'income': [],
            'expense': []
        })


@app.route('/api/transactions/<int:tx_id>/categorize', methods=['POST'])
@login_required
def categorize_manual(tx_id):
    try:
        transaction = Transaction.query.get(tx_id)
        
        if not transaction:
            return jsonify({'status': 'error', 'message': 'Transaction not found'}), 404
        
        if transaction.user_id != current_user.id:
            return jsonify({'status': 'error', 'message': 'Unauthorized'}), 403
        
        data = request.get_json()
        category_id = data.get('category_id')
        
        if category_id == '' or category_id == 'null':
            category_id = None
        elif category_id:
            try:
                category_id = int(category_id)
                category = Category.query.get(category_id)
                if not category:
                    return jsonify({'status': 'error', 'message': 'Invalid category'}), 400
            except ValueError:
                return jsonify({'status': 'error', 'message': 'Invalid category ID'}), 400
        
        transaction.category_id = category_id
        db.session.commit()
        
        return jsonify({
            'status': 'success',
            'message': 'Category updated',
            'transaction': transaction.to_dict(),
        })
    
    except Exception as e:
        db.session.rollback()
        print(f"Categorization error: {e}")
        return jsonify({'status': 'error', 'message': 'Failed to update category'}), 500


@app.route('/api/demo/generate-data', methods=['POST'])
@login_required
def generate_demo_data():
    """DEPRECATED: Use FastAPI sync instead"""
    return jsonify({
        'status': 'error',
        'message': 'Demo data generation disabled. Use FastAPI sync to get real transactions.'
    }), 400

@app.route('/retirement', methods=['GET'])
@login_required
def retirement():
    """Main retirement planning page."""
    try:
        goal = RetirementGoal.query.filter_by(user_id=current_user.id).first()
        plan = RetirementPlan.query.filter_by(user_id=current_user.id).first()
        milestones = RetirementMilestone.query.filter_by(user_id=current_user.id).all()
        insights = get_spending_insights(current_user.id, goal)

        return render_template(
            'retirement.html',
            page_name='retirement',
            user_accounts=get_user_accounts(current_user),
            linked_accounts=get_linked_accounts(current_user),
            goal=goal,
            plan=plan,
            milestones=milestones,
            insights=insights,
        )
    except Exception as e:
        print(f"Retirement page error: {e}")
        import traceback; traceback.print_exc()
        flash('Error loading retirement page.', 'error')
        return redirect(url_for('dashboard'))
 
 
@app.route('/retirement/setup', methods=['POST'])
@login_required
def retirement_setup():
    """Create or update a RetirementGoal."""
    try:
        current_age           = int(request.form.get('current_age', 25))
        retirement_age        = int(request.form.get('retirement_age', 60))
        target_monthly_income = float(request.form.get('target_monthly_income', 50000))
        current_monthly_contribution = float(request.form.get('current_monthly_contribution', 0))
        expected_return       = float(request.form.get('expected_return', 12.0))
        inflation_rate        = float(request.form.get('inflation_rate', 6.0))
        life_expectancy       = int(request.form.get('life_expectancy', 85))
 
        # Basic validation
        if retirement_age <= current_age:
            flash('Retirement age must be greater than your current age.', 'error')
            return redirect(url_for('retirement'))
        if life_expectancy <= retirement_age:
            flash('Life expectancy must be greater than retirement age.', 'error')
            return redirect(url_for('retirement'))
 
        # Upsert goal
        goal = RetirementGoal.query.filter_by(user_id=current_user.id).first()
        if not goal:
            goal = RetirementGoal(user_id=current_user.id)
            db.session.add(goal)
 
        goal.current_age                  = current_age
        goal.retirement_age               = retirement_age
        goal.target_monthly_income        = target_monthly_income
        goal.current_monthly_contribution = current_monthly_contribution
        goal.expected_return              = expected_return
        goal.inflation_rate               = inflation_rate
        goal.life_expectancy              = life_expectancy
        goal.updated_at                   = datetime.utcnow()
        db.session.commit()
 
        # Calculate + save plan
        calc = calculate_retirement_plan(goal)
        plan = save_retirement_plan(current_user.id, goal, calc)
 
        # Check milestones
        check_and_award_milestones(current_user.id, goal, plan)
 
        flash('Retirement goal updated! Here\'s your personalised plan. 🎯', 'success')
        return redirect(url_for('retirement'))
 
    except Exception as e:
        db.session.rollback()
        print(f"Retirement setup error: {e}")
        import traceback; traceback.print_exc()
        flash('Failed to save retirement goal. Please try again.', 'error')
        return redirect(url_for('retirement'))
 
 
@app.route('/api/retirement/simulate', methods=['POST'])
@login_required
def retirement_simulate():
    """
    API: run a what-if simulation without saving.
    Body: { monthly_investment: float, retire_age: int }
    """
    try:
        goal = RetirementGoal.query.filter_by(user_id=current_user.id).first()
        if not goal:
            return jsonify({'status': 'error', 'message': 'No retirement goal set'}), 404
 
        data = request.get_json() or {}
        monthly_investment = float(data.get('monthly_investment', goal.current_monthly_contribution))
        retire_age = int(data.get('retire_age', goal.retirement_age))
 
        if retire_age <= goal.current_age:
            return jsonify({
                'status': 'error',
                'message': 'Retirement age must be greater than current age',
            }), 400
 
        result = simulate_scenario(goal, monthly_investment, retire_age)
        payload = dict(result) if isinstance(result, dict) else {'result': result}
        payload['status'] = 'success'
        payload['message'] = 'OK'
        return jsonify(payload)
 
    except Exception as e:
        print(f"Simulation error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/retirement/analysis', methods=['GET'])
@login_required
def retirement_analysis():
    """
    NEW: Data-driven retirement intelligence API.
    Returns full JSON: finances, insights, portfolios.
    """
    try:
        analysis = get_retirement_analysis(current_user.id)
        analysis = dict(analysis)
        analysis['status'] = 'success'
        analysis.setdefault('message', analysis.get('message'))
        return jsonify(analysis)
    except Exception as e:
        print(f"Retirement analysis error: {e}")
        import traceback; traceback.print_exc()
        return jsonify({
            'status': 'error',
            'message': 'Analysis failed — sync transactions first',
            'has_data': False,
        }), 500


@app.route('/dev/reset-data', methods=['POST'])
def dev_reset_data():
    """
    Development only: delete users, transactions, bank/linked accounts, retirement rows.
    Categories are preserved.
    """
    if not current_app.config.get('DEBUG'):
        return jsonify({'status': 'error', 'message': 'Forbidden'}), 403
    try:
        Transaction.query.delete()
        RetirementMilestone.query.delete()
        RetirementPlan.query.delete()
        RetirementGoal.query.delete()
        LinkedAccount.query.delete()
        BankAccount.query.delete()
        User.query.delete()
        db.session.commit()
        return jsonify(
            {
                'status': 'success',
                'message': 'All user and transaction data cleared (categories preserved).',
            }
        )
    except Exception as e:
        db.session.rollback()
        return jsonify({'status': 'error', 'message': str(e)}), 500


def _init_app_db():
    with app.app_context():
        db.create_all()
        _ensure_sqlite_user_columns()
        _ensure_sqlite_linked_account_columns()
        _ensure_sqlite_transaction_columns()
        initialize_categories()
        initialize_banks()
        _repair_identity_consistency()


_init_app_db()


if __name__ == '__main__':
    # Check API server on startup
    print("\n" + "="*60)
    print("Starting FinTrack Flask Application")
    print("="*60)
    bank_api.check_api_server()
    print("="*60 + "\n")

    app.run(debug=True, host='0.0.0.0', port=5000)

# NOTE: Do not append @app.route handlers below.
# When using `run.py` or `import app`, __name__ is not "__main__", so this block
# is skipped but any code AFTER it still runs — duplicate routes cause:
# AssertionError: View function mapping is overwriting an existing endpoint function