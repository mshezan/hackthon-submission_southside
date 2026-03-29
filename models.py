from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from datetime import datetime

db = SQLAlchemy()


class User(UserMixin, db.Model):
    """User model with authentication"""
    __tablename__ = 'users'
    
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    password_hash = db.Column(db.String(500), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    aa_token = db.Column(db.String(500), nullable=True)
    # PAN-like identity for account aggregation (nullable for legacy users)
    identity_id = db.Column(db.String(64), nullable=True, index=True)
    full_name = db.Column(db.String(255), nullable=True)
    
    bank_accounts = db.relationship('BankAccount', backref='user', lazy=True, cascade='all, delete-orphan')
    linked_accounts = db.relationship('LinkedAccount', backref='user', lazy=True, cascade='all, delete-orphan')
    transactions = db.relationship('Transaction', backref='user', lazy=True, cascade='all, delete-orphan')
    
    def __repr__(self):
        return f'<User {self.email}>'
    
    def to_dict(self):
        return {
            'id': self.id,
            'email': self.email,
            'identity_id': self.identity_id,
            'full_name': self.full_name,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }


class Bank(db.Model):
    """Institution (HDFC, ICICI, SBI, …) — one user can link accounts across many banks."""
    __tablename__ = 'banks'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)

    linked_accounts = db.relationship('LinkedAccount', backref='bank', lazy=True)

    def __repr__(self):
        return f'<Bank {self.name}>'


class BankAccount(db.Model):
    """Legacy Bank Account model"""
    __tablename__ = 'bank_accounts'
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    
    account_name = db.Column(db.String(255), nullable=False, default='Primary Account')
    account_type = db.Column(db.String(50), nullable=True)
    aa_token = db.Column(db.String(500), nullable=False)
    
    is_active = db.Column(db.Boolean, default=True)
    balance = db.Column(db.Numeric(12, 2), default=0.0)
    last_synced = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    transactions = db.relationship('Transaction', backref='bank_account', lazy=True)
    
    def __repr__(self):
        return f'<BankAccount {self.account_name}>'
    
    def to_dict(self):
        return {
            'id': self.id,
            'account_name': self.account_name,
            'account_type': self.account_type,
            'is_active': self.is_active,
            'balance': float(self.balance) if self.balance else 0.0,
            'last_synced': self.last_synced.isoformat() if self.last_synced else None,
            'transaction_count': len(self.transactions),
            'created_at': self.created_at.isoformat() if self.created_at else None
        }


class LinkedAccount(db.Model):
    """
    Enhanced LinkedAccount model with FastAPI integration
    Links Flask app to FastAPI mock bank accounts
    """
    __tablename__ = 'linked_accounts'
    __table_args__ = (
        db.UniqueConstraint('user_id', 'api_account_id', name='uq_linked_user_api_account'),
    )
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    
    # User-provided information
    account_nickname = db.Column(db.String(255), nullable=False)
    
    # API Integration - CRITICAL FIELD
    api_account_id = db.Column(db.Integer, nullable=False, index=True)

    bank_id = db.Column(db.Integer, db.ForeignKey('banks.id'), nullable=True)
    masked_account_number = db.Column(db.String(32), nullable=True)
    
    # Cached data from API (updated on sync)
    api_account_name = db.Column(db.String(255), nullable=True)
    api_account_type = db.Column(db.String(100), nullable=True)
    api_balance = db.Column(db.Numeric(12, 2), nullable=True)
    # False when bank no longer returns this api_account_id (e.g. after mock ID scheme change)
    api_link_valid = db.Column(db.Boolean, default=True, nullable=False)
    
    # AA Integration fields (for future use)
    aa_consent_id = db.Column(db.String(500), nullable=True)
    consent_status = db.Column(db.String(50), default='active')
    
    # Metadata
    is_active = db.Column(db.Boolean, default=True)
    last_synced = db.Column(db.DateTime, nullable=True)
    creation_date = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    
    # Relationships
    transactions = db.relationship('Transaction', 
                                  foreign_keys='Transaction.account_id',
                                  backref='linked_account', 
                                  lazy=True)
    
    def __repr__(self):
        return f'<LinkedAccount {self.account_nickname} (API ID: {self.api_account_id})>'
    
    def to_dict(self):
        bank_name = self.bank.name if self.bank else None
        return {
            'id': self.id,
            'account_nickname': self.account_nickname,
            'api_account_id': self.api_account_id,
            'bank_id': self.bank_id,
            'bank_name': bank_name,
            'masked_account_number': self.masked_account_number,
            'api_account_name': self.api_account_name,
            'api_account_type': self.api_account_type,
            'api_balance': float(self.api_balance) if self.api_balance else 0.0,
            'api_link_valid': self.api_link_valid if self.api_link_valid is not None else True,
            'is_active': self.is_active,
            'consent_status': self.consent_status,
            'last_synced': self.last_synced.isoformat() if self.last_synced else None,
            'creation_date': self.creation_date.isoformat() if self.creation_date else None,
            'transaction_count': len(self.transactions)
        }


class Category(db.Model):
    """Transaction category model"""
    __tablename__ = 'categories'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    
    transactions = db.relationship('Transaction', backref='category', lazy=True)
    
    def __repr__(self):
        return f'<Category {self.name}>'


