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


# =====================================================================
# FLASK APP CONFIGURATION
# =====================================================================
app = Flask(__name__)
# IMPORTANT: Replace 'YOUR_SUPER_SECRET_KEY' with a strong, random, and unique secret key.
# This is crucial for Flask session security. Generate a long, random string.
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'a_very_long_and_complex_random_string_for_dev_purposes_change_this_in_prod_really_change_it')

# IMPORTANT: Update 'origins' to the exact URL of your GitHub Pages site.
# If your GitHub Pages URL is like https://username.github.io/repo-name/, use that.
# If you have multiple origins, list them: ["https://www.thatournaments.xyz", "https://username.github.io"]
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
if not scheduler.running:
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
        
        # Remove emojis and extra spaces, then split.
        # This regex removes characters that are not digits, colons, spaces, A, M, P.
        import re
        cleaned_time_str = re.sub(r'[^\d:APM\s]', '', time_24hr_str).strip()

        # Try parsing with AM/PM first, then fallback to 24-hour if it fails
        try:
            # Handle cases like "2:00 PM" or "11:45 AM"
            time_obj = datetime.strptime(cleaned_time_str, '%I:%M %p').time()
        except ValueError:
            # Handle cases like "14:00" (24-hour format)
            time_obj = datetime.strptime(cleaned_time_str, '%H:%M').time()
        
        # Combine to a datetime object for formatting
        dt_obj = datetime.combine(dummy_date, time_obj)
        
        return dt_obj.strftime('%I:%M %p') # %I for 12-hour, %p for AM/PM
    except ValueError as e:
        print(f"Warning: Could not parse time after cleaning '{time_24hr_str}'. Returning original. Error: {e}")
        return time_24hr_str # Return original if invalid format

def get_next_available_slot(match_slot_id):
    """
    Assigns a dummy slot number. In a real app, this would be more robust.
    Perhaps based on a counter on the match_slot document or pre-defined slots.
    For now, just a simple increment based on existing registrations (less robust).
    """
    # This is a simplified approach. For true slot management, you'd need
    # to maintain available slots or atomically increment a counter.
    # For now, let's just return a random-ish number or a simple counter.
    # A more robust solution would involve a counter field on the match_slot document.
    try:
        # This is not atomic with the transaction above, so it's a weak point for slot assignment.
        # A better way: maintain a 'next_slot_number' field on the match_slot document itself
        # and increment it within the transaction.
        registrations_query = db.collection('registrations').where('matchId', '==', match_slot_id)
        registrations_docs = registrations_query.stream() # Use stream for non-transactional count
        current_registrations = sum(1 for _ in registrations_docs)
        return current_registrations + 1
    except Exception as e:
        print(f"Error getting next available slot: {e}")
        return 1 # Fallback to slot 1

def book_slot_in_memory(match_slot_id, slot_number):
    """Placeholder for updating an in-memory slot tracking system."""
    print(f"In-memory: Booked slot {slot_number} for match {match_slot_id}")

def send_telegram_message(message, parse_mode="HTML"):
    """Sends a message to a Telegram bot."""
    TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
    TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram bot token or chat ID not configured.")
        return False
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': parse_mode
    }
    try:
        response = requests.post(url, json=payload)
        response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)
        print("Telegram message sent successfully!")
        return True
    except requests.exceptions.RequestException as e:
        print(f"Error sending Telegram message: {e}")
        traceback.print_exc()
        return False

def is_match_open_for_registration(match_time_str):
    """
    Checks if a match is open for registration based on its time string.
    Registration closes 20 minutes before match time.
    """
    try:
        now = datetime.now()
        
        import re
        cleaned_time_str = re.sub(r'[^\d:APM\s]', '', match_time_str).strip()

        try:
            match_dt_obj = datetime.strptime(cleaned_time_str, "%I:%M %p").time()
        except ValueError:
            match_dt_obj = datetime.strptime(cleaned_time_str, "%H:%M").time()

        match_datetime = now.replace(hour=match_dt_obj.hour, minute=match_dt_obj.minute, second=0, microsecond=0)

        if match_datetime < now:
            match_datetime += timedelta(days=1)
        
        registration_close_time = match_datetime - timedelta(minutes=20)
        
        return now < registration_close_time
    except Exception as e:
        print(f"Warning: Could not parse 24-hour time '{match_time_str}'. Error: {e}")
        return False

