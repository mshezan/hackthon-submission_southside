"""
Migration Script: Add LinkedAccount table
Run this once to add the new table to your database
"""

from app import app
from models import db

def migrate():
    with app.app_context():
        print("\n🔄 Adding LinkedAccount table...")
        db.create_all()
        print("✅ Migration complete!")
        print("\n📊 Available tables:")
        print("  - users")
        print("  - bank_accounts (legacy)")
        print("  - linked_accounts (NEW)")
        print("  - transactions")
        print("  - categories")

if __name__ == '__main__':
    migrate()
