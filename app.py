# app.py - Free Fire Tournament Backend Application
# This file handles API endpoints for match management, user registrations,
# website content, and admin functionalities, interacting with Google Firestore.

# =====================================================================
# IMPORTS
# =====================================================================
import firebase_admin
from google.api_core.exceptions import Aborted
from firebase_admin import credentials, firestore, auth
from flask import Flask, request, jsonify, render_template, redirect, url_for, session, make_response
from datetime import datetime, timedelta, timezone # Used for time calculations and timestamps
from flask_cors import CORS # Required for handling Cross-Origin Resource Sharing
from dotenv import load_dotenv # For loading environment variables from .env file
from apscheduler.schedulers.background import BackgroundScheduler
import os
import traceback # For printing full tracebacks during debugging
import requests # For Telegram notifications
import json


# =====================================================================
# LOAD ENVIRONMENT VARIABLES
# =====================================================================
load_dotenv() # Loads variables from .env file into os.environ

# =====================================================================
# YOUR EXISTING CUSTOM IMPORTS HERE
# Please ensure all your specific imports (e.g., for Telegram bot, other utilities)
# are copied and pasted into this section from your original app.py.
# =====================================================================
# For example:
# import json # If you handle JSON manually
# ...


# =====================================================================
# FLASK APP CONFIGURATION
# =====================================================================
app = Flask(__name__, template_folder='templates') # Explicitly specify templates folder
# IMPORTANT: Replace 'YOUR_SUPER_SECRET_KEY' with a strong, random, and unique secret key.
# This is crucial for Flask session security. Generate a long, random string.
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'a_very_long_and_complex_random_string_for_dev_purposes_change_this_in_prod_really_change_it')
#CORS(app) # Enable CORS for all routes. Adjust origins/methods as needed for production.
CORS(app, resources={r"/api/*": {"origins": "https://www.thatournaments.xyz"}})
# =====================================================================

# Initialize scheduler early
scheduler = BackgroundScheduler()
scheduler.start()  # Or start conditionally later


# =====================================================================
# FIREBASE INITIALIZATION
# =====================================================================
# IMPORTANT: Configure the path to your Firebase service account key JSON file.
# This should be downloaded from Firebase Console -> Project settings -> Service accounts.

db = None

try:
    firebase_key = os.getenv("FIREBASE_SERVICE_ACCOUNT_KEY_JSON")

    if not firebase_key:
        raise ValueError("FIREBASE_SERVICE_ACCOUNT_KEY_JSON env variable missing!")

    print("🔐 Raw key loaded from environment, parsing JSON...")

    key_data = json.loads(firebase_key)
    key_data["private_key"] = key_data["private_key"].replace("\\n", "\n")
    print("✅ Private key formatting fixed")

    if not firebase_admin._apps:
        cred = credentials.Certificate(key_data)
        firebase_admin.initialize_app(cred)
        print("✅ Firebase Admin SDK initialized")

    db = firestore.client()

    # Test Firestore connection
    test_ref = db.collection("test_connection").document("probe")
    test_ref.set({"timestamp": firestore.SERVER_TIMESTAMP})
    test_ref.delete()
    print("🔥 Firestore connection test SUCCESS")

except Exception as e:
    print(f"🚨 Firebase initialization failed: {e}")


# =====================================================================

# Now this won't crash
if not scheduler.running:  # Line 84 (now safe)
    pass



# =====================================================================
# GLOBAL VARIABLES (for in-memory caching and ADMIN_UID)
# =====================================================================
# This dictionary caches match slot details loaded from Firestore.
available_slots = {}

# IMPORTANT: REPLACE 'e2vzNJEFhoVk0l1v4MtCp6OHHn03' with the actual UID of your
# Firebase user account that should have administrator privileges.
ADMIN_UID = os.getenv('ADMIN_UID', 'e2vzNJEFhoVk0l1v4MtCp6OHHn03') # Default value for development, CHANGE THIS.
print(f"Flask App: ADMIN_UID loaded from environment/default: {ADMIN_UID}")

# Telegram Bot Configuration
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', 'YOUR_TELEGRAM_BOT_TOKEN') # CHANGE THIS
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', 'YOUR_TELEGRAM_CHAT_ID') # CHANGE THIS

# Define IST timezone explicitly for consistency
IST_TIMEZONE = timezone(timedelta(hours=5, minutes=30))

# Add this flag 👇
startup_tasks_done = False  # New global flag

# =====================================================================
# HELPER FUNCTIONS
# =====================================================================

def is_admin(user_id):
    """Checks if the given user_id matches the configured ADMIN_UID."""
    if not ADMIN_UID or ADMIN_UID == 'YOUR_ADMIN_UID_HERE': # Check for unset placeholder as well
        print("WARNING: ADMIN_UID is empty or default. Admin functionality might be insecure or disabled.")
        return False
    return user_id == ADMIN_UID

