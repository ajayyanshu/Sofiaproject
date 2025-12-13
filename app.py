import base64
import io
import os
import re
import sys
import json
from datetime import datetime, date, timedelta
import uuid
import random
from threading import Thread

# Third-party imports
import docx
import fitz  # PyMuPDF
import google.generativeai as genai
import requests
from flask import (Flask, jsonify, render_template, request, session, redirect,
                   url_for, flash, make_response)
from flask_cors import CORS
from PIL import Image
from pymongo import MongoClient
from bson.objectid import ObjectId
from youtube_transcript_api import YouTubeTranscriptApi
from flask_login import (LoginManager, UserMixin, login_user, logout_user,
                         login_required, current_user)
from flask_mail import Mail, Message
from itsdangerous import URLSafeTimedSerializer, SignatureExpired

app = Flask(__name__, template_folder='templates')
CORS(app)

# --- Configuration ---
# Generate a secure key for production, but this default works for dev
SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "dev-secret-key")
app.config['SECRET_KEY'] = SECRET_KEY
# This salt is used to secure your email tokens. Change it in Render Env Vars.
app.config['SECURITY_PASSWORD_SALT'] = os.environ.get("SECURITY_PASSWORD_SALT", "my_precious_two")

GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY")
MONGO_URI = os.environ.get("MONGO_URI")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
SERPER_API_KEY = os.environ.get("SERPER_API_KEY")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "ajay@123.com")

# --- Gmail / Email Configuration ---
# These settings MUST be in your Render Environment Variables
app.config['MAIL_SERVER'] = os.environ.get('MAIL_SERVER', 'smtp.gmail.com')
app.config['MAIL_PORT'] = int(os.environ.get('MAIL_PORT', 587))
app.config['MAIL_USE_TLS'] = os.environ.get('MAIL_USE_TLS', 'true').lower() in ['true', '1', 't']
app.config['MAIL_USE_SSL'] = os.environ.get('MAIL_USE_SSL', 'false').lower() in ['true', '1', 't']
app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME') # Your Gmail address
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD') # Your Gmail APP PASSWORD
app.config['MAIL_DEFAULT_SENDER'] = os.environ.get('MAIL_DEFAULT_SENDER', app.config['MAIL_USERNAME'])

mail = Mail(app)

# --- Email Helper Functions ---

def send_async_email(app, msg):
    """Sends email in background and prints logs to Render console."""
    with app.app_context():
        try:
            print(f"📧 LOG: Attempting to send email to {msg.recipients}...")
            mail.send(msg)
            print("✅ LOG: Email sent successfully!")
        except Exception as e:
            print(f"❌ LOG: EMAIL ERROR: {e}")

def generate_token(email):
    """Generates a secure token for email verification."""
    serializer = URLSafeTimedSerializer(app.config['SECRET_KEY'])
    return serializer.dumps(email, salt=app.config['SECURITY_PASSWORD_SALT'])

def confirm_token(token, expiration=3600):
    """Verifies the token. Expiration default is 1 hour (3600 seconds)."""
    serializer = URLSafeTimedSerializer(app.config['SECRET_KEY'])
    try:
        email = serializer.loads(
            token,
            salt=app.config['SECURITY_PASSWORD_SALT'],
            max_age=expiration
        )
    except Exception:
        return False
    return email

# --- MongoDB Configuration ---
mongo_client = None
chat_history_collection = None
temporary_chat_collection = None
conversations_collection = None
users_collection = None
library_collection = None

if MONGO_URI:
    try:
        mongo_client = MongoClient(MONGO_URI)
        db = mongo_client.get_database("ai_assistant_db")
        db.command('ping') # Test connection
        chat_history_collection = db.get_collection("chat_history")
        temporary_chat_collection = db.get_collection("temporary_chats")
        conversations_collection = db.get_collection("conversations")
        users_collection = db.get_collection("users")
        library_collection = db.get_collection("library_items")
        print("✅ LOG: Successfully connected to MongoDB.")
    except Exception as e:
        print(f"❌ LOG: CRITICAL DATABASE ERROR: {e}")
else:
    print("⚠️ LOG: MONGO_URI not found. Database features will fail.")