def is_match_completed_server_side(match_time_str):
    """
    Determines if a match is considered 'completed' server-side.
    Now considers date in addition to time.
    """
    try:
        now_ist = datetime.now(IST_TIMEZONE)
        
        match_hour, match_minute = map(int, match_time_str.split(':'))
        match_datetime_ist = now_ist.replace(
            hour=match_hour, 
            minute=match_minute, 
            second=0, 
            microsecond=0
        )
        
        if match_datetime_ist > now_ist:
            return False
        
        completion_time_ist = match_datetime_ist + timedelta(hours=1)
        return now_ist >= completion_time_ist
        
    except Exception as e:
        print(f"Error checking match completion: {e}")
        traceback.print_exc()
        return False

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

def release_slot_in_memory(match_id, slot_number):
    """Releases a slot from the in-memory `available_slots` dictionary."""
    if match_id in available_slots and 'booked_slots' in available_slots[match_id]:
        if slot_number in available_slots[match_id]['booked_slots']:
            available_slots[match_id]['booked_slots'].remove(slot_number)
            print(f"Released slot {slot_number} for {match_id}. Current booked: {available_slots[match_id]['booked_slots']}")
            return True
    print(f"Failed to release slot {slot_number} for {match_id}. Match_id not found or slot not booked.")
    return False

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

        # Now, populate the 'booked_slots' array by querying registrations
        print("  Populating booked_slots from existing registrations...")
        all_registrations_docs = db.collection('registrations').where('status', '==', 'registered').get() # Only active registrations
        
        for reg_doc in all_registrations_docs:
            reg_data = reg_doc.to_dict()
            match_id = reg_data.get('matchId')
            slot_number = reg_data.get('slotNumber')
            
            if match_id in available_slots and slot_number is not None:
                try:
                    slot_number = int(slot_number) 
                except (ValueError, TypeError):
                    print(f"Warning: Invalid slotNumber '{slot_number}' for registration {reg_doc.id}. Skipping.")
                    continue

                if slot_number not in available_slots[match_id]['booked_slots']:
                    available_slots[match_id]['booked_slots'].append(slot_number)
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
# REMOVED WALLET HELPER FUNCTIONS
# =====================================================================

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
# FLASK ROUTES - Frontend Page Renderers (REMOVED FOR STATIC HOSTING)
# These routes are commented out as HTML files are served by GitHub Pages.
# =====================================================================
@app.route('/')
def api_root():
    """Returns a simple JSON response for the API root."""
    return jsonify({"message": "THA Tournaments API is running!", "status": "ok"}), 200
    
# @app.route('/admin_panel.html')
# def admin_panel_page():
#     """Renders the admin panel page (admin_panel.html)."""
#     return render_template('admin_panel.html')

# @app.route('/registered.html')
# def registered_page():
#     """Renders the user's registered matches page (registered.html)."""
#     return render_template('registered.html')

# @app.route('/wallet.html')
# def wallet_page():
#     """Renders the user's wallet page (wallet.html)."""
#     return render_template('wallet.html')

# =====================================================================
# YOUR EXISTING CUSTOM FLASK ROUTES (Frontend or other API) HERE
# =====================================================================

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


