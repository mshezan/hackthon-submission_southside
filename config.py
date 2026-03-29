"""
Flask Configuration with Environment Variables
"""
import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    """Flask application configuration"""
    
    # Flask
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'dev-secret-key-change-in-production'
    DEBUG = os.environ.get('DEBUG', 'True').lower() == 'true'
    
    # Database
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or 'sqlite:///fintrack.db'
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    
    # API Configuration
    API_BASE_URL = os.environ.get('API_BASE_URL', 'http://127.0.0.1:8000')