class Transaction(db.Model):
    """
    Enhanced Transaction model with API data fields
    Maps to FastAPI TransactionSchema
    """
    __tablename__ = 'transactions'
    __table_args__ = (
        db.UniqueConstraint(
            'account_id',
            'transaction_hash',
            name='uq_tx_linked_account_hash',
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    
    # Account associations
    bank_account_id = db.Column(db.Integer, db.ForeignKey('bank_accounts.id'), nullable=True)
    account_id = db.Column(db.Integer, db.ForeignKey('linked_accounts.id'), nullable=True)
    
    category_id = db.Column(db.Integer, db.ForeignKey('categories.id'), nullable=True)
    
    # Core transaction data (from API)
    date = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    description = db.Column(db.String(500), nullable=False)  # Stores API 'merchant'
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    
    # NEW: Additional API fields
    mode = db.Column(db.String(50), nullable=True)  # Payment mode (UPI, Card, NEFT, etc.)
    transaction_type = db.Column(db.String(50), default='debit')  # debit/credit
    narration = db.Column(db.String(500), nullable=True)  # Additional description

    # Dedup key for linked-account imports: sha256(linked_account_id|date|amount|description)
    transaction_hash = db.Column(db.String(64), nullable=True, index=True)
    
    # Metadata
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    def __repr__(self):
        return f'<Transaction {self.description} - ₹{self.amount}>'
    
    def to_dict(self):
        return {
            'id': self.id,
            'date': self.date.strftime('%Y-%m-%d'),
            'description': self.description,
            'amount': float(self.amount),
            'mode': self.mode,
            'transaction_type': self.transaction_type,
            'narration': self.narration,
            'category': self.category.name if self.category else 'Uncategorized',
            'bank_account': self.bank_account.account_name if self.bank_account else None,
            'linked_account': self.linked_account.account_nickname if self.linked_account else None,
            'account_id': self.account_id or self.bank_account_id
        }
class RetirementGoal(db.Model):
    """User's retirement planning goal"""
    __tablename__ = 'retirement_goals'
 
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, unique=True)
 
    current_age = db.Column(db.Integer, nullable=False)
    retirement_age = db.Column(db.Integer, nullable=False, default=60)
    target_monthly_income = db.Column(db.Numeric(12, 2), nullable=False)   # desired monthly income post-retirement
    current_monthly_contribution = db.Column(db.Numeric(12, 2), default=0) # amount already saving/investing per month
    expected_return = db.Column(db.Float, default=12.0)    # annual % return
    inflation_rate = db.Column(db.Float, default=6.0)      # annual % inflation
    life_expectancy = db.Column(db.Integer, default=85)    # years expected to live
 
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
 
    plan = db.relationship('RetirementPlan', backref='goal', uselist=False, cascade='all, delete-orphan')
    milestones = db.relationship('RetirementMilestone', backref='goal', lazy=True, cascade='all, delete-orphan')
 
    def __repr__(self):
        return f'<RetirementGoal user={self.user_id} retire_at={self.retirement_age}>'
 
    def to_dict(self):
        return {
            'id': self.id,
            'current_age': self.current_age,
            'retirement_age': self.retirement_age,
            'target_monthly_income': float(self.target_monthly_income),
            'current_monthly_contribution': float(self.current_monthly_contribution),
            'expected_return': self.expected_return,
            'inflation_rate': self.inflation_rate,
            'life_expectancy': self.life_expectancy,
        }
 
 
class RetirementPlan(db.Model):
    """Calculated retirement plan (updated on every recalculation)"""
    __tablename__ = 'retirement_plans'
 
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, unique=True)
    goal_id = db.Column(db.Integer, db.ForeignKey('retirement_goals.id'), nullable=False)
 
    target_corpus = db.Column(db.Numeric(15, 2), nullable=False)           # total money needed
    projected_corpus = db.Column(db.Numeric(15, 2), nullable=False)        # corpus if current savings continue
    required_monthly_contribution = db.Column(db.Numeric(12, 2), nullable=False)
    readiness_score = db.Column(db.Integer, default=0)   # 0–100
    years_to_retirement = db.Column(db.Integer)
 
    last_calculated = db.Column(db.DateTime, default=datetime.utcnow)
 
    def __repr__(self):
        return f'<RetirementPlan user={self.user_id} score={self.readiness_score}>'
 
    def to_dict(self):
        return {
            'target_corpus': float(self.target_corpus),
            'projected_corpus': float(self.projected_corpus),
            'required_monthly_contribution': float(self.required_monthly_contribution),
            'readiness_score': self.readiness_score,
            'years_to_retirement': self.years_to_retirement,
            'last_calculated': self.last_calculated.isoformat(),
        }
 
 
class RetirementMilestone(db.Model):
    """Gamification milestones for retirement journey"""
    __tablename__ = 'retirement_milestones'
 
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    goal_id = db.Column(db.Integer, db.ForeignKey('retirement_goals.id'), nullable=False)
 
    milestone_key = db.Column(db.String(100), nullable=False)   # e.g. 'first_10k', 'score_50'
    milestone_label = db.Column(db.String(255), nullable=False)
    achieved_at = db.Column(db.DateTime, default=datetime.utcnow)
 
    def __repr__(self):
        return f'<RetirementMilestone {self.milestone_key}>'