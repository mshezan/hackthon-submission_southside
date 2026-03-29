"""
Migration Script: Convert single-account users to multi-account system
This script is SAFE to run multiple times - it checks before migrating
"""

from app import app
from models import db, User, BankAccount, Transaction
from datetime import datetime


def migrate_user_to_multi_account(user):
    """
    Migrate a single user from old system to new multi-account system
    """
    print(f"\n🔄 Migrating user: {user.email}")
    
    # Check if user already has bank accounts
    existing_accounts = BankAccount.query.filter_by(user_id=user.id).count()
    
    if existing_accounts > 0:
        print(f"  ✓ User already has {existing_accounts} bank account(s) - skipping")
        return False
    
    # Check if user has old aa_token
    if not user.aa_token:
        print(f"  ⚠️ User has no aa_token - nothing to migrate")
        return False
    
    # Create new BankAccount from old token
    print(f"  📝 Creating 'Primary Account' from existing token...")
    
    primary_account = BankAccount(
        user_id=user.id,
        account_name='Primary Account',
        account_type='checking',
        aa_token=user.aa_token,
        is_active=True,
        created_at=user.created_at or datetime.utcnow()
    )
    
    db.session.add(primary_account)
    db.session.flush()  # Get the ID
    
    # Link all existing transactions to this account
    user_transactions = Transaction.query.filter_by(user_id=user.id, bank_account_id=None).all()
    
    if user_transactions:
        print(f"  🔗 Linking {len(user_transactions)} existing transactions to Primary Account...")
        for tx in user_transactions:
            tx.bank_account_id = primary_account.id
    
    # Update account balance
    primary_account.update_balance()
    primary_account.last_synced = datetime.utcnow()
    
    db.session.commit()
    
    print(f"  ✅ Migration complete for {user.email}")
    print(f"     - Created: {primary_account.account_name}")
    print(f"     - Linked: {len(user_transactions)} transactions")
    print(f"     - Balance: ₹{primary_account.balance}")
    
    return True


def migrate_all_users():
    """
    Migrate ALL users in the database to multi-account system
    """
    print("=" * 60)
    print("🚀 MULTI-ACCOUNT MIGRATION SCRIPT")
    print("=" * 60)
    
    users = User.query.all()
    total_users = len(users)
    migrated_count = 0
    skipped_count = 0
    
    print(f"\n📊 Found {total_users} total users")
    
    for user in users:
        result = migrate_user_to_multi_account(user)
        if result:
            migrated_count += 1
        else:
            skipped_count += 1
    
    print("\n" + "=" * 60)
    print("✨ MIGRATION SUMMARY")
    print("=" * 60)
    print(f"Total users: {total_users}")
    print(f"✅ Migrated: {migrated_count}")
    print(f"⏭️  Skipped: {skipped_count}")
    print("=" * 60)
    
    # Verify migration
    verify_migration()


def verify_migration():
    """
    Verify that migration completed successfully
    """
    print("\n🔍 VERIFICATION")
    print("-" * 60)
    
    total_accounts = BankAccount.query.count()
    total_transactions = Transaction.query.count()
    linked_transactions = Transaction.query.filter(Transaction.bank_account_id != None).count()
    orphaned_transactions = Transaction.query.filter(Transaction.bank_account_id == None).count()
    
    print(f"Total Bank Accounts: {total_accounts}")
    print(f"Total Transactions: {total_transactions}")
    print(f"Linked Transactions: {linked_transactions}")
    print(f"Orphaned Transactions: {orphaned_transactions}")
    
    if orphaned_transactions > 0:
        print(f"\n⚠️  WARNING: {orphaned_transactions} transactions are not linked to any account!")
    else:
        print(f"\n✅ All transactions are properly linked!")


if __name__ == '__main__':
    with app.app_context():
        print("\n⚠️  IMPORTANT: This script will modify your database!")
        print("Make sure you have a backup before proceeding.\n")
        
        response = input("Continue with migration? (yes/no): ").strip().lower()
        
        if response == 'yes':
            migrate_all_users()
            print("\n✅ Migration complete! Your app now supports multiple accounts per user.")
            print("You can now restart your Flask app.\n")
        else:
            print("\n❌ Migration cancelled.")