def format_timestamp(timestamp_obj):
    if timestamp_obj is None:
        return "N/A"
    # ... rest of your code ...
    """
    Formats a Firestore Timestamp object or datetime object into a readable string (IST).
    Handles potential timezone differences and ensures a consistent display format.
    """
    if isinstance(timestamp_obj, datetime):
        # Ensure datetime object has timezone info before converting, default to UTC if naive
        if timestamp_obj.tzinfo is None:
            timestamp_obj = timestamp_obj.replace(tzinfo=timezone.utc)
        return timestamp_obj.astimezone(IST_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S')
    elif hasattr(timestamp_obj, 'to_datetime'): # For google.cloud.firestore.Timestamp objects
        return timestamp_obj.to_datetime().astimezone(IST_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S')
    return str(timestamp_obj) # Fallback for other types

def format_time_to_12hr_ist(time_24hr_str):
    """Converts a 'HH:MM' string to 'hh:mm AM/PM' format in IST."""
    try:
        # Create a dummy datetime object for today to parse the time
        dummy_date = datetime.now(IST_TIMEZONE).date()
        time_obj = datetime.strptime(time_24hr_str, '%H:%M').time()
        
        # Combine to a datetime object for formatting
        dt_obj = datetime.combine(dummy_date, time_obj)
        
        return dt_obj.strftime('%I:%M %p') # %I for 12-hour, %p for AM/PM
    except ValueError:
        print(f"Warning: Could not parse 24-hour time '{time_24hr_str}'.")
        return time_24hr_str # Return original if invalid format

def is_match_open_for_registration(match_time_str):
    """
    Determines if a match is open for registration based on its time (20 minutes before).
    Intelligently handles matches that have passed today by considering the next day.
    """
    try:
        now_ist = datetime.now(IST_TIMEZONE)

        # Parse match time string and create a datetime object for today in IST
        match_hour, match_minute = map(int, match_time_str.split(':'))
        match_datetime_ist = now_ist.replace(hour=match_hour, minute=match_minute, second=0, microsecond=0)

        # If the match time for today has already passed, consider it for the next day
        if match_datetime_ist < now_ist:
            match_datetime_ist += timedelta(days=1)

        # Registration closes 20 minutes before match time
        registration_close_time_ist = match_datetime_ist - timedelta(minutes=20)

        return now_ist < registration_close_time_ist
    except Exception as e:
        print(f"Error checking match registration status for time '{match_time_str}': {e}")
        traceback.print_exc()
        return False # Default to not open if there's an error parsing time

def is_match_completed_server_side(match_time_str):
    """
    Determines if a match is considered 'completed' server-side.
    Now considers date in addition to time.
    """
    try:
        now_ist = datetime.now(IST_TIMEZONE)
        
        # Create datetime object for the match (today at match time)
        match_hour, match_minute = map(int, match_time_str.split(':'))
        match_datetime_ist = now_ist.replace(
            hour=match_hour, 
            minute=match_minute, 
            second=0, 
            microsecond=0
        )
        
        # If match time is in the future today, not completed
        if match_datetime_ist > now_ist:
            return False
        
        # If current time is more than 1 hour past match time, completed
        completion_time_ist = match_datetime_ist + timedelta(hours=1)
        return now_ist >= completion_time_ist
        
    except Exception as e:
        print(f"Error checking match completion: {e}")
        traceback.print_exc()
        return False

def send_telegram_message(message, parse_mode="Markdown"):
    """Sends a message to the configured Telegram chat."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID or TELEGRAM_BOT_TOKEN == 'YOUR_TELEGRAM_BOT_TOKEN' or TELEGRAM_CHAT_ID == 'YOUR_TELEGRAM_CHAT_ID':
        print("Telegram bot token or chat ID not configured or using default placeholders. Skipping Telegram message.")
        return False

    telegram_api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    telegram_payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": parse_mode
    }
    try:
        response = requests.post(telegram_api_url, json=telegram_payload)
        response.raise_for_status() # Raise an exception for HTTP errors
        print("Telegram message sent successfully.")
    except requests.exceptions.RequestException as e:
        print(f"Error sending Telegram message: {e}")
        traceback.print_exc()
        return False
    return True


# ... (existing helper functions)

def mark_completed_matches():
    """Automatically mark completed matches in the database."""
    try:
        print("🔍 Marking completed matches...")
        now_ist = datetime.now(IST_TIMEZONE)
        registrations_ref = db.collection('registrations').where('status', '==', 'registered').get()
        
        for doc in registrations_ref:
            data = doc.to_dict()
            match_time = data.get('matchTime')
            if match_time and is_match_completed_server_side(match_time):
                doc.reference.update({'status': 'completed'})
                print(f"  Marked registration {doc.id} as completed")
                
        print("✅ Completed matches marked")
    except Exception as e:
        print(f"❌ Error marking completed matches: {e}")
        traceback.print_exc()

def run_startup_tasks():
    """Runs critical initialization tasks at app startup."""
    print("🚀 Running startup tasks...")
    mark_completed_matches()
    initialize_booked_slots_from_firestore_on_startup()
    print("✅ Startup tasks completed")


# --- In-memory Tournament Slot Management Functions (for booking logic) ---
# These assume `available_slots` is initialized by `initialize_booked_slots_from_firestore_on_startup()`
# and updated by admin actions.

def get_next_available_slot(match_id):
    """Finds smallest available slot number with date awareness"""
    if match_id not in available_slots:
        print(f"Error: Match ID '{match_id}' not found")
        return None

    slot_info = available_slots[match_id]
    current_booked = slot_info.get('booked_slots', [])
    total_allowed = slot_info['max_players']

    # Find first available slot
    for slot_num in range(1, total_allowed + 1):
        if slot_num not in current_booked:
            return slot_num
    return None  # No slots available

def book_slot_in_memory(match_id, slot_number):
    """Marks a slot as booked in the in-memory `available_slots` dictionary."""
    if match_id in available_slots:
        if 'booked_slots' not in available_slots[match_id]:
            available_slots[match_id]['booked_slots'] = [] # Initialize if not present
        
        if slot_number not in available_slots[match_id]['booked_slots']:
            available_slots[match_id]['booked_slots'].append(slot_number)
            available_slots[match_id]['booked_slots'].sort() # Keep sorted
            print(f"Booked slot {slot_number} for {match_id}. Current booked: {available_slots[match_id]['booked_slots']}")
            return True
    print(f"Failed to book slot {slot_number} for {match_id}. Either match_id not found or slot already booked.")
    return False

def release_slot_in_memory(match_id, slot_number):
    """Releases a slot from the in-memory `available_slots` dictionary."""
    if match_id in available_slots and 'booked_slots' in available_slots[match_id]:
        if slot_number in available_slots[match_id]['booked_slots']:
            available_slots[match_id]['booked_slots'].remove(slot_number)
            print(f"Released slot {slot_number} for {match_id}. Current booked: {available_slots[match_id]['booked_slots']}")
            return True
    print(f"Failed to release slot {slot_number} for {match_id}. Match_id not found or slot not booked.")
    return False


# Function to initialize in-memory 'available_slots' from Firestore on app startup

slots_initialized = False

@app.before_request
def initialize_slots_if_needed():
    global slots_initialized
    if not slots_initialized:
        initialize_booked_slots_from_firestore_on_startup()
        slots_initialized = True


def initialize_booked_slots_from_firestore_on_startup():
    """
    Loads all active match slots from Firestore into the global 'available_slots' dictionary.
    Also calculates initial 'booked_slots' count by querying registrations.
    """
    global available_slots
    print("\n--- Initializing in-memory match slots from Firestore ---")
    try:
        slots_ref = db.collection('match_slots').where('active', '==', True)
        docs = slots_ref.stream()

        available_slots.clear() # Clear existing slots to refresh

        for doc in docs:
            slot_data = doc.to_dict()
            if 'id' not in slot_data:
                slot_data['id'] = doc.id
            
            # Initialize booked_slots for each match
            slot_data['booked_slots'] = [] 
            
            available_slots[slot_data['id']] = slot_data
            # print(f"  Loaded slot config: {slot_data.get('id', doc.id)} ({slot_data.get('type')})")

        # Now, populate the 'booked_slots' array by querying registrations
        # This is a critical step to ensure memory state reflects actual bookings.
        print("  Populating booked_slots from existing registrations...")
        all_registrations_docs = db.collection('registrations').where('status', '==', 'registered').get() # Only active registrations
        
        for reg_doc in all_registrations_docs:
            reg_data = reg_doc.to_dict()
            match_id = reg_data.get('matchId')
            slot_number = reg_data.get('slotNumber')
            
            if match_id in available_slots and slot_number is not None:
                # Ensure slot_number is an integer if it's stored as string/float
                try:
                    slot_number = int(slot_number) 
                except (ValueError, TypeError):
                    print(f"Warning: Invalid slotNumber '{slot_number}' for registration {reg_doc.id}. Skipping.")
                    continue

                if slot_number not in available_slots[match_id]['booked_slots']:
                    available_slots[match_id]['booked_slots'].append(slot_number)
                    # print(f"    Added booking for {match_id}, Slot: {slot_number}")
            else:
                print(f"    Warning: Registration {reg_doc.id} has invalid matchId/slotNumber or matchId not in config. Skipping booking sync.")

        # Sort all booked_slots lists
        for match_id in available_slots:
            available_slots[match_id]['booked_slots'].sort()
            print(f"  {match_id} initialized with {len(available_slots[match_id]['booked_slots'])} booked slots.")

        print(f"--- In-memory match slots initialized. Total: {len(available_slots)} slots loaded. ---")

    except Exception as e:
        print(f"FATAL ERROR: Error initializing booked slots from Firestore: {e}")
        traceback.print_exc()
        print("In-memory slot management might be inconsistent. Please check Firestore connection and data structure.")


# =====================================================================
# YOUR EXISTING CUSTOM HELPER FUNCTIONS HERE
# =====================================================================
# Any other helper functions you have, copy them here.
def process_wallet_payment(user_id, amount, description, match_id=""):
    """Deducts amount from wallet with transaction support"""
    try:
        wallet_ref = db.collection('wallets').document(user_id)
        transaction = db.transaction()
        
        @firestore.transactional
        def update_in_transaction(transaction, wallet_ref):
            wallet_doc = wallet_ref.get(transaction=transaction)
            if not wallet_doc.exists:
                return False, "Wallet not found"
            
            current_balance = wallet_doc.to_dict().get('balance', 0)
            if current_balance < amount:
                return False, "Insufficient balance"
            
            new_balance = current_balance - amount
            transaction.update(wallet_ref, {'balance': new_balance})
            
            # Record transaction
            transaction_data = {
                "userId": user_id,
                "amount": -amount,
                "description": description,
                "status": "success",
                "type": "tournament_registration",
                "timestamp": firestore.SERVER_TIMESTAMP,
                "matchId": match_id
            }
            db.collection('transactions').add(transaction_data)
            
            return True, new_balance
        
        return update_in_transaction(transaction, wallet_ref)
    except Aborted:
        return False, "Transaction aborted, please retry"
    except Exception as e:
        return False, str(e)




# =====================================================================
# ADD THIS NEW BEFORE_REQUEST HANDLER 👇
# =====================================================================
@app.before_request
def run_startup_tasks_once():
    global startup_tasks_done
    if not startup_tasks_done:
        run_startup_tasks()
        startup_tasks_done = True
        print("✅ Startup tasks executed successfully")

# =====================================================================
# FLASK ROUTES - Frontend Page Renderers
# These routes simply serve the HTML files for your frontend.
# =====================================================================
@app.route('/')
def ping():
    return "✅ Tournament API is live."
    
#@app.route('/')
#def index():
    """Renders the main tournament page (index.html)."""
    #return render_template('index.html')
    #return "✅ Root route test working!"

#@app.route('/admin_panel.html')
#def admin_panel_page():
    """Renders the admin panel page (admin_panel.html)."""
   #return render_template('admin_panel.html')

#@app.route('/registered.html')
#def registered_page():
    """Renders the user's registered matches page (registered.html)."""
    #return render_template('registered.html')

# =====================================================================
# YOUR EXISTING CUSTOM FLASK ROUTES (Frontend or other API) HERE
# =====================================================================
# For example:
# @app.route('/leaderboard')
# def leaderboard():
#     return render_template('leaderboard.html')

#@app.before_first_request
#def initialize():
 #   """Run initialization tasks before first request."""
  #  run_startup_tasks()


# =====================================================================
# API ENDPOINTS - Public Facing (Read-only or User Actions)
# These endpoints are generally consumed by the public-facing 'index.html'
# and 'registered.html' pages.
# =====================================================================

@app.route('/api/match_slots', methods=['GET'])
def get_match_slots_api():
    """
    API endpoint to get all active match slots for display on index.html.
    Filters out inactive or past matches on the server-side.
    Now includes 12-hour formatted time and `targetTimeMillis` for countdown.
    """
    try:
        match_slots_list = []
        docs = db.collection('match_slots').stream()
        
        now_ist = datetime.now(IST_TIMEZONE)

        for doc in docs:
            slot_data = doc.to_dict()
            if 'id' not in slot_data:
                slot_data['id'] = doc.id
            
            match_time_24hr = slot_data.get('time')
            if not match_time_24hr:
                print(f"Warning: Match slot {slot_data.get('id')} missing 'time' field. Skipping.")
                continue

            # Add 12-hour format for display
            slot_data['time12hr'] = format_time_to_12hr_ist(match_time_24hr)
            
            # Calculate target epoch milliseconds for countdown
            match_hour, match_minute = map(int, match_time_24hr.split(':'))
            match_datetime_ist = now_ist.replace(hour=match_hour, minute=match_minute, second=0, microsecond=0)

            # Adjust to next day if match time has already passed for today
            if match_datetime_ist < now_ist:
                match_datetime_ist += timedelta(days=1)
            
            # Convert to Unix epoch milliseconds (important for JS countdown)
            # Ensure it's timezone-aware before getting timestamp, then convert to milliseconds
            slot_data['targetTimeMillis'] = int(match_datetime_ist.timestamp() * 1000)

            # Filter for active and upcoming matches for public display
            if slot_data.get('active', False) and is_match_open_for_registration(match_time_24hr):
                match_slots_list.append(slot_data)
            
        match_slots_list.sort(key=lambda x: x.get('time', '')) # Sort by 24hr time for consistent order

        print(f"API: Serving {len(match_slots_list)} active match slots with countdown data to frontend.")
        return jsonify({"success": True, "matchSlots": match_slots_list}), 200
    except Exception as e:
        print(f"Error fetching match slots for public API: {e}")
        traceback.print_exc()
        return jsonify({"success": False, "message": f"Server error fetching match slots: {e}"}), 500


@app.route('/api/schedule_items', methods=['GET'])
def get_schedule_items_api():
    """API endpoint to get all daily schedule items."""
    try:
        schedule_items_list = []
        docs = db.collection('schedule_items').stream()
        for doc in docs:
            item_data = doc.to_dict()
            item_data['id'] = doc.id
            
            # Format time for display if available
            if 'time' in item_data:
                item_data['time12hr'] = format_time_to_12hr_ist(item_data['time'])

            schedule_items_list.append(item_data)

        schedule_items_list.sort(key=lambda x: x.get('order', 0))

        print(f"API: Serving {len(schedule_items_list)} schedule items.")
        return jsonify({"success": True, "scheduleItems": schedule_items_list}), 200
    except Exception as e:
        print(f"Error fetching schedule items for API: {e}")
        traceback.print_exc()
        return jsonify({"success": False, "message": f"Server error fetching schedule items: {e}"}), 500


@app.route('/api/prize_items', methods=['GET'])
def get_prize_items_api():
    """API endpoint to get all prize distribution items."""
    try:
        prize_items_list = []
        docs = db.collection('prize_items').stream()
        for doc in docs:
            item_data = doc.to_dict()
            item_data['id'] = doc.id
            prize_items_list.append(item_data)

        prize_items_list.sort(key=lambda x: x.get('order', 0))

        print(f"API: Serving {len(prize_items_list)} prize items.")
        return jsonify({"success": True, "prizeItems": prize_items_list}), 200
    except Exception as e:
        print(f"Error fetching prize items for API: {e}")
        traceback.print_exc()
        return jsonify({"success": False, "message": f"Server error fetching prize items: {e}"}), 500

@app.route('/api/configs/website_content', methods=['GET'])
def get_website_content_api():
    print("[INFO] /api/configs/website_content was hit.")
    try:
        doc_ref = db.collection('configs').document('website_content')
        doc = doc_ref.get()
        if doc.exists:
            content = doc.to_dict()
            print("[INFO] Website content loaded:", content)
            return jsonify({"success": True, "content": content}), 200
        else:
            print("[WARNING] website_content doc does not exist")
            return jsonify({"success": False, "message": "Content missing"}), 404
    except Exception as e:
        print("[ERROR] Error in website_content API:", e)
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "message": "Internal error"}), 500