@app.route('/api/register-for-match', methods=['POST'])
def register_for_match():
    data = request.json
    match_slot_id = data.get('matchSlotId')
    team_name = data.get('teamName')
    leader_uid = data.get('leaderUid')
    leader_email = data.get('leaderEmail')
    leader_ign = data.get('leaderIgn')
    leader_whatsapp = data.get('leaderWhatsapp')
    member_ign_1 = data.get('memberIgn1')
    member_ign_2 = data.get('memberIgn2')
    member_ign_3 = data.get('memberIgn3')

    if not all([match_slot_id, team_name, leader_uid, leader_email, leader_ign, leader_whatsapp]):
        return jsonify({"success": False, "message": "Missing required registration information."}), 400

    match_slot_doc_ref = db.collection('match_slots').document(match_slot_id)

    try:
        @firestore.transactional
        def _register_transaction_logic(transaction):
            slot_doc = match_slot_doc_ref.get(transaction=transaction)
            if not slot_doc.exists:
                raise ValueError("Match slot not found.")
            
            slot_data = slot_doc.to_dict()
            capacity = slot_data.get('max_players', 0)
            
            registrations_query = db.collection('registrations').where('matchId', '==', match_slot_id).where('status', '==', 'registered')
            registrations_count = len(list(transaction.get(registrations_query)))

            # Check if registration is open
            match_time_str = slot_data.get('time')
            if not is_match_open_for_registration(match_time_str):
                raise ValueError(f"Registration for match at {match_time_str} is closed.")

            if registrations_count >= capacity:
                raise ValueError("Match slot is full.")

            # No wallet deduction or payment check here
            
            slot_number = get_next_available_slot(match_slot_id)
            registration_data = {
                'matchId': match_slot_id,
                'matchType': slot_data.get('type', 'N/A'),
                'matchTime': slot_data.get('time', 'N/A'),
                'teamName': team_name,
                'leaderUid': leader_uid,
                'leaderEmail': leader_email,
                'leaderIgn': leader_ign,
                'leaderWhatsapp': leader_whatsapp,
                'memberIgn1': member_ign_1,
                'memberIgn2': member_ign_2,
                'memberIgn3': member_ign_3,
                'registrationTimestamp': firestore.SERVER_TIMESTAMP,
                'status': 'registered',
                'entryFee': slot_data.get('entry', 0.0), # Still store entry fee for reference
                'slotNumber': slot_number,
                'roomCode': '',
                'roomPassword': ''
            }
            new_registration_ref = db.collection('registrations').document()
            transaction.set(new_registration_ref, registration_data)
            
            book_slot_in_memory(match_slot_id, slot_number)

            return {
                "success": True,
                "message": "Registered for match successfully!",
                "telegram_message_data": {
                    "team_name": team_name,
                    "leader_ign": leader_ign,
                    "leader_email": leader_email,
                    "match_type": slot_data.get('type', 'N/A'),
                    "match_time_str": match_time_str,
                    "slot_number": slot_number
                }
            }

        transaction_result = _register_transaction_logic()

        if transaction_result.get("success"):
            telegram_data = transaction_result["telegram_message_data"]
            telegram_message = (
                f"🎉 New Registration!\n"
                f"Team: {telegram_data['team_name']}\n"
                f"Leader: {telegram_data['leader_ign']} ({telegram_data['leader_email']})\n"
                f"Match: {telegram_data['match_type']} at {telegram_data['match_time_str']}\n"
                f"Slot Number: {telegram_data['slot_number']}"
            )
            send_telegram_message(telegram_message)

        return jsonify(transaction_result), 200

    except ValueError as ve:
        print(f"Registration validation error: {ve}")
        return jsonify({"success": False, "message": str(ve)}), 400
    except Aborted:
        print("Firestore transaction aborted due to contention")
        return jsonify({"success": False, "message": "Transaction failed due to concurrent access. Please try again."}), 500
    except Exception as e:
        print(f"Error registering for match: {e}")
        traceback.print_exc()
        return jsonify({"success": False, "message": "An unexpected error occurred during registration."}), 500
        