# --- Flask-Login Setup ---
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login_page'

class User(UserMixin):
    def __init__(self, user_data):
        self.id = str(user_data["_id"])
        self.email = user_data.get("email")
        self.name = user_data.get("name")
        self.isAdmin = user_data.get("isAdmin", False)
        self.isPremium = user_data.get("isPremium", False)
        self.session_id = user_data.get("session_id")
        # Ensure we read the verified status from DB (default to False)
        self.is_verified = user_data.get("is_verified", False)

    @staticmethod
    def get(user_id):
        if users_collection is None:
            return None
        try:
            user_data = users_collection.find_one({"_id": ObjectId(user_id)})
            return User(user_data) if user_data else None
        except Exception as e:
            print(f"❌ LOG: User Loader Error: {e}")
            return None

@login_manager.user_loader
def load_user(user_id):
    return User.get(user_id)

@app.before_request
def before_request_callback():
    if current_user.is_authenticated:
        if session.get('session_id') != current_user.session_id:
            logout_user()
            flash("You have been logged out from another device.", "info")
            return redirect(url_for('login_page'))


# --- Routes: Pages ---

@app.route('/')
@login_required
def home():
    return render_template('index.html') 

@app.route('/login.html', methods=['GET'])
def login_page():
    if current_user.is_authenticated:
        return redirect(url_for('home'))
    return render_template('login.html')

@app.route('/signup.html', methods=['GET'])
def signup_page():
    if current_user.is_authenticated:
        return redirect(url_for('home'))
    return render_template('signup.html')

@app.route('/login')
def login_redirect():
    return redirect(url_for('login_page'))

@app.route('/signup')
def signup_redirect():
    return redirect(url_for('signup_page'))


# --- Routes: Authentication API ---

@app.route('/api/signup', methods=['POST'])
def api_signup():
    data = request.get_json()
    name = data.get('name')
    email = data.get('email')
    password = data.get('password')

    if not all([name, email, password]):
        return jsonify({'success': False, 'error': 'Please fill out all fields.'}), 400

    if users_collection is None:
        return jsonify({'success': False, 'error': 'Database error.'}), 500

    if users_collection.find_one({"email": email}):
        return jsonify({'success': False, 'error': 'Email already registered.'}), 409

    # Create new user object
    new_user = {
        "name": name, 
        "email": email, 
        "password": password,
        "isAdmin": email == ADMIN_EMAIL, 
        "isPremium": False, 
        "is_verified": False, # User must verify email
        "session_id": str(uuid.uuid4()),
        "usage_counts": { "messages": 0, "webSearches": 0 },
        "last_usage_reset": datetime.utcnow().strftime('%Y-%m-%d'),
        "timestamp": datetime.utcnow().isoformat()
    }
    
    try:
        users_collection.insert_one(new_user)
        print(f"✅ LOG: User {email} inserted into DB. Preparing email...")
        
        # --- Send Verification Email ---
        token = generate_token(email)
        confirm_url = url_for('confirm_email', token=token, _external=True)
        
        html_body = f"""
        <h2>Welcome to Sofia AI, {name}!</h2>
        <p>Please click the link below to verify your email address:</p>
        <p><a href="{confirm_url}" style="background-color: #4CAF50; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px;">Verify Email</a></p>
        <p>Or copy this link: {confirm_url}</p>
        <br>
        <p>If you did not create this account, please ignore this email.</p>
        """
        
        msg = Message("Verify your Sofia AI Account", recipients=[email], html=html_body)
        Thread(target=send_async_email, args=(app, msg)).start()

        return jsonify({'success': True, 'message': 'Account created! Check your email to verify.'})

    except Exception as e:
        print(f"❌ LOG: Signup Error: {e}")
        return jsonify({'success': False, 'error': 'Error creating account.'}), 500