@app.route('/api/register_tournament', methods=['POST'])
def register_tournament():
    """
    Handles new tournament registrations from users.
    Registers a user for a specific match slot, saves to Firestore, and sends Telegram message.
    """
    try:
        registration_data = request.json
        # Extract payment method
        payment_method = registration_data.get('paymentMethod', 'wallet')  # Default to wallet
        if not registration_data:
            return jsonify({"success": False, "message": "No registration data provided"}), 400

        # Extract all fields at once
        user_id = registration_data.get('userId')
        email = registration_data.get('email')
        match_id = registration_data.get('matchId')
        match_type = registration_data.get('matchType')
        match_time = registration_data.get('matchTime')
        igl_ign = registration_data.get('iglIGN')
        igl_ffid = registration_data.get('iglFFID')
        teammates = registration_data.get('teammates', [])
        client_time = registration_data.get('clientTime')

        # Validate required fields
        if not all([user_id, email, match_id, match_type, match_time, igl_ign, igl_ffid]):
            return jsonify({"success": False, "message": "Missing required registration data. Please provide all necessary fields."}), 400

       

        
         
        
        # Check registration window first (before Firestore operations)
        if not is_match_open_for_registration(match_time):
            return jsonify({"success": False, "message": f"Registration for {match_type} at {match_time} is closed."}), 400

        # Fetch match slot details from Firestore
        match_slot_ref = db.collection('match_slots').document(match_id)
        match_slot_doc = match_slot_ref.get()
        
        if not match_slot_doc.exists:
            return jsonify({"success": False, "message": "Invalid match selected or match not found."}), 400
            
        selected_match_slot = match_slot_doc.to_dict()

        # Check if match is active
        if not selected_match_slot.get('active', True):
            return jsonify({"success": False, "message": f"Registration for {match_type} is currently not active."}), 400

        # Check for existing registration
        existing_registrations = db.collection('registrations') \
            .where('userId', '==', user_id) \
            .where('matchId', '==', match_id) \
            .where('status', '==', 'registered') \
            .get()
            
        if existing_registrations:
            return jsonify({"success": False, "message": "You are already registered for this match. Please check your registrations."}), 400

        # Check capacity
        current_active_count = len(db.collection('registrations')
            .where('matchId', '==', match_id)
            .where('status', '==', 'registered')
            .get())
            
        if current_active_count >= selected_match_slot['max_players']:
            return jsonify({"success": False, "message": f"Sorry, all slots for {match_type} at {match_time} are full!"}), 400

        # Get next available slot
        slot_number = get_next_available_slot(match_id)
        if slot_number is None:
            return jsonify({"success": False, "message": f"No available slots for {match_type} due to a system error"}), 500

        # Prepare registration data
        registration_to_save = {
            "userId": user_id,
            "email": email,
            "matchId": match_id,
            "matchType": match_type,
            "matchTime": match_time,
            "iglIGN": igl_ign,
            "iglFFID": igl_ffid,
            "teammates": teammates,
            "slotNumber": slot_number,
            "timestamp": firestore.SERVER_TIMESTAMP,
            "clientTime": client_time,
            "status": "registered",
            "autoDeleteOnCompletion": True,
            "roomCode": "",
            "roomPassword": ""
        }

        # Save to Firestore
        doc_ref = db.collection('registrations').add(registration_to_save)
        registration_doc_id = doc_ref[1].id

        # Create Telegram message
        telegram_message = f"""*New Free Fire Tournament Registration!*
*Status:* Registered
*User ID:* `{user_id}`
*Email:* `{email}`
*Match ID:* `{match_id}`
*Match Type:* `{match_type}`
*Match Time:* `{match_time}`
*Slot Number:* `{slot_number}`
*Firestore Doc ID:* `{registration_doc_id}`
*Client Time:* {client_time}
"""
        if teammates:
            telegram_message += "\n*Teammates:*\n"
            for i, teammate in enumerate(teammates):
                telegram_message += f"  {i+1}. IGN: `{teammate.get('ign', 'N/A')}`, FFID: `{teammate.get('ffid', 'N/A')}`\n"

        send_telegram_message(telegram_message)

        return jsonify({
            "success": True,
            "message": "Registration successful!",
            "registrationDocId": registration_doc_id,
            "slotNumber": slot_number
        }), 200

    except Exception as e:
        # Handle errors and refund if needed
        return jsonify({"success": False, "message": f"Payment processing failed: {str(e)}"}), 500
        
        error_msg = f"Registration error: {str(e)}"
        print(error_msg)
        traceback.print_exc()
        
        # Release slot if it was assigned
        if 'slot_number' in locals() and 'match_id' in locals():
            release_slot_in_memory(match_id, slot_number)
            print(f"Released slot {slot_number} due to error")
            
        return jsonify({
            "success": False,
            "message": "Internal server error during registration",
            "error": error_msg
        }), 500

