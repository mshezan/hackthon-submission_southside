from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from models import db, User

auth_bp = Blueprint('auth', __name__)


@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    """
    Complete user registration with full validation.
    """
    # Redirect if already logged in
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        # Get form data
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')
        
        # Validation: Check if fields are empty
        if not email or not password:
            flash('Email and password are required.', 'error')
            return render_template('register.html')
        
        # Validation: Check if passwords match
        if password != confirm_password:
            flash('Passwords do not match.', 'error')
            return render_template('register.html')
        
        # Validation: Check password length
        if len(password) < 6:
            flash('Password must be at least 6 characters long.', 'error')
            return render_template('register.html')
        
        # Validation: Check if email already exists
        existing_user = User.query.filter_by(email=email).first()
        if existing_user:
            flash('Email address already registered. Please log in.', 'error')
            return redirect(url_for('auth.login'))
        
        # Create new user
        try:
            # Generate password hash
            password_hash = generate_password_hash(password, method='pbkdf2:sha256')
            
            # Create user object
            new_user = User(email=email, password_hash=password_hash)
            
            # Save to database
            db.session.add(new_user)
            db.session.commit()
            
            # Auto-login after registration
            login_user(new_user)
            
            flash('Registration successful! Welcome to FinTrack.', 'success')
            return redirect(url_for('dashboard'))
            
        except Exception as e:
            db.session.rollback()
            flash('An error occurred during registration. Please try again.', 'error')
            print(f"Registration error: {e}")
            return render_template('register.html')
    
    # GET request - show registration form
    return render_template('register.html')


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    """
    Complete user login with validation.
    """
    # Redirect if already logged in
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        # Get form data
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        remember = bool(request.form.get('remember'))
        
        # Validation: Check if fields are empty
        if not email or not password:
            flash('Email and password are required.', 'error')
            return render_template('login.html')
        
        # Query user by email
        user = User.query.filter_by(email=email).first()
        
        # Check if user exists and password is correct
        if not user or not check_password_hash(user.password_hash, password):
            flash('Invalid email or password. Please try again.', 'error')
            return render_template('login.html')
        
        # Login successful
        login_user(user, remember=remember)
        
        flash('Login successful! Welcome back.', 'success')
        
        # Redirect to next page or dashboard
        next_page = request.args.get('next')
        if next_page and next_page.startswith('/'):
            return redirect(next_page)
        return redirect(url_for('dashboard'))
    
    # GET request - show login form
    return render_template('login.html')


@auth_bp.route('/logout')
@login_required
def logout():
    """User logout route."""
    logout_user()
    flash('You have been logged out successfully.', 'success')
    return redirect(url_for('auth.login'))