@app.route('/api/login', methods=['POST'])
def api_login():
    data = request.get_json()
    email = data.get('email')
    password = data.get('password')

    if not all([email, password]):
        return jsonify({'success': False, 'error': 'Missing credentials.'}), 400

    if users_collection is None:
        return jsonify({'success': False, 'error': 'Database error.'}), 500
        
    user_data = users_collection.find_one({"email": email})

    if user_data and user_data.get('password') == password:
        # --- Check Verification Status ---
        if not user_data.get('is_verified', False):
             print(f"⚠️ LOG: Login blocked for {email} - Not Verified")
             return jsonify({'success': False, 'error': 'Please verify your email address before logging in.'}), 403

        new_session_id = str(uuid.uuid4())
        users_collection.update_one({'_id': user_data['_id']}, {'$set': {'session_id': new_session_id}})
        user_data['session_id'] = new_session_id

        user_obj = User(user_data)
        login_user(user_obj)
        session['session_id'] = new_session_id
        return jsonify({'success': True, 'user': {'name': user_data['name'], 'email': user_data['email']}})
    else:
        return jsonify({'success': False, 'error': 'Incorrect email or password.'}), 401


# --- Routes: Email Verification ---

@app.route('/confirm/<token>')
def confirm_email(token):
    try:
        email = confirm_token(token)
    except:
        return render_template('login.html', error_message="The confirmation link is invalid or has expired.")
    
    if not email:
         return render_template('login.html', error_message="Invalid confirmation link.")

    user = users_collection.find_one({"email": email})
    if not user:
        return render_template('login.html', error_message="User not found.")
    
    if user.get("is_verified"):
        print(f"ℹ️ LOG: User {email} already verified.")
    else:
        users_collection.update_one({"email": email}, {"$set": {"is_verified": True}})
        print(f"✅ LOG: User {email} successfully verified.")
        
    # Redirect to login page with query param to show success alert
    return redirect(url_for('login_page', verified='true'))


@app.route('/send_verification_email', methods=['POST'])
@login_required
def send_verification_email_route():
    """Allows sending a new verification email from the Settings menu."""
    if current_user.is_verified:
        return jsonify({'error': 'Email is already verified.'}), 400

    try:
        token = generate_token(current_user.email)
        confirm_url = url_for('confirm_email', token=token, _external=True)
        html_body = f"<p>Hi {current_user.name},</p><p>Click here to verify your email:</p><a href='{confirm_url}'>Verify Email</a>"
        
        msg = Message("Resend: Verify your Sofia AI Account", recipients=[current_user.email], html=html_body)
        Thread(target=send_async_email, args=(app, msg)).start()
        return jsonify({'success': True, 'message': 'Verification email sent.'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/get_user_info')
@login_required
def get_user_info():
    user_data = users_collection.find_one({'_id': ObjectId(current_user.id)})
    usage_counts = user_data.get('usage_counts', {"messages": 0, "webSearches": 0})
    
    return jsonify({
        "name": current_user.name,
        "email": current_user.email,
        "isAdmin": current_user.isAdmin,
        "isPremium": current_user.isPremium,
        "emailVerified": user_data.get("is_verified", False), 
        "usageCounts": usage_counts
    })

# --- Other Standard Routes (Logout, Chat, etc.) ---

@app.route('/logout', methods=['POST'])
@login_required
def logout():
    logout_user()
    return jsonify({'success': True})

@app.route('/logout-all', methods=['POST'])
@login_required
def logout_all_devices():
    try:
        new_session_id = str(uuid.uuid4())
        users_collection.update_one({'_id': ObjectId(current_user.id)}, {'$set': {'session_id': new_session_id}})
        logout_user()
        return jsonify({'success': True, 'message': 'Logged out of all devices.'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/delete_account', methods=['DELETE'])
@login_required
def delete_account():
    try:
        user_id = ObjectId(current_user.id)
        users_collection.update_one(
            {'_id': user_id},
            {
                '$set': {'email': f'deleted_{user_id}@anonymous.com', 'password': 'deleted'},
                '$unset': {'name': "", 'session_id': ""}
            }
        )
        logout_user()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# --- CHAT & LIBRARY APIs (Simplified for length - add your specific logic if modified) ---
# NOTE: Ensure you add back your specific /chat, /api/chats, /library endpoints here.
# For this update, I am assuming you will copy-paste your existing Chat/Library logic 
# below or keep them if you are merging files. 
# ... (Paste your Chat Logic here) ...

# Basic Status Check
@app.route('/status')
def status():
    return jsonify({'status': 'ok'})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