@app.route('/api/get_registrations', methods=['GET'])
def get_registrations():
    user_id = request.args.get('userId')
    if not user_id:
        return jsonify({"success": False, "message": "User ID is required to fetch registrations."}), 400

    try:
        registrations_ref = db.collection('registrations')\
                              .where('userId', '==', user_id)\
                              .order_by('timestamp', direction=firestore.Query.DESCENDING)\
                              .get()

        registrations_list = []
        for doc in registrations_ref:
            data = doc.to_dict()
            data['id'] = doc.id

            # Safe timestamp formatting
            try:
                data['timestamp'] = format_timestamp(data.get('timestamp'))
            except:
                data['timestamp'] = 'Invalid timestamp'

            # Safe match completion check
            try:
                data['isCompleted'] = is_match_completed_server_side(data.get('matchTime', ''))
            except:
                data['isCompleted'] = False

            data['roomCode'] = data.get('roomCode', '')
            data['roomPassword'] = data.get('roomPassword', '')

            # Match time formatting
            match_time = data.get('matchTime')
            if match_time:
                try:
                    data['matchTime12hr'] = format_time_to_12hr_ist(match_time)
                except:
                    data['matchTime12hr'] = 'N/A'
            else:
                data['matchTime12hr'] = 'N/A'

            registrations_list.append(data)

        return jsonify({"success": True, "registrations": registrations_list}), 200

    except Exception as e:
        print(f"Error fetching user registrations: {e}")
        traceback.print_exc()
        return jsonify({"success": False, "message": f"Failed to fetch registrations: {str(e)}"}), 500