@app.route('/api/get_registrations', methods=['GET'])
async def get_registrations():
    user_id = request.args.get('userId')
    if not user_id:
        return jsonify({"success": False, "message": "User ID is required to fetch registrations."}), 400

    try:
        registrations_ref = db.collection('registrations')\
                              .where('leaderUid', '==', user_id)\
                              .order_by('registrationTimestamp', direction=firestore.Query.DESCENDING)
        
        docs = registrations_ref.stream()

        registrations_list = []
        for doc in docs:
            data = doc.to_dict()
            data['id'] = doc.id

            try:
                data['registrationTimestamp'] = format_timestamp(data.get('registrationTimestamp'))
            except:
                data['registrationTimestamp'] = 'Invalid timestamp'

            try:
                data['isCompleted'] = is_match_completed_server_side(data.get('matchTime', ''))
            except:
                data['isCompleted'] = False

            data['roomCode'] = data.get('roomCode', '')
            data['roomPassword'] = data.get('roomPassword', '')
            data['entryFee'] = data.get('entryFee', 0.0)

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
async def get_match_participants():
    """
    Fetches participants (IGN, FFID) for a specific match ID.
    Accessible to any logged-in user to see their lobby.
    """
    match_id = request.args.get('matchId')
    if not match_id:
        return jsonify({"success": False, "message": "Match ID is required to fetch participants."}), 400

    try:
        participants_ref = db.collection('registrations').where('matchId', '==', match_id).where('status', '==', 'registered')
        
        docs = participants_ref.stream()
        
        participants_list = []
        for doc in docs:
            data = doc.to_dict()
            participant = {
                "leaderIgn": data.get('leaderIgn', 'N/A'),
                "leaderWhatsapp": data.get('leaderWhatsapp', 'N/A'),
                "slotNumber": data.get('slotNumber', 'N/A'),
                "teamName": data.get('teamName', 'N/A'),
                "members": []
            }
            all_members = []
            if data.get('leaderIgn') and data.get('leaderWhatsapp'):
                all_members.append({'ign': data['leaderIgn'], 'ffid': data['leaderWhatsapp']})

            if data.get('memberIgn1'):
                all_members.append({'ign': data['memberIgn1'], 'ffid': 'N/A'})
            if data.get('memberIgn2'):
                all_members.append({'ign': data['memberIgn2'], 'ffid': 'N/A'})
            if data.get('memberIgn3'):
                all_members.append({'ign': data['memberIgn3'], 'ffid': 'N/A'})

            participant['members'] = all_members
            participants_list.append(participant)
        
        participants_list.sort(key=lambda x: x.get('slotNumber', float('inf')))

        return jsonify({"success": True, "participants": participants_list}), 200

    except Exception as e:
        print(f"Error fetching match participants: {e}")
        traceback.print_exc()
        return jsonify({"success": False, "message": f"Failed to fetch match participants: {str(e)}"}), 500


@app.route('/api/update_registration_status', methods=['POST'])
async def update_registration_status():
    """Updates the status (e.g., 'canceled') of a registration and manages slots."""
    try:
        data = request.json
        registration_id = data.get('registrationId')
        user_id = data.get('userId')
        new_status = data.get('status')
        admin_user_id_from_request = data.get('adminUserId')

        if not all([registration_id, user_id, new_status]):
            return jsonify({"success": False, "message": "Missing registration ID, user ID, or new status."}), 400

        registration_doc_ref = db.collection('registrations').document(registration_id)
        registration_doc = await registration_doc_ref.get()

        if not registration_doc.exists:
            return jsonify({"success": False, "message": "Registration not found."}), 404
            
        current_data = registration_doc.to_dict()
        
        if not (is_admin(admin_user_id_from_request) or current_data.get('leaderUid') == user_id):
            return jsonify({"success": False, "message": "Unauthorized: You can only modify your own registrations or require admin privileges."}), 403
            
        if current_data.get('status') == 'canceled' and new_status == 'canceled':
            return jsonify({"success": False, "message": "This registration is already canceled."}), 400

        await registration_doc_ref.update({"status": new_status})

        if new_status == 'canceled':
            match_id = current_data.get('matchId')
            slot_number = current_data.get('slotNumber')

            if match_id and slot_number:
                release_slot_in_memory(match_id, slot_number)
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
async def update_auto_delete_preference():
    """Updates the autoDeleteOnCompletion preference for a registration."""
    try:
        data = request.json
        registration_id = data.get('registrationId')
        user_id = data.get('userId')
        auto_delete = data.get('autoDelete')

        if not all([registration_id, user_id, auto_delete is not None]):
            return jsonify({"success": False, "message": "Missing registration ID, user ID, or autoDelete preference."}), 400

        registration_doc_ref = db.collection('registrations').document(registration_id)
        registration_doc = await registration_doc_ref.get()

        if not registration_doc.exists:
            return jsonify({"success": False, "message": "Registration not found."}), 404
            
        current_data = registration_doc.to_dict()
        if current_data.get('leaderUid') != user_id:
            return jsonify({"success": False, "message": "Unauthorized: You can only modify your own registrations."}), 403

        await registration_doc_ref.update({"autoDeleteOnCompletion": auto_delete})
        return jsonify({"success": True, "message": "Auto-delete preference updated successfully."}), 200
    except Exception as e:
        print(f"Error updating auto-delete preference: {e}")
        traceback.print_exc()
        return jsonify({"success": False, "message": f"An error occurred while updating preference: {str(e)}"}), 500

