"""
Initialize database with new multi-account schema
Run this ONCE to set up the database properly
"""

from app import app
from models import db, User, BankAccount, Category, Transaction

def init_database():
    """Create all tables with new schema"""
    with app.app_context():
        print("🗄️  Creating database tables...")
        db.create_all()
        print("✅ Database tables created!")
        
        # Verify tables exist
        print("\n📊 Initializing categories...")
        from services import initialize_categories
        initialize_categories()
        print("✅ Categories initialized!")
        
        print("\n✨ Database initialization complete!")
        print("You can now run: python migrate_to_multi_account.py\n")

if __name__ == '__main__':
    init_database()