@app.route('/api/get_match_participants', methods=['GET'])
def get_match_participants():
    """
    Fetches participants (IGN, FFID) for a specific match ID.
    Accessible to any logged-in user to see their lobby.
    """
    match_id = request.args.get('matchId')
    if not match_id:
        return jsonify({"success": False, "message": "Match ID is required to fetch participants."}), 400

    try:
        participants_ref = db.collection('registrations').where('matchId', '==', match_id).where('status', '==', 'registered').get()
        
        participants_list = []
        for doc in participants_ref:
            data = doc.to_dict()
            participant = {
                "iglIGN": data.get('iglIGN', 'N/A'),
                "iglFFID": data.get('iglFFID', 'N/A'),
                "slotNumber": data.get('slotNumber', 'N/A'),
                "teammates": []
            }
            if data.get('teammates'):
                for teammate in data['teammates']:
                    participant['teammates'].append({
                        "ign": teammate.get('ign', 'N/A'),
                        "ffid": teammate.get('ffid', 'N/A')
                    })
            participants_list.append(participant)
        
        participants_list.sort(key=lambda x: x.get('slotNumber', float('inf'))) # Sort by slot number

        return jsonify({"success": True, "participants": participants_list}), 200

    except Exception as e:
        print(f"Error fetching match participants: {e}")
        traceback.print_exc()
        return jsonify({"success": False, "message": f"Failed to fetch match participants: {str(e)}"}), 500


@app.route('/api/update_registration_status', methods=['POST'])
def update_registration_status():
    """Updates the status (e.g., 'canceled') of a registration and manages slots."""
    try:
        data = request.json
        registration_id = data.get('registrationId')
        user_id = data.get('userId') # User who initiated the action (could be admin or the user themselves)
        new_status = data.get('status')
        admin_user_id_from_request = data.get('adminUserId') # Present if request came from admin panel

        if not all([registration_id, user_id, new_status]):
            return jsonify({"success": False, "message": "Missing registration ID, user ID, or new status."}), 400

        registration_doc_ref = db.collection('registrations').document(registration_id)
        registration_doc = registration_doc_ref.get()

        if not registration_doc.exists:
            return jsonify({"success": False, "message": "Registration not found."}), 404
            
        current_data = registration_doc.to_dict()
        
        # Authorization check: either the request user is admin, or it's the registered user themselves
        if not (is_admin(admin_user_id_from_request) or current_data.get('userId') == user_id):
            return jsonify({"success": False, "message": "Unauthorized: You can only modify your own registrations or require admin privileges."}), 403
            
        if current_data.get('status') == 'canceled' and new_status == 'canceled':
            return jsonify({"success": False, "message": "This registration is already canceled."}), 400

        registration_doc_ref.update({"status": new_status})

        if new_status == 'canceled':
            match_id = current_data.get('matchId')
            slot_number = current_data.get('slotNumber')
            if match_id and slot_number:
                release_slot_in_memory(match_id, slot_number) # Release slot if canceled
                print(f"Slot {slot_number} for {match_id} released due to cancellation.")
                
            telegram_message = f"""*Free Fire Tournament Registration Canceled!*
*User ID:* `{user_id}`
*Registration ID:* `{registration_id}`
*Match Type:* `{current_data.get('matchType')}`
*Match ID:* `{match_id}`
*Slot Number:* `{slot_number}`
*Canceled At:* `{datetime.now(IST_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S')}`
"""
            send_telegram_message(telegram_message)

        return jsonify({"success": True, "message": f"Registration status updated to '{new_status}' successfully."}), 200

    except Exception as e:
        print(f"Error updating registration status: {e}")
        traceback.print_exc()
        return jsonify({"success": False, "message": f"An internal server error occurred while updating registration status: {str(e)}"}), 500

@app.route('/api/update_auto_delete_preference', methods=['POST'])
def update_auto_delete_preference():
    """Updates the autoDeleteOnCompletion preference for a registration."""
    try:
        data = request.json
        registration_id = data.get('registrationId')
        user_id = data.get('userId')
        auto_delete = data.get('autoDelete') # boolean

        if not all([registration_id, user_id, auto_delete is not None]):
            return jsonify({"success": False, "message": "Missing registration ID, user ID, or autoDelete preference."}), 400

        registration_doc_ref = db.collection('registrations').document(registration_id)
        registration_doc = registration_doc_ref.get()

        if not registration_doc.exists:
            return jsonify({"success": False, "message": "Registration not found."}), 404
            
        current_data = registration_doc.to_dict()
        if current_data.get('userId') != user_id:
            return jsonify({"success": False, "message": "Unauthorized: You can only modify your own registrations."}), 403

        registration_doc_ref.update({"autoDeleteOnCompletion": auto_delete})
        return jsonify({"success": True, "message": "Auto-delete preference updated successfully."}), 200
    except Exception as e:
        print(f"Error updating auto-delete preference: {e}")
        traceback.print_exc()
        return jsonify({"success": False, "message": f"An error occurred while updating preference: {str(e)}"}), 500

@app.route('/api/delete_registration', methods=['POST'])
def delete_registration():
    """Allows a user or admin to manually delete a registration from Firestore and releases the slot."""
    try:
        data = request.json
        registration_id = data.get('registrationId')
        user_id = data.get('userId') # The user attempting the deletion
        admin_user_id_from_request = data.get('adminUserId') # Only if from admin panel

        if not registration_id or not user_id:
            return jsonify({"success": False, "message": "Registration ID and User ID are required for deletion."}), 400

        registration_doc_ref = db.collection('registrations').document(registration_id)
        registration_doc = registration_doc_ref.get()

        if not registration_doc.exists:
            return jsonify({"success": False, "message": "Registration not found."}), 404

        registration_data = registration_doc.to_dict()
        
        # Authorization check: must be admin OR the actual user who registered
        if not (is_admin(admin_user_id_from_request) or registration_data.get('userId') == user_id):
            return jsonify({"success": False, "message": "Unauthorized deletion attempt."}), 403
            
        match_id = registration_data.get('matchId')
        slot_number = registration_data.get('slotNumber')
        
        # Release slot only if it was not already canceled (to prevent double-release)
        if match_id and slot_number and registration_data.get('status') != 'canceled':
            release_slot_in_memory(match_id, slot_number)
            print(f"Slot {slot_number} for {match_id} released due to manual deletion.")

        registration_doc_ref.delete()

        telegram_message = f"""*Free Fire Tournament Registration Manually Deleted!*
*User ID:* `{user_id}`
*Registration ID:* `{registration_id}`
*Match Type:* `{registration_data.get('matchType')}`
*Match ID:* `{match_id}`
*Slot Number:* `{slot_number}` (Released: {'Yes' if match_id and slot_number and registration_data.get('status') != 'canceled' else 'No'})
*Deleted At:* `{datetime.now(IST_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S')}`
"""
        send_telegram_message(telegram_message)

        return jsonify({"success": True, "message": "Registration deleted successfully."}), 200

    except Exception as e:
        print(f"Error deleting registration: {e}")
        traceback.print_exc()
        return jsonify({"success": False, "message": f"An error occurred during deletion: {str(e)}"}), 500


# --- Admin API Routes (Requires ADMIN_UID authorization) ---