@app.route('/api/delete_registration', methods=['POST'])
async def delete_registration():
    """Allows a user or admin to manually delete a registration from Firestore and releases the slot."""
    try:
        data = request.json
        registration_id = data.get('registrationId')
        user_id = data.get('userId')
        admin_user_id_from_request = data.get('adminUserId')

        if not registration_id or not user_id:
            return jsonify({"success": False, "message": "Registration ID and User ID are required for deletion."}), 400

        registration_doc_ref = db.collection('registrations').document(registration_id)
        registration_doc = await registration_doc_ref.get()

        if not registration_doc.exists:
            return jsonify({"success": False, "message": "Registration not found."}), 404

        registration_data = registration_doc.to_dict()
        
        if not (is_admin(admin_user_id_from_request) or registration_data.get('leaderUid') == user_id):
            return jsonify({"success": False, "message": "Unauthorized deletion attempt."}), 403
            
        match_id = registration_data.get('matchId')
        slot_number = registration_data.get('slotNumber')
        
        if match_id and slot_number and registration_data.get('status') != 'canceled':
            release_slot_in_memory(match_id, slot_number)
            print(f"Slot {slot_number} for {match_id} released due to manual deletion.")

        await registration_doc_ref.delete()

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
async def create_firebase_user_api_admin():
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
async def delete_firebase_user_api_admin():
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
            user = auth.get_user_by_email(target_email)
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
async def update_firebase_user_password_api_admin():
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
        if target_email and not target_uid:
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
async def update_website_content_api_admin():
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
        await doc_ref.set(content, merge=True)
        print(f"Admin {admin_user_id} updated website content.")
        return jsonify({"success": True, "message": "Website content updated successfully."}), 200
    except Exception as e:
        print(f"Error updating website content (Admin API): {e}")
        traceback.print_exc()
        return jsonify({"success": False, "message": f"Server error updating website content: {e}"}), 500

@app.route('/api/admin/match_slots', methods=['POST'])
async def manage_match_slots_api_admin():
    """Admin API to add, update, or delete match slots."""
    try:
        data = request.json
        action = data.get('action')
        slot_id = data.get('id')
        slot_data = data.get('data')
        admin_user_id = data.get('adminUserId')

        if not is_admin(admin_user_id):
            return jsonify({"success": False, "message": "Unauthorized: Admin privileges required."}), 403
        if not slot_id:
            return jsonify({"success": False, "message": "Match Slot ID is required."}), 400

        doc_ref = db.collection('match_slots').document(slot_id)

        if action == 'add':
            if not slot_data: return jsonify({"success": False, "message": "Slot data is missing for add action."}), 400
            await doc_ref.set(slot_data)
            print(f"Admin {admin_user_id} added match slot: {slot_id}")
            initialize_booked_slots_from_firestore_on_startup()
            return jsonify({"success": True, "message": f"Match slot '{slot_id}' added successfully."}), 200
        elif action == 'update':
            if not slot_data: return jsonify({"success": False, "message": "Slot data is missing for update action."}), 400
            await doc_ref.update(slot_data)
            print(f"Admin {admin_user_id} updated match slot: {slot_id}")
            initialize_booked_slots_from_firestore_on_startup()
            return jsonify({"success": True, "message": f"Match slot '{slot_id}' updated successfully."}), 200
        elif action == 'delete':
            await doc_ref.delete()
            print(f"Admin {admin_user_id} deleted match slot: {slot_id}")
            initialize_booked_slots_from_firestore_on_startup()
            return jsonify({"success": True, "message": f"Match slot '{slot_id}' deleted successfully."}), 200
        else:
            return jsonify({"success": False, "message": "Invalid action specified for match slots."}), 400
    except Exception as e:
        print(f"Error managing match slots (Admin API): {e}")
        traceback.print_exc()
        return jsonify({"success": False, "message": f"Server error managing match slots: {e}"}), 500

