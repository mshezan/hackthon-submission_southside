#!/usr/bin/env python3
"""
FinTrack Project Setup Script
Automatically creates the entire project structure and files
"""

import os
import sys

def create_directory_structure():
    """Create all required directories"""
    directories = [
        'FinTrack',
        'FinTrack/static',
        'FinTrack/static/css',
        'FinTrack/static/js',
        'FinTrack/templates',
    ]
    
    for directory in directories:
        os.makedirs(directory, exist_ok=True)
        print(f"✓ Created directory: {directory}")

def create_file(filepath, content):
    """Create a file with given content"""
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)
    print(f"✓ Created file: {filepath}")

def setup_project():
    """Main setup function"""
    print("="*60)
    print("FinTrack Project Setup")
    print("="*60)
    print("\nCreating project structure...\n")
    
    create_directory_structure()
    
    # File contents will be added below
    print("\n" + "="*60)
    print("✓ Project structure created successfully!")
    print("="*60)
    print("\nNext steps:")
    print("1. cd FinTrack")
    print("2. python -m venv venv")
    print("3. Activate virtual environment:")
    print("   - Windows: venv\\Scripts\\activate")
    print("   - Mac/Linux: source venv/bin/activate")
    print("4. pip install -r requirements.txt")
    print("5. python run.py")
    print("\n" + "="*60)

if __name__ == '__main__':
    try:
        setup_project()
    except Exception as e:
        print(f"\n✗ Error during setup: {e}")
        sys.exit(1)