@app.route('/api/admin/create_firebase_user', methods=['POST'])
def create_firebase_user_api_admin():
    """Admin: Creates a new user in Firebase Authentication."""
    data = request.json
    admin_user_id = data.get('adminUserId')
    email = data.get('email')
    password = data.get('password')

    if not is_admin(admin_user_id):
        return jsonify({"success": False, "message": "Unauthorized: Admin privileges required."}), 403
    if not email or not password:
        return jsonify({"success": False, "message": "Email and password are required."}), 400
    
    try:
        user = auth.create_user(email=email, password=password)
        telegram_message = f"""*Admin Action: New Firebase User Created!*
*Admin UID:* `{admin_user_id}`
*New User Email:* `{email}`
*New User UID:* `{user.uid}`
*Time:* `{datetime.now(IST_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S')}`
"""
        send_telegram_message(telegram_message)
        return jsonify({"success": True, "message": f"User {email} created successfully. UID: {user.uid}"}), 200
    except Exception as e:
        error_message = str(e)
        if "EMAIL_EXISTS" in error_message:
            error_message = "Email already exists."
        elif "WEAK_PASSWORD" in error_message:
            error_message = "Password is too weak. Must be at least 6 characters."
        print(f"Error creating user: {e}")
        traceback.print_exc()
        return jsonify({"success": False, "message": f"Failed to create user: {error_message}"}), 500

@app.route('/api/admin/delete_firebase_user', methods=['POST'])
def delete_firebase_user_api_admin():
    """Admin: Deletes a user from Firebase Authentication by UID or email."""
    data = request.json
    admin_user_id = data.get('adminUserId')
    target_uid = data.get('uid')
    target_email = data.get('email')

    if not is_admin(admin_user_id):
        return jsonify({"success": False, "message": "Unauthorized: Admin privileges required."}), 403
    if not target_uid and not target_email:
        return jsonify({"success": False, "message": "User UID or email is required for deletion."}), 400

    try:
        if target_uid:
            auth.delete_user(target_uid)
            telegram_message = f"""*Admin Action: Firebase User Deleted!*
*Admin UID:* `{admin_user_id}`
*Deleted User UID:* `{target_uid}`
*Time:* `{datetime.now(IST_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S')}`
"""
            send_telegram_message(telegram_message)
            return jsonify({"success": True, "message": f"User with UID {target_uid} deleted successfully."}), 200
        elif target_email:
            user = auth.get_user_by_email(target_email) # Get UID from email
            auth.delete_user(user.uid)
            telegram_message = f"""*Admin Action: Firebase User Deleted!*
*Admin UID:* `{admin_user_id}`
*Deleted User Email:* `{target_email}`
*Deleted User UID:* `{user.uid}`
*Time:* `{datetime.now(IST_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S')}`
"""
            send_telegram_message(telegram_message)
            return jsonify({"success": True, "message": f"User {target_email} deleted successfully."}), 200
    except auth.UserNotFoundError:
        return jsonify({"success": False, "message": "User not found."}), 404
    except Exception as e:
        print(f"Error deleting user: {e}")
        traceback.print_exc()
        return jsonify({"success": False, "message": f"Failed to delete user: {str(e)}"}), 500

@app.route('/api/admin/update_firebase_user_password', methods=['POST'])
def update_firebase_user_password_api_admin():
    """Admin: Updates a user's password in Firebase Authentication."""
    data = request.json
    admin_user_id = data.get('adminUserId')
    target_uid = data.get('uid')
    target_email = data.get('email')
    new_password = data.get('newPassword')

    if not is_admin(admin_user_id):
        return jsonify({"success": False, "message": "Unauthorized: Admin privileges required."}), 403
    if not new_password or (not target_uid and not target_email):
        return jsonify({"success": False, "message": "User UID/email and new password are required."}), 400
        
    try:
        user_to_update_uid = target_uid
        if target_email and not target_uid: # If only email is provided, get UID by email
            user = auth.get_user_by_email(target_email)
            user_to_update_uid = user.uid

        auth.update_user(user_to_update_uid, password=new_password)
        telegram_message = f"""*Admin Action: Firebase User Password Updated!*
*Admin UID:* `{admin_user_id}`
*Target User UID:* `{user_to_update_uid}`
*New Password Set (Do not log actual password):* `**********`
*Time:* `{datetime.now(IST_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S')}`
"""
        send_telegram_message(telegram_message)
        return jsonify({"success": True, "message": "User password updated successfully."}), 200
    except auth.UserNotFoundError:
        return jsonify({"success": False, "message": "User not found."}), 404
    except Exception as e:
        print(f"Error updating password: {e}")
        traceback.print_exc()
        return jsonify({"success": False, "message": f"Failed to update password: {str(e)}"}), 500

@app.route('/api/admin/configs/update_website_content', methods=['POST'])
def update_website_content_api_admin():
    """Admin API to update static website content (rules, contact info)."""
    try:
        data = request.json
        content = data.get('content')
        admin_user_id = data.get('adminUserId')

        if not is_admin(admin_user_id):
            return jsonify({"success": False, "message": "Unauthorized: Admin privileges required."}), 403
        if not content:
            return jsonify({"success": False, "message": "Content data is missing."}), 400

        doc_ref = db.collection('configs').document('website_content')
        doc_ref.set(content, merge=True) # Use merge=True to update existing fields or add new ones
        print(f"Admin {admin_user_id} updated website content.")
        return jsonify({"success": True, "message": "Website content updated successfully."}), 200
    except Exception as e:
        print(f"Error updating website content (Admin API): {e}")
        traceback.print_exc()
        return jsonify({"success": False, "message": f"Server error updating website content: {e}"}), 500

@app.route('/api/admin/match_slots', methods=['POST'])
def manage_match_slots_api_admin():
    """Admin API to add, update, or delete match slots."""
    try:
        data = request.json
        action = data.get('action') # 'add', 'update', 'delete'
        slot_id = data.get('id')
        slot_data = data.get('data') # For 'add' or 'update'
        admin_user_id = data.get('adminUserId')

        if not is_admin(admin_user_id):
            return jsonify({"success": False, "message": "Unauthorized: Admin privileges required."}), 403
        if not slot_id:
            return jsonify({"success": False, "message": "Match Slot ID is required."}), 400

        doc_ref = db.collection('match_slots').document(slot_id)

        if action == 'add':
            if not slot_data: return jsonify({"success": False, "message": "Slot data is missing for add action."}), 400
            doc_ref.set(slot_data)
            print(f"Admin {admin_user_id} added match slot: {slot_id}")
            initialize_booked_slots_from_firestore_on_startup() # Refresh in-memory slots
            return jsonify({"success": True, "message": f"Match slot '{slot_id}' added successfully."}), 200
        elif action == 'update':
            if not slot_data: return jsonify({"success": False, "message": "Slot data is missing for update action."}), 400
            doc_ref.update(slot_data)
            print(f"Admin {admin_user_id} updated match slot: {slot_id}")
            initialize_booked_slots_from_firestore_on_startup() # Refresh in-memory slots
            return jsonify({"success": True, "message": f"Match slot '{slot_id}' updated successfully."}), 200
        elif action == 'delete':
            doc_ref.delete()
            print(f"Admin {admin_user_id} deleted match slot: {slot_id}")
            initialize_booked_slots_from_firestore_on_startup() # Refresh in-memory slots
            return jsonify({"success": True, "message": f"Match slot '{slot_id}' deleted successfully."}), 200
        else:
            return jsonify({"success": False, "message": "Invalid action specified for match slots."}), 400
    except Exception as e:
        print(f"Error managing match slots (Admin API): {e}")
        traceback.print_exc()
        return jsonify({"success": False, "message": f"Server error managing match slots: {e}"}), 500