@app.route('/api/admin/schedule_items', methods=['POST'])
async def manage_schedule_items_api_admin():
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
            new_doc_ref = await collection_ref.add(item_data)
            print(f"Admin {admin_user_id} added schedule item: {new_doc_ref[1].id}")
            return jsonify({"success": True, "message": f"Schedule item added successfully with ID: {new_doc_ref[1].id}"}), 200
        elif action == 'update':
            if not item_id or not item_data: return jsonify({"success": False, "message": "Item ID or data missing for update."}), 400
            doc_ref = collection_ref.document(item_id)
            await doc_ref.update(item_data)
            print(f"Admin {admin_user_id} updated schedule item: {item_id}")
            return jsonify({"success": True, "message": f"Schedule item '{item_id}' updated successfully."}), 200
        elif action == 'delete':
            if not item_id: return jsonify({"success": False, "message": "Item ID missing for delete."}), 400
            await collection_ref.document(item_id).delete()
            print(f"Admin {admin_user_id} deleted schedule item: {item_id}")
            return jsonify({"success": True, "message": f"Schedule item '{item_id}' deleted successfully."}), 200
        else:
            return jsonify({"success": False, "message": "Invalid action specified for schedule items."}), 400
    except Exception as e:
        print(f"Error managing schedule items (Admin API): {e}")
        traceback.print_exc()
        return jsonify({"success": False, "message": f"Server error managing schedule items: {e}"}), 500

@app.route('/api/admin/prize_items', methods=['POST'])
async def manage_prize_items_api_admin():
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
            new_doc_ref = await collection_ref.add(item_data)
            print(f"Admin {admin_user_id} added prize item: {new_doc_ref[1].id}")
            return jsonify({"success": True, "message": f"Prize item added successfully with ID: {new_doc_ref[1].id}"}), 200
        elif action == 'update':
            if not item_id or not item_data: return jsonify({"success": False, "message": "Item ID or data missing for update."}), 400
            doc_ref = collection_ref.document(item_id)
            await doc_ref.update(item_data)
            print(f"Admin {admin_user_id} updated prize item: {item_id}")
            return jsonify({"success": True, "message": f"Prize item '{item_id}' updated successfully."}), 200
        elif action == 'delete':
            if not item_id: return jsonify({"success": False, "message": "Item ID missing for delete."}), 400
            await collection_ref.document(item_id).delete()
            print(f"Admin {admin_user_id} deleted prize item: {item_id}")
            return jsonify({"success": True, "message": f"Prize item '{item_id}' deleted successfully."}), 200
        else:
            return jsonify({"success": False, "message": "Invalid action specified for prize items."}), 400
    except Exception as e:
        print(f"Error managing prize items (Admin API): {e}")
        traceback.print_exc()
        return jsonify({"success": False, "message": f"Server error managing prize items: {e}"}), 500


