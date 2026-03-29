#!/usr/bin/env python3
"""
FinTrack Application Runner
Use this file to start the application
"""

from app import app
import os

if __name__ == '__main__':
    print("=" * 70)
    print("🚀 FinTrack - Personal Finance Manager")
    print("=" * 70)
    print()
    print("Application is starting...")
    print()
    print("📍 Local URL: http://localhost:5000")
    print("📍 Network URL: http://0.0.0.0:5000")
    print()
    print("👤 Demo User Credentials:")
    print("   Email: demo@fintrack.com")
    print("   Password: demo123")
    print()
    print("=" * 70)
    print()
    
    # Run the application
    app.run(
        debug=True,
        host='0.0.0.0',
        port=5000,
        use_reloader=True
    )
