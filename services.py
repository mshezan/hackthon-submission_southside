import re
from datetime import datetime
from typing import Optional

from models import Bank, BankAccount, Category, db

DEFAULT_BANK_NAMES = (
    'HDFC Bank',
    'ICICI Bank',
    'State Bank of India',
)


def initialize_banks():
    """Seed bank institutions if missing."""
    for name in DEFAULT_BANK_NAMES:
        if not Bank.query.filter_by(name=name).first():
            db.session.add(Bank(name=name))
    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"Warning: Could not initialize banks: {e}")


def get_or_create_bank_by_name(name: str) -> Optional[Bank]:
    """Resolve API bank label to Bank row (fuzzy match on known names)."""
    if not name or not str(name).strip():
        return None
    name = str(name).strip()
    bank = Bank.query.filter_by(name=name).first()
    if bank:
        return bank
    # Prefix / alias match
    nlow = name.lower()
    for b in Bank.query.all():
        if b.name.lower() in nlow or nlow in b.name.lower():
            return b
    # New bank row for unknown institutions
    bank = Bank(name=name[:120])
    db.session.add(bank)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return Bank.query.filter_by(name=name[:120]).first()
    return bank

# Indian vendor categorization keywords (substring match unless listed in _BOUNDARY_KEYWORDS)
CATEGORY_KEYWORDS = {
    # Food — include common app names and spellings
    'Food & Drink': ['swiggy', 'zomato', 'mcdonalds', 'mcd', 'starbucks', 'cafe coffee day', 'ccd',
                     'dominos', 'pizza hut', 'eatsure', 'burger king', 'kfc', 'subway', 'dunkin',
                     'instamart'],
    'Groceries': ['dmart', 'd-mart', 'bigbasket', 'big basket', 'blinkit', 'zepto', 'grofers', 'jiomart',
                  'reliance fresh', 'more', 'spencers', 'nature basket', 'star bazaar'],
    'Fuel': ['indian oil', 'ioc', 'hpcl', 'hindustan petroleum', 'bharat petroleum', 'bpcl', 
             'shell', 'essar', 'reliance petroleum', 'petrol', 'diesel', 'fuel'],
    'Subscriptions': ['netflix', 'spotify', 'prime video', 'amazon prime', 'hotstar', 'disney', 
                      'jiocinema', 'sonyliv', 'zee5', 'apple music', 'youtube premium', 'voot'],
    'Utilities': ['bses', 'tata power', 'bescom', 'adani electricity', 'airtel', 'jio', 'vodafone', 
                  'vi', 'bsnl', 'mtnl', 'electricity', 'water bill', 'gas bill', 'piped gas', 
                  'indraprastha gas', 'mahanagar gas'],
    'Transport': ['uber', 'ola', 'uber eats', 'rapido', 'redbus', 'irctc', 'metro', 'delhi metro',
                  'mumbai metro', 'bangalore metro', 'namma metro', 'makemytrip', 'goibibo', 'yatra'],
    'Shopping': ['amazon', 'amazon.in', 'amazon pay', 'flipkart', 'myntra', 'meesho', 'ajio', 'nykaa',
                 'reliance digital', 'croma', 'vijay sales', 'lifestyle', 'westside', 'max fashion',
                 'pantaloons'],
    'Payments': ['paytm', 'phonepe', 'gpay', 'google pay', 'bhim', 'upi', 'mobikwik'],
    'Rent/EMI': ['rent', 'emi', 'housing loan', 'home loan', 'hdfc', 'icici', 'sbi', 'axis']
}

# Whole-token match only — avoids "rent" in "current", "ola" in "cola", etc.
_BOUNDARY_KEYWORDS = frozenset({'rent', 'ola', 'emi', 'axis', 'sbi'})


def _normalize_description(text: Optional[str]) -> str:
    """Lowercase, trim, collapse internal whitespace (and common unicode dashes)."""
    if not text:
        return ''
    s = str(text).strip().lower()
    s = s.replace('\u2013', '-').replace('\u2014', '-').replace('\u2212', '-')
    s = re.sub(r'\s+', ' ', s)
    return s


def _keyword_in_haystack(keyword: str, haystack: str) -> bool:
    if not keyword or not haystack:
        return False
    if keyword in _BOUNDARY_KEYWORDS:
        return bool(
            re.search(
                rf'(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])',
                haystack,
                re.IGNORECASE,
            )
        )
    return keyword in haystack


def categorize_transaction(transaction):
    """
    Automatically categorize a transaction based on description.
    FIXED: Better error handling and performance optimization
    """
    if transaction.category_id is not None:
        return False  # Already categorized

    haystack = _normalize_description(transaction.description)

    # Iterate through categories and keywords
    for category_name, keywords in CATEGORY_KEYWORDS.items():
        for keyword in keywords:
            if _keyword_in_haystack(keyword, haystack):
                # Find category (cached query)
                category = Category.query.filter_by(name=category_name).first()
                if category:
                    transaction.category_id = category.id
                    return True
    
    return False


def initialize_categories():
    """
    Initialize default categories in the database.
    FIXED: Better error handling and duplicate prevention
    """
    default_categories = list(CATEGORY_KEYWORDS.keys()) + ['Uncategorized', 'Other', 'Income']
    
    for category_name in default_categories:
        # Check if category already exists
        if not Category.query.filter_by(name=category_name).first():
            category = Category(name=category_name)
            db.session.add(category)
    
    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"Warning: Could not initialize categories: {e}")


def get_user_accounts(user):
    """
    Get all bank accounts for a user
    Returns list of BankAccount objects
    """
    return BankAccount.query.filter_by(user_id=user.id).order_by(BankAccount.created_at.desc()).all()


def get_active_account(user):
    """
    Get the currently active bank account for a user
    Returns BankAccount object or None
    """
    active = BankAccount.query.filter_by(user_id=user.id, is_active=True).first()
    
    # If no active account, make the first one active
    if not active:
        accounts = get_user_accounts(user)
        if accounts:
            accounts[0].is_active = True
            db.session.commit()
            return accounts[0]
    
    return active


def set_active_account(user, account_id):
    """
    Set a specific account as active for the user
    Deactivates all other accounts
    """
    # Deactivate all user's accounts
    BankAccount.query.filter_by(user_id=user.id).update({'is_active': False})
    
    # Activate the selected account
    account = BankAccount.query.get(account_id)
    if account and account.user_id == user.id:
        account.is_active = True
        db.session.commit()
        return True
    
    return False


def get_account_stats(account):
    """
    Get statistics for a specific bank account
    """
    from models import Transaction, Category
    from sqlalchemy import func, or_

    debit_only = or_(
        Transaction.transaction_type == "debit",
        Transaction.transaction_type.is_(None),
    )

    total_spending = db.session.query(
        func.sum(Transaction.amount)
    ).filter(
        Transaction.bank_account_id == account.id,
        debit_only,
    ).scalar() or 0
    
    transaction_count = Transaction.query.filter_by(bank_account_id=account.id).count()
    
    # Get top category
    top_category = db.session.query(
        Category.name,
        func.sum(Transaction.amount).label('total')
    ).join(Transaction).filter(
        Transaction.bank_account_id == account.id,
        debit_only,
    ).group_by(Category.name).order_by(
        func.sum(Transaction.amount).desc()
    ).first()
    
    return {
        'total_spending': float(total_spending),
        'transaction_count': transaction_count,
        'top_category': top_category[0] if top_category else 'N/A',
        'balance': float(account.balance) if account.balance else 0.0
    }