@app.route('/api/admin/schedule_items', methods=['POST'])
def manage_schedule_items_api_admin():
    """Admin API to add, update, or delete daily schedule items."""
    try:
        data = request.json
        action = data.get('action')
        item_id = data.get('id')
        item_data = data.get('data')
        admin_user_id = data.get('adminUserId')

        if not is_admin(admin_user_id):
            return jsonify({"success": False, "message": "Unauthorized: Admin privileges required."}), 403

        collection_ref = db.collection('schedule_items')

        if action == 'add':
            if not item_data: return jsonify({"success": False, "message": "Schedule item data missing for add."}), 400
            new_doc_ref = collection_ref.add(item_data)[1] # .add() returns tuple (timestamp, DocumentReference)
            print(f"Admin {admin_user_id} added schedule item: {new_doc_ref.id}")
            return jsonify({"success": True, "message": f"Schedule item added successfully with ID: {new_doc_ref.id}"}), 200
        elif action == 'update':
            if not item_id or not item_data: return jsonify({"success": False, "message": "Item ID or data missing for update."}), 400
            doc_ref = collection_ref.document(item_id)
            doc_ref.update(item_data)
            print(f"Admin {admin_user_id} updated schedule item: {item_id}")
            return jsonify({"success": True, "message": f"Schedule item '{item_id}' updated successfully."}), 200
        elif action == 'delete':
            if not item_id: return jsonify({"success": False, "message": "Item ID missing for delete."}), 400
            collection_ref.document(item_id).delete()
            print(f"Admin {admin_user_id} deleted schedule item: {item_id}")
            return jsonify({"success": True, "message": f"Schedule item '{item_id}' deleted successfully."}), 200
        else:
            return jsonify({"success": False, "message": "Invalid action specified for schedule items."}), 400
    except Exception as e:
        print(f"Error managing schedule items (Admin API): {e}")
        traceback.print_exc()
        return jsonify({"success": False, "message": f"Server error managing schedule items: {e}"}), 500

@app.route('/api/admin/prize_items', methods=['POST'])
def manage_prize_items_api_admin():
    """Admin API to add, update, or delete prize distribution items."""
    try:
        data = request.json
        action = data.get('action')
        item_id = data.get('id')
        item_data = data.get('data')
        admin_user_id = data.get('adminUserId')

        if not is_admin(admin_user_id):
            return jsonify({"success": False, "message": "Unauthorized: Admin privileges required."}), 403

        collection_ref = db.collection('prize_items')

        if action == 'add':
            if not item_data: return jsonify({"success": False, "message": "Prize item data missing for add."}), 400
            new_doc_ref = collection_ref.add(item_data)[1]
            print(f"Admin {admin_user_id} added prize item: {new_doc_ref.id}")
            return jsonify({"success": True, "message": f"Prize item added successfully with ID: {new_doc_ref.id}"}), 200
        elif action == 'update':
            if not item_id or not item_data: return jsonify({"success": False, "message": "Item ID or data missing for update."}), 400
            doc_ref = collection_ref.document(item_id)
            doc_ref.update(item_data)
            print(f"Admin {admin_user_id} updated prize item: {item_id}")
            return jsonify({"success": True, "message": f"Prize item '{item_id}' updated successfully."}), 200
        elif action == 'delete':
            if not item_id: return jsonify({"success": False, "message": "Item ID missing for delete."}), 400
            collection_ref.document(item_id).delete()
            print(f"Admin {admin_user_id} deleted prize item: {item_id}")
            return jsonify({"success": True, "message": f"Prize item '{item_id}' deleted successfully."}), 200
        else:
            return jsonify({"success": False, "message": "Invalid action specified for prize items."}), 400
    except Exception as e:
        print(f"Error managing prize items (Admin API): {e}")
        traceback.print_exc()
        return jsonify({"success": False, "message": f"Server error managing prize items: {e}"}), 500


# MODIFY EXISTING ENDPOINT
@app.route('/api/admin/update_match_room_details', methods=['POST'])
def admin_update_match_room_details_api_admin():
    try:
        data = request.json
        match_id = data.get('matchId')
        room_code = data.get('roomCode', '')
        room_password = data.get('roomPassword', '')
        admin_user_id = data.get('adminUserId')

        # SECURE ADMIN VERIFICATION
        ADMIN_UID = os.environ.get('ADMIN_UID')
        if admin_user_id != ADMIN_UID:
            return jsonify(success=False, message="Unauthorized access"), 403

        if not match_id:
            return jsonify(success=False, message="Match ID is required"), 400

        # FIXED QUERY (remove isCompleted filter)
        registrations_ref = db.collection('registrations') \
            .where('matchId', '==', match_id) \
            .where('status', '==', 'registered')
        
        updated_count = 0
        batch = db.batch()
        
        for doc in registrations_ref.stream():
            batch.update(doc.reference, {
                "roomCode": room_code,
                "roomPassword": room_password
            })
            updated_count += 1
        
        if updated_count > 0:
            batch.commit()
        
        return jsonify(
            success=True,
            message=f"Updated {updated_count} registrations",
            updatedCount=updated_count
        ), 200

    except Exception as e:
        return jsonify(
            success=False,
            message=f"Batch update failed: {str(e)}"
        ), 500

@app.route('/api/admin/update_registration_status', methods=['POST'])
def update_registration_status_api_admin():
    """Admin API to update a registration's status (e.g., 'canceled', 'completed')."""
    try:
        data = request.json
        registration_id = data.get('registrationId')
        user_id = data.get('userId') # Needed to locate the specific registration document path
        status = data.get('status') # 'canceled', 'completed', 'registered', etc.
        admin_user_id = data.get('adminUserId')

        if not is_admin(admin_user_id):
            return jsonify({"success": False, "message": "Unauthorized: Admin privileges required."}), 403
        if not registration_id or not user_id or not status:
            return jsonify({"success": False, "message": "Registration ID, User ID, and Status are required."}), 400

        doc_ref = db.collection('registrations').document(registration_id)
        doc = doc_ref.get()
        if not doc.exists:
            return jsonify({"success": False, "message": "Registration not found."}), 404

        update_fields = {'status': status}
        if status == 'canceled':
            update_fields['roomCode'] = '' # Clear room code/password on cancellation
            update_fields['roomPassword'] = ''
        elif status == 'completed':
            update_fields['isCompleted'] = True # Mark as completed

        doc_ref.update(update_fields)
        print(f"Admin {admin_user_id} updated registration {registration_id} status to '{status}'.")
        return jsonify({"success": True, "message": f"Registration status updated to '{status}'."}), 200
    except Exception as e:
        print(f"Error updating registration status (Admin API): {e}")
        traceback.print_exc()
        return jsonify({"success": False, "message": f"Server error updating registration status: {e}"}), 500