@app.route('/api/admin/update_match_room_details', methods=['POST'])
async def admin_update_match_room_details_api_admin():
    try:
        data = request.json
        match_id = data.get('matchId')
        room_code = data.get('roomCode', '')
        room_password = data.get('roomPassword', '')
        admin_user_id = data.get('adminUserId')

        ADMIN_UID = os.environ.get('ADMIN_UID')
        if admin_user_id != ADMIN_UID:
            return jsonify(success=False, message="Unauthorized access"), 403

        if not match_id:
            return jsonify(success=False, message="Match ID is required"), 400

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
            await batch.commit()
        
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
async def update_registration_status_api_admin():
    """Admin API to update a registration's status (e.g., 'canceled', 'completed')."""
    try:
        data = request.json
        registration_id = data.get('registrationId')
        user_id = data.get('userId')
        status = data.get('status')
        admin_user_id = data.get('adminUserId')

        if not is_admin(admin_user_id):
            return jsonify({"success": False, "message": "Unauthorized: Admin privileges required."}), 403
        if not registration_id or not user_id or not status:
            return jsonify({"success": False, "message": "Registration ID, User ID, and Status are required."}), 400

        doc_ref = db.collection('registrations').document(registration_id)
        doc = await doc_ref.get()
        if not doc.exists:
            return jsonify({"success": False, "message": "Registration not found."}), 404

        update_fields = {'status': status}
        if status == 'canceled':
            update_fields['roomCode'] = ''
            update_fields['roomPassword'] = ''
        elif status == 'completed':
            update_fields['isCompleted'] = True

        await doc_ref.update(update_fields)
        print(f"Admin {admin_user_id} updated registration {registration_id} status to '{status}'.")
        return jsonify({"success": True, "message": f"Registration status updated to '{status}'."}), 200
    except Exception as e:
        print(f"Error updating registration status (Admin API): {e}")
        traceback.print_exc()
        return jsonify({"success": False, "message": f"Server error updating registration status: {e}"}), 500

@app.route('/api/admin/delete_registration', methods=['POST'])
async def delete_registration_api_admin():
    """Admin API to permanently delete a tournament registration."""
    try:
        data = request.json
        registration_id = data.get('registrationId')
        user_id = data.get('userId')
        admin_user_id = data.get('adminUserId')

        if not is_admin(admin_user_id):
            return jsonify({"success": False, "message": "Unauthorized: Admin privileges required."}), 403
        if not registration_id or not user_id:
            return jsonify({"success": False, "message": "Registration ID and User ID are required for deletion."}), 400

        doc_ref = db.collection('registrations').document(registration_id)
        doc = await doc_ref.get()
        if not doc.exists:
            return jsonify({"success": False, "message": "Registration not found for deletion."}), 404

        await doc_ref.delete()
        print(f"Admin {admin_user_id} deleted registration: {registration_id}")
        return jsonify({"success": True, "message": "Registration deleted successfully."}), 200
    except Exception as e:
        print(f"Error deleting registration: {e}")
        traceback.print_exc()
        return jsonify({"success": False, "message": f"An error occurred during deletion: {str(e)}"}), 500


@app.route('/api/admin/get_all_registrations', methods=['GET'])
async def get_all_registrations_api_admin():
    """
    Admin API to retrieve all tournament registrations for display in the admin panel.
    Includes server-side calculation of 'isCompleted' status and 12-hour time format.
    """
    try:
        admin_user_id = request.args.get('adminUserId')
        if not is_admin(admin_user_id):
            return jsonify({"success": False, "message": "Unauthorized: Admin privileges required."}), 403

        registrations_list = []
        docs = db.collection('registrations').stream()

        for doc in docs:
            reg_data = doc.to_dict()
            reg_data['id'] = doc.id
            reg_data['registrationTimestamp'] = format_timestamp(reg_data.get('registrationTimestamp'))

            match_time_str = reg_data.get('matchTime')
            if match_time_str:
                reg_data['isCompleted'] = is_match_completed_server_side(match_time_str)
                reg_data['matchTime12hr'] = format_time_to_12hr_ist(match_time_str)
            else:
                reg_data['isCompleted'] = False
                reg_data['matchTime12hr'] = 'N/A'

            registrations_list.append(reg_data)

        registrations_list.sort(key=lambda x: x.get('registrationTimestamp', '9999-12-31 23:59:59'), reverse=True)

        print(f"Admin {admin_user_id} fetched {len(registrations_list)} registrations.")
        return jsonify({"success": True, "registrations": registrations_list}), 200
    except Exception as e:
        print(f"Error fetching all registrations for admin (Admin API): {e}")
        traceback.print_exc()
        return jsonify({"success": False, "message": f"Server error fetching all registrations: {e}"}), 500


