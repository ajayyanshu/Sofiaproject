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
from itsdangerous import URLSafeTimedSerializer, SignatureExpired

app = Flask(__name__, template_folder='templates')
CORS(app)

# --- Configuration ---
SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "dev-secret-key")
app.config['SECRET_KEY'] = SECRET_KEY
app.config['SECURITY_PASSWORD_SALT'] = os.environ.get("SECURITY_PASSWORD_SALT", "my_precious_two")

GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY")
MONGO_URI = os.environ.get("MONGO_URI")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
SERPER_API_KEY = os.environ.get("SERPER_API_KEY")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "ajay@123.com")

# --- Brevo Email Configuration ---
BREVO_API_KEY = os.environ.get('BREVO_API_KEY')
# This sender email must be verified in Brevo
SENDER_EMAIL = os.environ.get('MAIL_DEFAULT_SENDER', 'ajayyanshu@gmail.com')
SENDER_NAME = "Sofia AI"

# --- Email Helper Function (Brevo API) ---

def send_async_email(app, to_email, subject, html_content):
    """Sends email via Brevo API (Port 443 - Allowed on Render Free Tier)"""
    with app.app_context():
        try:
            print(f"📧 LOG: Preparing to send to {to_email} via Brevo...")
            
            if not BREVO_API_KEY:
                print("❌ LOG: BREVO_API_KEY is missing! Cannot send email.")
                return

            url = "https://api.brevo.com/v3/smtp/email"
            
            payload = {
                "sender": {
                    "name": SENDER_NAME,
                    "email": SENDER_EMAIL
                },
                "to": [
                    {
                        "email": to_email
                    }
                ],
                "subject": subject,
                "htmlContent": html_content
            }
            
            headers = {
                "accept": "application/json",
                "api-key": BREVO_API_KEY,
                "content-type": "application/json"
            }

            response = requests.post(url, json=payload, headers=headers)
            
            if response.status_code in [200, 201, 202]:
                print(f"✅ LOG: Email sent successfully! Message ID: {response.json().get('messageId')}")
            else:
                print(f"❌ LOG: Brevo Error {response.status_code}: {response.text}")
                
        except Exception as e:
            print(f"❌ LOG: EMAIL EXCEPTION: {e}")

def generate_token(email):
    """Generates a secure token for email verification."""
    serializer = URLSafeTimedSerializer(app.config['SECRET_KEY'])
    return serializer.dumps(email, salt=app.config['SECURITY_PASSWORD_SALT'])

def confirm_token(token, expiration=3600):
    """Verifies the token. Expiration default is 1 hour."""
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
        # Test connection
        db.command('ping') 
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

    new_user = {
        "name": name, 
        "email": email, 
        "password": password,
        "isAdmin": email == ADMIN_EMAIL, 
        "isPremium": False, 
        "is_verified": False, 
        "session_id": str(uuid.uuid4()),
        "usage_counts": { "messages": 0, "webSearches": 0 },
        "last_usage_reset": datetime.utcnow().strftime('%Y-%m-%d'),
        "timestamp": datetime.utcnow().isoformat()
    }
    
    try:
        users_collection.insert_one(new_user)
        print(f"✅ LOG: User {email} inserted. Sending Brevo email...")
        
        # --- Send Verification Email via Brevo ---
        token = generate_token(email)
        confirm_url = url_for('confirm_email', token=token, _external=True)
        
        subject = "Verify your Sofia AI Account"
        html_body = f"""
        <h2>Welcome to Sofia AI, {name}!</h2>
        <p>Please click the link below to verify your email address:</p>
        <p><a href="{confirm_url}" style="background-color: #4CAF50; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px;">Verify Email</a></p>
        <p>Or copy this link: {confirm_url}</p>
        """
        
        # Send email in background thread to avoid blocking response
        Thread(target=send_async_email, args=(app, email, subject, html_body)).start()

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
        # Check verification status
        if not user_data.get('is_verified', False):
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
        return render_template('login.html', error_message="Link invalid or expired.")
    
    if not email:
         return render_template('login.html', error_message="Invalid link.")

    user = users_collection.find_one({"email": email})
    if not user:
        return render_template('login.html', error_message="User not found.")
    
    if not user.get("is_verified"):
        users_collection.update_one({"email": email}, {"$set": {"is_verified": True}})
        
    return redirect(url_for('login_page', verified='true'))


@app.route('/send_verification_email', methods=['POST'])
@login_required
def send_verification_email_route():
    if current_user.is_verified:
        return jsonify({'error': 'Email is already verified.'}), 400

    try:
        token = generate_token(current_user.email)
        confirm_url = url_for('confirm_email', token=token, _external=True)
        
        subject = "Resend: Verify your Sofia AI Account"
        html_body = f"<p>Hi {current_user.name},</p><p>Click here to verify:</p><a href='{confirm_url}'>Verify Email</a>"
        
        Thread(target=send_async_email, args=(app, current_user.email, subject, html_body)).start()
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

# --- Other APIs (Logout, Delete) ---

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
        return jsonify({'success': True, 'message': 'Logged out all devices.'})
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

@app.route('/status')
def status():
    return jsonify({'status': 'ok'})

# --- CHAT & LIBRARY APIs: (Paste your previous Logic Here) ---
# NOTE: The chat, chat history, and library upload routes from your previous version
# should be preserved. Since the prompt asked for an update to the authentication/email part,
# ensure you don't delete your `def chat():`, `def save_chat_history():`, etc.
# if they are present in your local file. 

# If this is a fresh file, you will need to add those functions back.
# Based on your previous uploads, they were quite long.
# I have ensured the top 200 lines (Auth/Email) are fully updated for Brevo.

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