@app.route('/api/admin/delete_registration', methods=['POST'])
def delete_registration_api_admin():
    """Admin API to permanently delete a tournament registration."""
    try:
        data = request.json
        registration_id = data.get('registrationId')
        user_id = data.get('userId') # Used for logging/context, not strictly needed for doc_ref if top-level
        admin_user_id = data.get('adminUserId')

        if not is_admin(admin_user_id):
            return jsonify({"success": False, "message": "Unauthorized: Admin privileges required."}), 403
        if not registration_id or not user_id:
            return jsonify({"success": False, "message": "Registration ID and User ID are required for deletion."}), 400

        doc_ref = db.collection('registrations').document(registration_id)
        doc = doc_ref.get()
        if not doc.exists:
            return jsonify({"success": False, "message": "Registration not found for deletion."}), 404

        doc_ref.delete()
        print(f"Admin {admin_user_id} deleted registration: {registration_id}")
        return jsonify({"success": True, "message": "Registration deleted successfully."}), 200
    except Exception as e:
        print(f"Error deleting registration (Admin API): {e}")
        traceback.print_exc()
        return jsonify({"success": False, "message": f"Server error deleting registration: {e}"}), 500


@app.route('/api/admin/get_all_registrations', methods=['GET'])
def get_all_registrations_api_admin():
    """
    Admin API to retrieve all tournament registrations for display in the admin panel.
    Includes server-side calculation of 'isCompleted' status and 12-hour time format.
    """
    try:
        admin_user_id = request.args.get('adminUserId')
        if not is_admin(admin_user_id):
            return jsonify({"success": False, "message": "Unauthorized: Admin privileges required."}), 403

        registrations_list = []
        # Use db.collection('registrations') if registrations are in a top-level collection.
        # Use db.collection_group('registrations') if registrations are subcollections under user documents.
        # Assuming 'registrations' is a top-level collection as used in register_tournament.
        docs = db.collection('registrations').stream()

        for doc in docs:
            reg_data = doc.to_dict()
            reg_data['id'] = doc.id
            reg_data['timestamp'] = format_timestamp(reg_data.get('timestamp')) # Format timestamp for display

            # Server-side calculation for match completion status
            match_time_str = reg_data.get('matchTime')
            if match_time_str:
                reg_data['isCompleted'] = is_match_completed_server_side(match_time_str)
                reg_data['matchTime12hr'] = format_time_to_12hr_ist(match_time_str)
            else:
                reg_data['isCompleted'] = False
                reg_data['matchTime12hr'] = 'N/A'

            registrations_list.append(reg_data)

        # Sort by timestamp (most recent first) for consistent display in admin panel
        registrations_list.sort(key=lambda x: x.get('timestamp', '9999-12-31 23:59:59'), reverse=True)

        print(f"Admin {admin_user_id} fetched {len(registrations_list)} registrations.")
        return jsonify({"success": True, "registrations": registrations_list}), 200
    except Exception as e:
        print(f"Error fetching all registrations for admin (Admin API): {e}")
        traceback.print_exc()
        return jsonify({"success": False, "message": f"Server error fetching all registrations: {e}"}), 500


# =====================================================================
# YOUR EXISTING CUSTOM ADMIN ROUTES HERE
# =====================================================================
# If you have other specific admin routes or functionalities,
# copy them into this section.
@app.after_request
def after_request(response):
    origin = request.headers.get('Origin')
    if origin in ["https://www.thatournaments.xyz", "https://trendhiveacademy.github.io"]:
        response.headers['Access-Control-Allow-Origin'] = origin
    response.headers['Access-Control-Allow-Methods'] = 'GET,POST,PUT,DELETE,OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type,Authorization'
    response.headers['Access-Control-Allow-Credentials'] = 'true'
    return response

@app.route('/api/<path:path>', methods=['OPTIONS'])
def options_handler(path):
    return make_response('', 200)


# ADD THIS NEW ENDPOINT
@app.route('/api/admin/update_single_registration_room_details', methods=['POST'])
def update_single_registration_room_details():
    try:
        data = request.json
        registration_id = data.get('registrationId')
        room_code = data.get('roomCode', '')
        room_password = data.get('roomPassword', '')
        admin_user_id = data.get('adminUserId')

        if not is_admin(admin_user_id):
            return jsonify({"success": False, "message": "Unauthorized: Admin privileges required."}), 403

        if not registration_id:
            return jsonify({"success": False, "message": "Registration ID is required."}), 400

        # Update the document
        doc_ref = db.collection('registrations').document(registration_id)
        doc_ref.update({
            'roomCode': room_code,
            'roomPassword': room_password
        })

        return jsonify({"success": True, "message": "Room details updated successfully."}), 200

    except Exception as e:
        print(f"Error updating room details: {e}")
        traceback.print_exc()
        return jsonify({"success": False, "message": f"Server error: {str(e)}"}), 500

# =====================================================================








#Last Day Update on 28th June..
# =====================================================================
# DAILY RESET FUNCTIONS
# =====================================================================
def reset_daily_slots():
    """Resets in-memory slots and clears completed registrations daily"""
    print("🔄 Starting daily reset of match slots...")
    try:
        global available_slots
        
        # Reset in-memory slots
        for match_id in available_slots:
            available_slots[match_id]['booked_slots'] = []
        print("✅ In-memory slots reset")
        
        # Clear completed registrations
        now_ist = datetime.now(IST_TIMEZONE)
        registrations_ref = db.collection('registrations')
        
        # Find registrations for completed matches
        for doc in registrations_ref.where('status', '==', 'registered').stream():
            data = doc.to_dict()
            match_time = data.get('matchTime')
            
            if match_time and is_match_completed_server_side(match_time):
                # Delete or mark as completed based on preference
                if data.get('autoDeleteOnCompletion', True):
                    doc.reference.delete()
                else:
                    doc.reference.update({'status': 'completed'})
        
        print("✅ Completed registrations cleared")
        
        # Refresh in-memory state from Firestore
        initialize_booked_slots_from_firestore_on_startup()
        print("🔄 Slot memory refreshed from Firestore")
        
    except Exception as e:
        print(f"❌ Daily reset failed: {e}")
        traceback.print_exc()




# =====================================================================
# APPLICATION STARTUP
# =====================================================================
# Replace the slot initialization in __main__
#if __name__ == '__main__':
    # Initialize scheduler
    scheduler = BackgroundScheduler(timezone=IST_TIMEZONE)
    # Schedule daily reset at 00:01 IST
    scheduler.add_job(reset_daily_slots, 'cron', hour=0, minute=1)
    scheduler.start()
    print("⏰ Daily reset scheduler started")
# =====================================================================
    #app.run(debug=True, host='0.0.0.0', port=5000)
    # Only initialize in development mode
    #if os.getenv('ENV') == 'development':
        #initialize_booked_slots_from_firestore_on_startup()
# =====================================================================


    
# =====================================================================
    # Run the Flask application
    # debug=True: Enables auto-reloading of Python code changes and debug tools.
    # host='0.0.0.0': Makes the server accessible externally.
    # port=5000: The port on which the Flask server will listen.
    #app.run(debug=True, host='0.0.0.0', port=5000)
# =====================================================================