# =====================================================================
# YOUR EXISTING CUSTOM ADMIN ROUTES HERE
# =====================================================================
@app.after_request
def after_request(response):
    origin = request.headers.get('Origin')
    allowed_origins = ["https://www.thatournaments.xyz", "https://trendhiveacademy.github.io"]
    
    if origin and origin in allowed_origins:
        response.headers['Access-Control-Allow-Origin'] = origin
    else:
        pass 

    response.headers['Access-Control-Allow-Methods'] = 'GET,POST,PUT,DELETE,OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type,Authorization'
    response.headers['Access-Control-Allow-Credentials'] = 'true'
    return response

@app.route('/api/<path:path>', methods=['OPTIONS'])
def options_handler(path):
    return make_response('', 200)

@app.route('/api/admin/update_single_registration_room_details', methods=['POST'])
async def update_single_registration_room_details():
    try:
        data = request.json
        registration_id = data.get('registrationId')
        room_code = data.get('roomCode', '')
        room_password = data.get('roomPassword', '')
        admin_user_id = data.get('adminUserId')

        ADMIN_UID = os.environ.get('ADMIN_UID')
        if admin_user_id != ADMIN_UID:
            return jsonify({"success": False, "message": "Unauthorized access"}), 403

        if not registration_id:
            return jsonify({"success": False, "message": "Registration ID is required"}), 400

        doc_ref = db.collection('registrations').document(registration_id)
        await doc_ref.update({
            'roomCode': room_code,
            'roomPassword': room_password
        })

        return jsonify({"success": True, "message": "Room details updated successfully."}), 200

    except Exception as e:
        print(f"Error updating room details: {e}")
        traceback.print_exc()
        return jsonify({"success": False, "message": f"Server error: {str(e)}"}), 500

# =====================================================================
# REMOVED WALLET API ENDPOINTS
# =====================================================================

# =====================================================================
# REMOVED RAZORPAY CONFIGURATION AND API ENDPOINTS
# =====================================================================

# =====================================================================
# DAILY RESET FUNCTIONS
# =====================================================================
def reset_daily_slots():
    """Resets in-memory slots and clears completed registrations daily"""
    print("🔄 Starting daily reset of match slots...")
    try:
        global available_slots
        
        for match_id in available_slots:
            available_slots[match_id]['booked_slots'] = []
        print("✅ In-memory slots reset")
        
        now_ist = datetime.now(IST_TIMEZONE)
        registrations_ref = db.collection('registrations')
        
        for doc in registrations_ref.where('status', '==', 'registered').stream():
            data = doc.to_dict()
            match_time = data.get('matchTime')
            
            if match_time and is_match_completed_server_side(match_time):
                if data.get('autoDeleteOnCompletion', True):
                    doc.reference.delete()
                else:
                    doc.reference.update({'status': 'completed'})
        
        print("✅ Completed registrations cleared")
        
        initialize_booked_slots_from_firestore_on_startup()
        print("🔄 Slot memory refreshed from Firestore")
        
    except Exception as e:
        print(f"❌ Daily reset failed: {e}")
        traceback.print_exc()


# =====================================================================
# APPLICATION STARTUP
# =====================================================================
if __name__ == '__main__':
    scheduler = BackgroundScheduler(timezone=IST_TIMEZONE)
    scheduler.add_job(reset_daily_slots, 'cron', hour=0, minute=1)
    scheduler.start()
    print("⏰ Daily reset scheduler started")
