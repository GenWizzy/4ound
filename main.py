import os
import math
import threading
import logging
import json
import hashlib
import time
import sys
import io
import urllib.parse
import random
import uuid
import re
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta, timezone
from collections import deque
from typing import Union, Dict, List

# Flask & External Services
from flask import Flask, request, jsonify
from dotenv import load_dotenv
import requests
from geopy.geocoders import Nominatim
from better_profanity import profanity
from langdetect import detect, LangDetectException
from rapidfuzz import process, fuzz

# Google Cloud & AI
from google import genai
from google.genai import types
from google.api_core import retry as g_retry, exceptions
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from google.oauth2 import service_account

# Scheduler
from apscheduler.schedulers.background import BackgroundScheduler



# Initialize the model at start-up.
# It will consume memory once, but it won't crash on search requests.
# Zero-shot classifier for greeting/intent detection
#zero_shot_pipeline = pipeline(
#    "zero-shot-classification",
#    model="MoritzLaurer/deberta-v3-base-mnli-fever-anli"
#)

# --- GLOBAL AREA (The "Storage") ---
CACHED_MARKET_TIP = {}  # Dict keyed by city
LAST_CACHE_TIME = {}    # Dict keyed by city
# -------------------------
# Load environment variables
# -------------------------
load_dotenv()

ADMIN_SECRET_CODE = os.getenv("ADMIN_SECRET_CODE")
ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN", "PASTE_FALLBACK_TOKEN_HERE")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "4ound_bot_token")
META_TEST_NUMBER = os.getenv("META_TEST_NUMBER", "+15551903534")
FOURSQUARE_SERVICE_KEY = os.getenv("FOURSQUARE_SERVICE_KEY")
WHATSAPP_TOKEN = ACCESS_TOKEN
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID") # You still need this one!
# Make sure GOOGLE_APPLICATION_CREDENTIALS is set to 4oundkey.json
# Do NOT need FIREBASE_PROJECT if using service account
# --- Replace your old single line with this block ---
if os.getenv('GOOGLE_APPLICATION_CREDENTIALS_JSON'):
    key_dict = json.loads(os.getenv('GOOGLE_APPLICATION_CREDENTIALS_JSON'))
    creds = service_account.Credentials.from_service_account_info(key_dict)
    db = firestore.Client(credentials=creds)
else:
    db = firestore.Client.from_service_account_json("4oundkey.json")
# -----------------------------------------------------
# Uses project info from the JSON key
FIRESTORE_OFFERS =  "listings"
FIRESTORE_SESSIONS = "sessions"
FIRESTORE_INTERACTIONS = os.getenv("FIRESTORE_COLLECTION_INTERACTIONS", "interactions")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "paraphrase-multilingual-MiniLM-L12-v2")
# Admin token for secure endpoints (set in .env)
ADMIN_RELOAD_TOKEN = os.getenv("ADMIN_RELOAD_TOKEN", "change_me_token")
# Training schedule (cron-like): default daily at 03:00 local time
TRAIN_HOUR = int(os.getenv("TRAIN_HOUR", "3"))
TRAIN_MINUTE = int(os.getenv("TRAIN_MINUTE", "0"))

# Processed Messages
processed_messages = {}
processed_message_ids = set()
# -------------------------
# Logging configuration
# -------------------------
# -------------------------
# Logging configuration (UTF-8 Fix)
# -------------------------
logger = logging.getLogger("found_bot")
logger.setLevel(logging.INFO)

# 1. Added encoding="utf-8" to handle emojis in the log file
file_handler = RotatingFileHandler(
    "found_bot.log",
    maxBytes=5_000_000,
    backupCount=3,
    encoding="utf-8"
)
file_formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
file_handler.setFormatter(file_formatter)
logger.addHandler(file_handler)

# 2. Fix for Windows console (cp1252) crashing on emojis

if sys.platform == "win32":
    # This ensures the console output uses utf-8 instead of the Windows default
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(file_formatter)
logger.addHandler(console_handler)





# -------------------------
# Flask app setup
# -------------------------
app = Flask(__name__)

# -------------------------
# Helpers: normalization, safety, language
# -------------------------

# --- API CONFIG ---
FB_API_VERSION = "v18.0" # Centralized versioning


geolocator = Nominatim(user_agent="4ound_global_engine")



# 2. CONFIGURATION
load_dotenv()

# The Client automatically finds 'GEMINI_API_KEY' in your .env
# We initialize it once here to use throughout the script
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

# 3. GLOBAL PROMPTS
# Keeping these separate makes them easy to tweak later
INTENT_SYSTEM_PROMPT = "Classify user intent as SEARCH or LISTING."

UNIFIED_4OUND_PROMPT = """
You are the 4ound Global Brain. Analyze user messages (any language, regional Nigerian slang, or Pidgin) and return ONLY a raw JSON object.

CORE MISSION:
1. Identify 'intent': 'search' (finding), 'offer' (selling/listing), 'greeting' (hello/intro).
2. Extract 'item': The literal product or service mentioned (e.g., 'bread', 'barber').
3. Generate 'search_query': A map-optimized venue type (e.g., 'Supermarket', 'Restaurant').
4. Map 'category_id': The Foursquare v3 ID for the PLACE that provides the item.
5. Detect 'language': 'English', 'Pidgin', 'Hausa', 'Yoruba', or 'Igbo'.

SMART MAPPING LOGIC (Intent -> Venue):
If the user's request is vague or expresses a need, map it to the provider:
- "I'm hungry/starving" -> item: "food", search_query: "Restaurant", category_id: "13000"
- "My car stop/spoilt" -> item: "mechanic", search_query: "Auto Repair", category_id: "11013"
- "I need cash/money" -> item: "ATM", search_query: "Bank", category_id: "11044"
- "Headache/Sick" -> item: "medicine", search_query: "Pharmacy", category_id: "17091"
- "Buy fuel/petrol" -> item: "fuel", search_query: "Gas Station", category_id: "19007"
- "Cut my hair" -> item: "haircut", search_query: "Barber Shop", category_id: "11062"

CATEGORY REFERENCE:
- '17069': Grocery/Supermarkets (bread, milk, biscuits, provisions)
- '17091': Pharmacies (medicine, drugs, health)
- '19007': Gas Stations (petrol, diesel, kerosene)
- '13000': Dining/Drinking (prepared food, buka, mama-put)
- '17045': Electronics (phones, repairs, chargers)
- '11044': Banks/ATMs
- '11000': Professional Services (tradesmen, salons)

STRICT RULES:
- Return ONLY valid JSON. NO markdown blocks (```json) or prose.
- If slang like "I de find", "Abeg", or "Wetin" is used, language is 'Pidgin'.
- If the user is GREETING, set intent to 'greeting' and other fields to null.

JSON Structure:
{
  "intent": "search" | "offer" | "greeting",
  "item": "string",
  "search_query": "string",
  "category_id": "string",
  "language": "string"
}
"""

# Contextual negation tokens for English and Naija/Pidgin
NEGATION_TOKENS = [
    r"\bno\b",
    r"\bdon'?t\b",
    r"\bnot\b",
    r"\bnever\b",
    r"\bi no\b",
    r"\bstop\b",
    r"\bnah\b"
]


# 4ound AI Prohibited Categories
ammunition_slang = ["gun", "pistol", "bullets", "ak47", "rifle", "weapon"]
sexual_content = ["sex", "nude", "porn", "xxx", "hookup", "fuck", "dick","toto", "pussy", "prick"]
gruesome_content = ["blood", "kill", "dead", "murder", "wound"]

# Merge and load into the library
all_prohibited = ammunition_slang + sexual_content + gruesome_content
profanity.load_censor_words(all_prohibited)

# Load the responses once when the bot starts
def load_responses():
    try:
        with open('responses.json', 'r', encoding='utf-8') as file:
            data = json.load(file)
            if not data:
                logger.warning("⚠️ responses.json is empty. Using fallback.")
                return {}
            return data
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.error(f"❌ Critical Error loading responses.json: {e}")
        return {}

RESPONSES_DATA = load_responses()



def get_system_reply(category, sub_key, item_name="something", language="English"):
    """
    1. Navigates the nested RESPONSES_DATA (e.g., search -> no_results).
    2. Picks a random template and formats the item name.
    3. Translates via Gemini ONLY if the detected language isn't English.
    """
    try:
        # 1. Navigate to the specific list (e.g., search -> searching_start)
        # Fallback to 'general' -> 'greeting' if the category/key is missing
        category_data = RESPONSES_DATA.get(category, RESPONSES_DATA.get("general", {}))
        options = category_data.get(sub_key, category_data.get("greeting", ["Hello!"]))

        # 2. Pick a random template and format it
        chosen_template = random.choice(options)
        english_reply = chosen_template.replace("{item}", item_name)

    except Exception as e:
        logger.error(f"Error accessing responses JSON: {e}")
        english_reply = f"Looking for {item_name}..."

    # 3. The Language Gate: Only call Gemini if NOT English
    if language.lower() != "english":
        try:
            # Using your Gemini 3 Flash Lite for cost-effective translation
            translation_prompt = f"Translate this to {language} naturally: {english_reply}"
            response = client.models.generate_content(
                model="models/gemini-3-flash-lite",  # Adjusted to your available model
                contents=translation_prompt
            )
            return response.text.strip()
        except Exception as e:
            logger.error(f"Translation failed, falling back to English: {e}")
            return english_reply

    return english_reply


def translate_if_needed(text, language):
    """Simple wrapper to translate onboarding strings on the fly."""
    if language.lower() == "english":
        return text

    try:
        # Ultra-fast translation call for short strings
        prompt = f"Translate this WhatsApp message to {language} naturally: {text}"
        response = client.models.generate_content(
            model="models/gemini-3.1-flash-lite",
            contents=prompt
        )
        return response.text.strip()
    except Exception as e:
        logger.error(f"Onboarding translation failed: {e}")
        return text  # Fallback to English


def refine_query(text):
    """
    Strips away conversational filler and grammar particles so the AI
    focuses on the core product or service.
    """
    if not text:
        return ""

    # 1. Phrases to remove (Updated to include business-related fillers)
    fillers = [
        "i am looking for", "i'm looking for", "looking for a", "looking for",
        "i want to buy", "i want buy", "i want", "do you have", "is there any",
        "someone who can", "help me with", "search for", "can i find",
        "i need a", "i need", "please", "show me",
        "seller", "shop", "provider", "vendor", "business", "services", "service"
    ]

    refined = text.lower()
    for phrase in fillers:
        refined = refined.replace(phrase, "")

    # 2. Clean up standalone particles (a, an, the, some)
    refined = re.sub(r'\b(a|an|the|some|any|in|at|near)\b', '', refined)

    # 3. Final cleanup of punctuation and extra whitespace
    refined = re.sub(r'[?!.,]', '', refined)  # Remove punctuation
    refined = " ".join(refined.split())  # Collapse multiple spaces into one

    # 4. Handle Empty results or Capitalization
    final_query = refined.capitalize() if refined else text.capitalize()

    return final_query


def normalize_number(n: str) -> str:
    if not n:
        return ""
    return n.strip().replace(" ", "").replace("-", "")

TEST_NUMBER_NORM = normalize_number(META_TEST_NUMBER)
PHONE_RE = re.compile(r"(\+?\d{7,15})")

# Keyword sets for blocking; extend from logs as needed
MISSING_KEYWORDS = {"missing", "lost", "missing person", "gone missing", "not found", "disappeared"}
SEXUAL_KEYWORDS = {"sex", "porn", "nude", "nudes", "sexual", "xxx", "pornography", "explicit"}


# Candidate labels used throughout your bot
CANDIDATE_LABELS = ["greeting", "offer", "search", "other"]

greeting_examples = [
    "hello",
    "hi",
    "hey",
    "hello there",
    "hi there",
    "hey there",
    "good morning",
    "good afternoon",
    "good evening",
    "yo",
    "sup",
    "what's up",
    "how far",
    "hello bot",
    "hi bot"
]

# --- Example messages for prototype classifier ---
search_examples = [
    "I am looking for a barber",
    "Where can I find a mechanic?",
    "I need a plumber in Abuja",
    "Searching for a hairdresser nearby",
    "Can you help me find a tutor?"
]

offer_examples = [
    "I am selling my laptop",
    "Offering graphic design services",
    "I want to sell my old bike",
    "I provide tutoring lessons",
    "For sale: used iPhone 12"
]

other_examples = [
    "How's the weather?",
    "Tell me a joke",
    "What is AI?",
    "Random question here"
]

service_roles = [
        "developer", "dev", "engineer", "mechanic", "teacher",
        "plumber", "tailor", "barber", "designer", "photographer",
        "electrician", "doctor", "nurse", "carpenter",
        "stylist", "makeup", "artist", "graphics"
    ]

# supports optional currency symbols, commas, decimals, k/m suffixes
PRICE_RE = r"(?:(?:n|₦|\$|€|£)\s?)?(\d{1,3}(?:[,\s]\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)([kKmM])?\b"

# 🚀 Global data storage
# Instead of an index object, we store the raw matrix of all embeddings
embeddings_matrix = None
id_map = [] # A simple list of IDs corresponding to the rows in the matrix


def parse_price(text, default_locale=None):
    t = text.lower()
    m = re.search(PRICE_RE, t)
    if not m:
        return None
    raw = m.group(1).replace(',', '').replace(' ', '')
    suffix = (m.group(2) or '').lower()
    try:
        num = float(raw)
    except ValueError:
        return None
    if suffix == 'k':
        num *= 1_000
    elif suffix == 'm':
        num *= 1_000_000
    return int(num)

def detect_negation(text, window_tokens=5):
    t = text.lower()
    if not any(re.search(tok, t) for tok in NEGATION_TOKENS):
        return False
    tokens = re.findall(r"\w+|\S", t)
    for i, tok in enumerate(tokens):
        if re.fullmatch(r"(no|not|never|don'?t|nah|stop|i no)", tok):
            left = max(0, i - window_tokens)
            right = min(len(tokens), i + window_tokens + 1)
            window = " ".join(tokens[left:right])
            if re.search(r"\b(sell|selling|buy|want|need|looking|find|hire|available)\b", window):
                return True
    return False


def blend_confidence(model_conf, prior, alpha=0.35):
    final = (1 - alpha) * float(model_conf) + alpha * float(prior)
    return max(0.0, min(1.0, final))



def has_role(text, roles=service_roles):
    text_lower = text.lower()
    tokens = re.findall(r"\w+", text_lower)
    # Exact token match only
    return any(token in roles for token in tokens)

session = {}  # or import from your session context


def rule_quick_sell(text):
    candidates = []
    text_low = text.lower().strip()
    price = parse_price(text)

    # --- 1. DETECTION FLAGS ---
    # Keywords that imply a physical item is involved
    item_context = bool(re.search(r"\b(my|this|these|those|used|fairly|tokunbo|brand new|clean|carton)\b", text_low))

    # Strong Action Verbs (The "Izu" patterns)
    # Added 'sale', 'selling', 'sell'
    action_keywords = r"\b(sell|selling|sale|for sale|wan sell|disposed?|liquidate)\b"
    has_action = bool(re.search(action_keywords, text_low))

    # Pattern for "I sell [item]" or "I am selling [item]"
    # This catches "I sell biscuits" or "I am selling tomatoes"
    direct_selling_pattern = bool(re.search(r"\b(i|we)\s+(sell|am selling|de sell)\b", text_low))

    # "Buy my..." is a classic seller phrase
    buy_my = bool(re.search(r"\b(buy my|buy this|purchase my)\b", text_low))

    neg = detect_negation(text)

    # --- 2. THE LOGIC GATES ---

    # GATE 1: ULTRA-HIGH CONFIDENCE (Explicit intent)
    # Catches: "I want to sell books", "I am selling tomatoes"
    if (direct_selling_pattern or buy_my) and not neg:
        candidates.append(("quick_sell", 0.98, {"price": price}))
        return candidates  # Exit early, we are sure

    # GATE 2: KEYWORD + MINIMUM INFO
    # Catches: "I have computer for sale"
    # len >= 2 allows "Sell shoes" or "Sale: Bag"
    has_min_words = len(text_low.split()) >= 2

    if has_action and (item_context or price or has_min_words) and not neg:
        # If they use 'sale' or 'sell' and provide an item name, we're 95% sure
        candidates.append(("quick_sell", 0.95, {"price": price}))

    # GATE 3: THE "PRICE TAG" GATE (Contextual)
    # Catches: "Male shoes 5k" (No 'sell' verb, but price + item)
    search_blockers = r"\b(looking for|need|find|want to buy)\b"
    is_searching = bool(re.search(search_blockers, text_low))

    if price and not has_role(text) and not is_searching:
        candidates.append(("quick_sell", 0.85, {"price": price}))

    return candidates


def merge_candidates(rule_candidates, ml_candidate=None, session_prior=0.6):
    scores = {}
    reasons = {}

    for intent, score, meta in rule_candidates:
        scores[intent] = max(scores.get(intent, 0.0), score)
        reasons.setdefault(intent, []).append(("rule", score, meta))

    if ml_candidate:
        intent_ml, conf_ml = ml_candidate

        if hasattr(intent_ml, "item"):
            intent_ml = intent_ml.item()
        elif isinstance(intent_ml, (list, tuple)) and len(intent_ml) > 0:
            intent_ml = intent_ml[0]

        intent_ml = str(intent_ml)

        scores[intent_ml] = max(scores.get(intent_ml, 0.0), conf_ml)
        reasons.setdefault(intent_ml, []).append(("ml", conf_ml, {}))

    if "provider_onboarding" in scores:
        scores["provider_onboarding"] = blend_confidence(
            scores["provider_onboarding"],
            session_prior
        )

    # 🛡️ SAFETY NET
    if not scores:
        logger.warning("No intent candidates generated")
        return "search", 0.5, []

    best_intent, best_score = max(scores.items(), key=lambda x: x[1])

    return best_intent, best_score, reasons.get(best_intent, [])


def clean_user_text(text):
    """Fix 2: Strips filler and verbal clutter to leave only the 'Gold'."""
    # Remove filler words and stutters
    fillers = r"\b(uhm|err|uhhh|actually|basically|kind\s+of|sort\s+of|please|help|with)\b"
    text = re.sub(fillers, "", text.lower())
    # Clean double spaces
    text = re.sub(r"\s+", " ", text).strip()
    return text


# --- Prototype intent classifier (updated for greeting support) ---
def predict_intent_prototype(text: str, cached_intent=None, cached_lang=None):
    """
    4ound Marketplace Engine (V1.4 - Final Production Grade):
    - SUPPLY-FIRST: Provider intent prioritised over Search.
    - SELF-HEALING: Smart cache override for misclassified providers.
    - NAIJA-READY: Multilingual (Pidgin/Yoruba/Hausa/Igbo) + street-slang.
    - ROBUST ML: Fixed explicit index extraction for string comparison.
    """
    global intent_clf
    lower_text = clean_user_text(text)
    detected_lang = cached_lang or "English"
    global session
    session = session or {}

    # --- STEP 1: SMART CACHE OVERRIDE (Self-Healing Memory) ---
    # 🛡️ THE SELLER PROTECTOR (Added for Quick Sell compatibility)
    is_sell_intent = any(w in lower_text for w in ["sell", "wan sell", "selling"])

    # Added "list" and "post" to the provider triggers
    is_active_provider = any(
        p in lower_text for p in ["i am", "i'm", "my service", "register", "onboard", "list", "post"]
    ) or has_role(lower_text)

    # Special handling for "I sell"
    is_business_sell = re.search(r"\bi sell\b", lower_text) and not re.search(r"\bmy\b",
                                                                              lower_text) and not detect_negation(
        lower_text)

    is_passive_provider = (
            re.search(r"\b(available|services?|for hire|hire me|contact me|here|around)\b", lower_text)
            and has_role(lower_text)
    )

    # 🔥 THE FIX: Include 'is_sell_intent' in the re-evaluation trigger
    if cached_intent in ["search", "other", "greeting"]:
        if is_active_provider or is_passive_provider or is_sell_intent:
            # Re-evaluate because the cache likely contains a mistake (e.g., calling a seller a searcher)
            cached_intent = None

    if cached_intent:
        return cached_intent, 1.0, detected_lang

    # --- STEP 2: 🇳🇬 FULL MULTILINGUAL DETECTION ---
    if any(w in lower_text for w in ["how far", "wetin", "wan sell", "de find"]):
        detected_lang = "Pidgin"
    elif any(w in lower_text for w in ["n wa", "n lẹ", "ṣọọbu", "oníbàárà", "e nle"]):
        detected_lang = "Yoruba"
    elif any(w in lower_text for w in ["ina neman", "sanya", "shagon", "barka", "sannu"]):
        detected_lang = "Hausa"
    elif any(w in lower_text for w in ["achọrọ m", "ebe ị nọ", "nke ikpeazụ", "biko", "nnọọ"]):
        detected_lang = "Igbo"

    # 🔑 INTENT CONTEXT DETECTORS
    is_first_person = any(p in lower_text for p in ["i", "i'm", "i am", "we", "my"])
    is_search_intent = any(w in lower_text for w in ["need", "find", "looking", "search", "buy"])

    # ✅ 🔥 HARD OVERRIDE: SEARCH QUESTIONS  👈 ADD IT HERE
    search_question_patterns = [
        r"where can i get",
        r"where can i find",
        r"where can i buy",
        r"who sells",
        r"anywhere i can get"
    ]

    if any(re.search(p, lower_text, re.IGNORECASE) for p in search_question_patterns):
        logger.info("⚡ Hard Override: Search Intent Detected")
        return "search", 0.99, detected_lang

    # =========================================================
    # ✅ A. QUICK SELL (TOP PRIORITY)
    # =========================================================
    sell_keywords = ["sell", "selling", "for sale", "wan sell", "i get"]

    if is_first_person and any(w in lower_text for w in sell_keywords):
        logger.info("⚡ Hard Override: Quick Sell Detected")

        noise = r"\b(i|want|to|sell|my|an|old|used|clean|fairly|a|pair|of|get|for|sale)\b"
        item_raw = re.sub(noise, "", lower_text).strip()

        loc_bridges = r"\b(?:in|at|near|around|inside|close\s+to|by)\b"
        parts = re.split(loc_bridges, item_raw, maxsplit=1, flags=re.IGNORECASE)
        item_clean = parts[0].strip()

        session.update({
            "query_item": item_clean.title() if item_clean else "Item",
            "mode": "QUICK_SELL_START" if item_clean else "QUICK_SELL_VAGUE",
            "flow": "awaiting_quick_sell_image" if item_clean else "awaiting_quick_sell_item_name",
            "prompt_namespace": "offer_quick"
        })

        return "quick_sell", 0.99, detected_lang

    # =========================================================
    # ✅ B. JOB POSTING (EMPLOYER ONLY)
    # =========================================================

    job_keywords = [
        "hire", "hiring", "employ", "recruit",
        "staff", "employee", "vacancy", "job"
    ]

    job_posting_verbs = [
        "list", "post", "advertise", "publish"
    ]

    is_job_search = any(
        re.search(rf"\b{re.escape(w)}\b", lower_text)
        for w in [
            "find", "looking", "search", "need", "apply"
        ]
    )

    has_job_word = any(
        re.search(rf"\b{re.escape(w)}\b", lower_text)
        for w in job_keywords
    )

    has_posting_verb = any(
        re.search(rf"\b{re.escape(v)}\b", lower_text)
        for v in job_posting_verbs
    )

    is_job_posting = (
            is_first_person
            and (
                    any(
                        re.search(rf"\b{re.escape(w)}\b", lower_text)
                        for w in ["hiring", "hire", "recruit", "vacancy"]
                    )
                    or (has_posting_verb and has_job_word)
            )
            and not is_job_search
    )

    if is_job_posting:
        logger.info("⚡ Hard Override: Job Posting Detected")

        noise = (
            r"\b(i|want|to|list|post|advertise|publish|"
            r"a|an|job|vacancy|for|need|looking|hire|"
            r"staff|employee)\b"
        )

        job_title = re.sub(noise, "", lower_text).strip().title()

        session.update({
            "mode": "RECRUITER_ONBOARDING",
            "item_name": job_title if job_title else "Staff",
            "query_item": job_title if job_title else "Staff",
            "prompt_namespace": "offer_job"
        })

        return "offer_job", 0.99, detected_lang

    # =========================================================
    # ✅ C. BUSINESS ONBOARDING
    # =========================================================
    if (
            is_first_person and
            "business" in lower_text and
            any(w in lower_text for w in ["register", "list", "post", "add", "onboard"])
    ):
        logger.info("⚡ Hard Override: Business Onboarding Detected")
        return "offer", 0.99, detected_lang

    # --- STEP 3: INSTANT WIN GREETINGS ---
    all_greetings = {
        "English": ["hello", "hi", "hey", "sup"],
        "Pidgin": ["how far", "how body", "wetin dey"],
        "Yoruba": ["ẹ n lẹ", "bawo ni"],
        "Hausa": ["sannu", "barka"],
        "Igbo": ["nnọọ", "ndewoo"]
    }
    for lang, words in all_greetings.items():
        if any(lower_text.startswith(g) for g in words):
            return "greeting", 0.9, lang

    # --- STEP 3.5: ⚡ QUICK SELL DETECTION ---
    rule_candidates = []

    # 🛒 Quick Sell
    rule_candidates += rule_quick_sell(lower_text)

    # 🔍 Search Detection
    is_search_pattern = re.search(
        r"\b(looking for|need|find|want|buy|get|search|who can|where is|where can i|get me|show me|nearby|around me|close by|closeby)\b",
        lower_text
    )

    # ✅ ADD THIS BLOCK
    search_keywords = ["find", "need", "looking", "search", "buy", "get"]

    if (
            is_search_pattern
            or any(w in lower_text for w in search_keywords)
    ):
        rule_candidates.append(
            ("search", 0.90, {"search_hint": True})
        )

    # --- PROVIDER / IMPLICIT CANDIDATE ---
    is_active_provider = any(
        p in lower_text for p in ["i am", "i'm", "my service", "register", "onboard", "sell"]
    ) or has_role(lower_text)

    implicit_provider = has_role(lower_text) and any(
        w in lower_text for w in ["work", "jobs", "customers", "client", "business"]
    )

    provider_score = 0.96 if (is_active_provider or implicit_provider) else 0.0

    if provider_score > 0:
        rule_candidates.append(
            ("provider_onboarding", provider_score, {"provider_hint": True})
        )

    # --- SHORT INPUT FILTER ---
    words = lower_text.split()

    if has_role(lower_text) and len(words) <= 2 and not any(
            p in lower_text for p in ["i am", "i'm", "my"]
    ):
        rule_candidates.append(
            ("search", 0.85 * session.get("supply_prior", 1.0), {})
        )

    logger.info(f"DEBUG CANDIDATES: {rule_candidates}")

    # --- MERGE RULE + ML CANDIDATES ---
    chosen_intent, confidence, reasons = merge_candidates(
        rule_candidates,
        None,
        session.get("supply_prior", 0.6)
    )

    # --- STEP 4: 🚨 SHORT INPUT FIRST (The Filter) ---
    # Ensures "mechanic" -> search, but "I am a mechanic" -> onboarding.
    words_in_text = lower_text.split()

    if has_role(lower_text) and len(words_in_text) <= 2:
        if not any(p in lower_text for p in ["i am", "i'm", "my"]):
            score = 0.85 * session.get("supply_prior", 1.0)
            if score > confidence:
                chosen_intent = "search"
                confidence = score

    # --- STEP 5: SEMANTIC PROVIDER FIREWALL (Supply Priority) ---
    is_listing_intent = any(p in lower_text for p in [
        "register my business",
        "list my business",
        "post my business",
        "add my business",
        "onboard my business",
        "register business",
        "list business"
    ])

    implicit_provider = has_role(lower_text) and any(
        w in lower_text for w in ["work", "jobs", "customers", "client", "business"]
    )

    # 🛑 NEW: Add a "Search Sentinel"
    # If the user is using clear search keywords, suppress the provider score
    # regardless of whether a role (e.g., "engineer") was detected.
    search_keywords = ["need", "find", "looking", "wa", "achọrọ"]
    is_search_detected = any(w in lower_text for w in search_keywords) or is_search_pattern

    # 🚀 REFINED LOGIC
    if is_search_detected and not is_listing_intent:
        provider_score = 0
    else:
        provider_score = 0.98 if (
                is_listing_intent or is_active_provider or is_business_sell or implicit_provider or is_passive_provider
        ) else 0

    if provider_score > 0:
        if provider_score > confidence:
            chosen_intent = "provider_onboarding"
            confidence = provider_score

    # Catch "pure service statements" (e.g. "graphic designer here")
    # Catch "pure service statements" (e.g. "graphic designer here")
    if has_role(lower_text) and len(words_in_text) <= 4 and not is_search_pattern:
        if 0.88 > confidence:
            chosen_intent = "provider_onboarding"
            confidence = 0.88

    # --- STEP 6: SEARCH FIREWALL (Demand Fallback) ---
    search_keywords = ["find", "need", "looking", "wa", "neman", "achọrọ"]

    # 🔍 New: Specifically detect if the search is for work/employment
    is_job_search = any(w in lower_text for w in ["job", "employment", "work", "vacancy", "hiring"])

    # 🛡️ THE SELLER PROTECTOR: Detect if the user is trying to list/sell something
    # This prevents "I want to SELL" from being caught by "I want to..."
    is_selling_intent = any(w in lower_text for w in ["sell", "wan sell", "selling", "post", "list"])

    # 🚀 REFINED LOGIC: Only trigger search if it's NOT a selling intent
    search_score = 0.9 if (is_search_pattern or any(
        w in lower_text for w in search_keywords)) and not is_selling_intent else 0

    if search_score > confidence:
        if is_job_search:
            chosen_intent = "search_employment"
            # Optional: Flag remote/online in the session for Firestore filtering
            if any(w in lower_text for w in ["online", "home", "remote"]):
                session['work_type'] = "remote"
        else:
            chosen_intent = "search"
        confidence = search_score


    # --- STEP 8: THE FINAL CONFLICT RESOLVER (The "Employer" Firewall) ---
    # 🛡️ Updated to catch "I have a job offer" or "Giving an offer"
    employer_keywords = ["workers", "employees", "staff", "to hire", "hiring", "vacancy", "job offer", "offering a job"]

    # 💥 THE GUARD: Check if they are looking for a vacancy rather than posting one
    is_searching_vacancies = any(
        w in lower_text for w in ["any", "looking for", "around me", "find", "search", "de find"])

    is_employer = any(w in lower_text for w in employer_keywords) and not is_searching_vacancies

    if is_employer:

        chosen_intent = "recruiter_onboarding"
        confidence = 0.99

        # 🎯 SLOT EXTRACTION: Enhanced noise removal
        # We add "offer" and "job" to noise so it doesn't save as "Job Offer"
        # but extracts the role (e.g., "I have a driver job offer" -> "Driver")
        noise = r"\b(i|am|hiring|need|a|an|want|to|hire|staff|workers|vacancy|for|looking|job|offer)\b"
        job_title = re.sub(noise, "", lower_text).strip().title()

        session.update({
            "mode": "RECRUITER_ONBOARDING",
            "item_name": job_title if job_title else "Staff",
            "query_item": job_title if job_title else "Staff",
            "prompt_namespace": "offer_job"
        })

    elif chosen_intent == "provider_onboarding" and is_job_search:
        chosen_intent = "search_employment"
        session['mode'] = "EMPLOYMENT_SEARCH"

    # --- FINAL SLOT EXTRACTION (V1.4 Global Pro Edition) ---
    # 1. 🛒 QUICK SELL (Move this to the TOP of the logic)
    if chosen_intent == "quick_sell":
        # 1. Strip the initial intent noise
        noise = r"\b(i|want|to|sell|my|an|old|used|clean|fairly|a|female|male|pair|of)\b"
        item_raw = re.sub(noise, "", lower_text).strip()

        # 2. Check if the user was vague
        if not item_raw or len(item_raw) < 2:
            session.update({
                "query_item": "Item",
                "mode": "QUICK_SELL_VAGUE",
                "flow": "awaiting_quick_sell_item_name",
                "prompt_namespace": "offer_quick"
            })
        else:
            # 🎯 ADD THE LOCATION STRIPPER HERE
            loc_bridges = r"\b(?:in|at|wey\s+dey|near|around|inside|close\s+to|side|by)\b"

            # Split and take the first part (the item)
            item_parts = re.split(loc_bridges, item_raw, maxsplit=1, flags=re.IGNORECASE)

            # ✅ REAL FIX: take first element
            item_clean = item_parts[0].strip()

            # ✅ Now save the clean item name
            session.update({
                "query_item": item_clean.title(),
                "mode": "QUICK_SELL_START",
                "flow": "awaiting_quick_sell_image",
                "prompt_namespace": "offer_quick"
            })
            logger.info(f"🛒 Quick Sell Prepared: {item_clean.title()}")

        #chosen_intent = "offer"

    elif chosen_intent == "search":
        # 1. Unified Noise & Bridges
        noise = r"\b(i|am|looking|want|need|find|get|person|are|there|any|someone|who|can|a|an|the|for|me)\b"
        # Parentheses capture the bridge, so it appears in parts
        loc_bridges = r"\b(in|at|wey\s+dey|near|around|inside|close\s+to|side|by)\b"

        # 2. CLEAN FIRST: Remove intent fluff
        clean_text = re.sub(noise, "", lower_text)
        clean_text = re.sub(r"\s+", " ", clean_text).strip()

        # 3. SPLIT BY LOCATION
        parts = re.split(loc_bridges, clean_text, maxsplit=1)

        # With capturing group, parts = [Service, Bridge, Location]
        if len(parts) >= 3:
            service_raw = parts[0].strip()
            location_raw = parts[2].strip()  # skip the bridge word at parts[1]

            session['query'] = service_raw.title() if service_raw else "General"
            session['location'] = location_raw.title() if location_raw else None
        else:
            # Fallback: No bridge detected
            session['query'] = clean_text.title() if clean_text else "General"
            session['location'] = None

        session['mode'] = "MARKET_SEARCH"

    elif chosen_intent == "search_employment":
        noise = r"\b(i|want|need|a|job|work|looking|for|an|online)\b"
        job_raw = re.sub(noise, "", lower_text).strip()
        session['job_query'] = job_raw.title() if job_raw else "General"
        session['mode'] = "EMPLOYMENT_SEARCH"


    elif chosen_intent == "provider_onboarding":

        noise = r"\b(i|do|am|a|an|the|render|sell|deals|in|into|business|of|dey)\b"

        skill_raw = re.sub(noise, "", lower_text).strip()

        # 🟢 Save the specific skill/trade for later use

        session['provider_role'] = skill_raw.title() if skill_raw else "Service"

        session['mode'] = "ONBOARDING_START"

        # 🔑 THE KEY FIX: Convert internal name to the routing name

        chosen_intent = "offer"

    # --- FINAL ROUTING NORMALIZATION ---
    # This ensures your Section 7 "Gatekeeper" sees the right keys
    if chosen_intent == "provider_onboarding":
        chosen_intent = "offer"

    # 🆕 ADD THIS: Map the employer mode to the offer intent
    if chosen_intent == "recruiter_onboarding":
        chosen_intent = "offer_job"  # or just 'offer' if your Section 7 maps offer + is_job

    session["route_intent"] = chosen_intent
    return chosen_intent, confidence, detected_lang



def contains_media(value: dict) -> bool:
    for m in value.get("messages", []) or []:
        if m.get("type") in {"image", "video", "audio", "document", "sticker"}:
            return True
    return False

def detect_sensitive_text(text: str):
    txt = (text or "").lower()
    if any(k in txt for k in MISSING_KEYWORDS):
        return "missing_person"
    if any(k in txt for k in SEXUAL_KEYWORDS):
        return "sexual_content"
    return None

def safe_language_detect(text: str):
    try:
        return detect(text)
    except LangDetectException:
        return "unknown"

# --- Helper 2: rerun pending search after collecting location ---
def rerun_pending_search(from_number: str, query_text: str, filters: dict, phone_number_id: str):
    """
    Runs Firestore search with provided query and location filters.
    Sends results to the user and updates session for awaiting_selection.
    """

    if not query_text:
        session_data = get_session(from_number) or {}
        query_text = session_data.get("last_query")
        if not query_text and session_data.get("last_interaction_id"):
            try:
                doc = db.collection(FIRESTORE_INTERACTIONS).document(session_data["last_interaction_id"]).get()
                if doc.exists:
                    query_text = doc.to_dict().get("query")
            except Exception:
                logger.exception("Failed to fetch query from last interaction")
        if not query_text:
            query_text = "Service"  # fallback default

    results = search_offers_firestore(query=query_text, top_k=3, filters=filters)

    if not results:
        send_message(from_number=from_number, phone_number_id=phone_number_id,
                     text="Sorry, I couldn't find matches for that location. You can try rephrasing your request or register an offer by saying 'I am selling ...'.")
        return

    # Fetch session first
    session = get_session(from_number) or {}

    # Build result lines
    reply_lines = []
    for idx, r in enumerate(results, start=1):
        contact = r.get("contact_whatsapp") or "Contact not shared by provider"
        service_display = r.get("service") or session.get("service") or "Service not specified"
        reply_lines.append(
            f"{idx}. {r['provider_name']} — {service_display} — {r['town']}, {r['state']}\n{r['description']}\nContact: {contact}"
        )

    # Send all results at once
    send_message(phone_number_id, from_number,
                 "Found! Here are the top matches:\n\n" + "\n\n".join(reply_lines))
    send_message(phone_number_id, from_number,
                 "Reply with the number of the result you want (1, 2, or 3), or reply 'none' if none match.")

    # Save session for awaiting selection
    session = get_session(from_number) or {}
    session["flow"] = "awaiting_selection"
    session["awaiting_selection"] = True
    session["last_results"] = results
    session_data = get_session(from_number) or {}
    session["last_interaction_id"] = session_data.get("last_interaction_id") or session.get("last_interaction_id")
    session["updated_at"] = time.time()
    save_session(from_number, session)

    # Log interaction
    try:
        last_interaction_id = session.get("last_interaction_id")
        if last_interaction_id:
            db.collection(FIRESTORE_INTERACTIONS).document(last_interaction_id).update({
                "search_results": [{"doc_id": r["doc_id"], "score": r["score"]} for r in results],
                "used_filters": filters
            })
            log_interaction({
                "whatsapp_number": from_number,
                "action": "presented_results_after_location",
                "presented_doc_ids": [r["doc_id"] for r in results],
                "interaction_ref": last_interaction_id,
                "used_filters": filters
            })
    except Exception:
        logger.exception("Failed to log search results after location")


def handle_search_intent(from_number: str, text: str, phone_number_id: str,
                         session: dict = None, clarify_for_interaction: str = None,
                         location: dict = None):
    """
    Manages the user conversation flow.
    Uses perform_smart_search to handle AI Intent + Foursquare Double-Tap.
    """
    try:
        session = session or {}
        if location:
            session.update(location)

        query_text = text.strip()

        # 1. GPS SHORT-CIRCUIT
        # If the user shared their location, we can skip all location questions!
        active_lat = session.get("latitude")
        active_lng = session.get("longitude")

        if active_lat and active_lng:
            # Execute the Smart Search (AI + Sniper + Double-Tap)
            results = perform_smart_search(
                user_message=query_text,
                lat=active_lat,
                lng=active_lng
            )

            if results:
                present_foursquare_results(phone_number_id, from_number, results)
            else:
                send_message(phone_number_id, from_number,
                             "I couldn't find any matches nearby. Try searching for something else?")

            return True, None

        # 2. TEXT-BASED LOCATION FLOW (If no GPS)
        # Check if we already have the town/city from the message or session
        extracted = extract_global_location(query_text)
        service = extracted.get("service") or query_text

        # Update session with any new location info found in the text
        for key in ["country", "state", "town"]:
            if extracted.get(key):
                session[key] = extracted[key]

        save_session(from_number, session)

        # Ensure we have a full location path before searching via text
        if not session.get("country"):
            send_message(phone_number_id, from_number, "Which country should I search in?")
            return True, None
        if not session.get("state"):
            send_message(phone_number_id, from_number, "Which state?")
            return True, None
        if not session.get("town"):
            send_message(phone_number_id, from_number, "Which town or city?")
            return True, None

        # 3. TEXT SEARCH EXECUTION
        # Now that we have a town, use the Smart Search with location_text
        loc_text = f"{session['town']}, {session['state']}"
        results = perform_smart_search(
            user_message=service,
            location_text=loc_text
        )

        if results:
            present_foursquare_results(phone_number_id, from_number, results)
        else:
            send_message(phone_number_id, from_number, f"I couldn't find any {service} in {loc_text}.")

        return True, None

    except Exception:
        logger.exception("handle_search_intent failed for %s", from_number)
        return False, "Error processing your request."
# -------------------------
# -------------------------
# Firestore setup
# -------------------------
# Firestore client uses GOOGLE_APPLICATION_CREDENTIALS env var
# Thread-safe lazy Firestore client
_FIRESTORE_LOCK = threading.Lock()
_db = None


def save_advert_to_db(ad_data):
    """Saves the advert to the 'active_ads' collection in Firestore."""
    try:
        # 🛠️ FIX 1: Force ID to uppercase for consistent lookups
        ad_id = ad_data.get("ad_id").upper()

        # 🛠️ FIX 2: Create a lowercase title for future case-insensitive searches
        biz_name = ad_data.get("biz_name")
        biz_name_lower = biz_name.lower() if biz_name else ""

        db.collection("active_ads").document(ad_id).set({
            "ad_id": ad_id,
            "biz_name": biz_name,
            "biz_name_lowercase": biz_name_lower,  # For "status [name]" search
            "target_locations": ad_data.get("target_locations"),
            "target_categories": ad_data.get("target_categories"),
            "target_gender": ad_data.get("target_gender"),
            "budgeted_reach": ad_data.get("budgeted_reach"),
            "current_reach": 0,
            "seen_by": [],
            "type": ad_data.get("type"),
            "media_id": ad_data.get("media_id"),
            "body": ad_data.get("body"),
            "is_active": True,
            "created_at": firestore.SERVER_TIMESTAMP
        })
        return True
    except Exception as e:
        logger.error(f"Error saving ad: {e}")  # Using logger instead of print
        return False




def get_db():
    global _db
    if _db is not None:
        return _db

    with _FIRESTORE_LOCK:
        if _db is not None:
            return _db

        project = os.getenv("FIRESTORE_PROJECT")
        key_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        key_json_str = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")

        # 1. Try JSON string from ENV
        if key_json_str:
            logger.info("🔐 Loading Firestore credentials from JSON ENV variable.")
            key_dict = json.loads(key_json_str)
            creds = service_account.Credentials.from_service_account_info(key_dict)
            _db = firestore.Client(project=project or creds.project_id, credentials=creds)

        # 2. Try file path from ENV
        elif key_path and os.path.exists(key_path):
            creds = service_account.Credentials.from_service_account_file(key_path)
            _db = firestore.Client(project=project or creds.project_id, credentials=creds)

        # 3. Fallback to local file
        elif os.path.exists("4oundkey.json"):
            creds = service_account.Credentials.from_service_account_file("4oundkey.json")
            _db = firestore.Client(project=project or creds.project_id, credentials=creds)

        # 4. Final attempt
        else:
            _db = firestore.Client(project=project) if project else firestore.Client()

        return _db


def save_offer_to_firestore(provider_name, description, contact_whatsapp, lat, lng, biz_name=None,
                            entry_type="service"):
    """
    Saves a listing to Firestore. The search engine (RapidFuzz)
    will automatically include this on the next search.
    """
    try:
        # Prepare the Firestore Document
        doc_ref = db.collection(FIRESTORE_OFFERS).document()
        offer_data = {
            "entry_type": entry_type,
            "provider_name": biz_name or provider_name,
            "description": description,
            "contact_whatsapp": contact_whatsapp,
            "latitude": float(lat) if lat else None,
            "longitude": float(lng) if lng else None,
            "indexed": True,
            "created_at": time.time(),
            "updated_at": time.time()
        }

        # Save to Firestore
        doc_ref.set(offer_data)

        logger.info(f"🚀 {entry_type.upper()} Live: {doc_ref.id} for {biz_name or provider_name}")
        return doc_ref.id

    except Exception as e:
        logger.error(f"❌ Failed to save {entry_type}: {e}")
        return None


def search_offers_firestore(query, user_lat=None, user_lng=None, top_k=5,
                            offset=0,
                            category_id=None, entry_type="service",
                            search_key="default", required_types=None):
    # --- STEP 1: RapidFuzz Retrieval ---
    docs = db.collection(FIRESTORE_OFFERS).stream()

    all_docs = []
    names_to_search = []
    for doc in docs:
        d = doc.to_dict()
        d['id'] = doc.id
        all_docs.append(d)
        names_to_search.append(d.get('title') or d.get('biz_name') or d.get('provider_name') or "")

    fuzzy_matches = process.extract(query, names_to_search, scorer=fuzz.WRatio, limit=50)

    candidates = []
    now = datetime.now(timezone.utc)
    score_map = {}

    for match_name, score, index in fuzzy_matches:
        if score < 50: continue

        data = all_docs[index]
        firestore_doc_id = data['id']
        score_map[firestore_doc_id] = score / 100.0
        doc_type = data.get("entry_type") or data.get("listing_type", "service")

        # 1. Type Filtering
        if required_types and doc_type not in required_types: continue

        # 2. Expiry Logic
        expiry = data.get("expires_at") or data.get("expiry_date")
        if expiry:
            try:
                expiry_dt = expiry.to_datetime().replace(tzinfo=timezone.utc) if hasattr(expiry,
                                                                                         "to_datetime") else datetime.fromisoformat(
                    str(expiry).replace("Z", "+00:00"))
                if expiry_dt < now:
                    threading.Thread(target=perform_actual_deletion_silent,
                                     args=(firestore_doc_id, data.get("owner_phone"))).start()
                    continue
            except:
                continue

        # 3. Verification Logic
        if doc_type == "quick_sale" and not data.get("is_verified", False): continue

        # 4. Category/Type Filtering
        doc_category = data.get("category")
        if category_id:
            if category_id == "service" and doc_category not in ["service", "professional", "job", "provider"]:
                continue
            elif str(doc_category).lower() != str(category_id).lower():
                continue

        # ✅ PASSED ALL FILTERS
        candidates.append({
            "base_score": score_map[firestore_doc_id],
            "priority": 1 if data.get("is_verified") else 2,
            "id": firestore_doc_id,
            "category": doc_category,
            "visibility": data.get("visibility"),
            "provider_name": data.get("title") or data.get("biz_name") or "Local Business",
            "lat": data.get("location", {}).get("lat") or data.get("lat"),
            "lng": data.get("location", {}).get("lng") or data.get("lng"),
            "description": data.get("description", ""),
            "entry_type": doc_type,
            "compensation": data.get("compensation"),
            "contact_whatsapp": data.get("contact_phone") or data.get("biz_phone") or data.get("contact_whatsapp"),
            "image_id": data.get("image_id") or data.get("media_id")
        })

    # --- STEP 2: Proximity & Relevance Scoring ---
    scored = []

    # 🎯 Dynamic thresholds by marketplace type
    SCORE_THRESHOLD = 0.60 if entry_type in ["product", "quick_sale"] else (
        0.55 if entry_type in ["job", "quick_job"] else 0.30)

    for c in candidates:
        final_score = c["base_score"]
        logger.info(f"🔍 Candidate: {c.get('provider_name')} | Score: {final_score} | Threshold: {SCORE_THRESHOLD}")
        if final_score < SCORE_THRESHOLD:
            logger.info(f"🚫 Rejected (Score too low): {c.get('provider_name')} | Score: {final_score}")
            continue

        # 1. Product/Service Keyword Logic
        ignore_words = {"looking", "need", "want", "buy", "sell", "nearby", "available", "searching", "find", "show",
                        "around", "near", "close", "area", "please", "help", "give", "some", "with", "job", "work",
                        "vacancy", "hire", "hiring", "employment"}
        query_words = [w for w in query.lower().split() if len(w) >= 3 and w not in ignore_words]

        combined_text = f"{c.get('provider_name', '').lower()} {c.get('description', '').lower()}"
        keyword_overlap = [w for w in query_words if re.search(r'\b' + re.escape(w) + r'\b', combined_text)]

        # Soft relevance boost
        if keyword_overlap:
            boost = 0.45 if entry_type == "quick_job" else 0.15
            final_score += boost  # ✅ RapidFuzz: higher is better
            final_score = min(final_score, 1.0)  # Cap at 1.0

        # 2. Category Boosting
        if category_id and str(c.get("category")) == str(category_id):
            final_score += 0.10  # ✅ Small boost for category match
            final_score = min(final_score, 1.0)

        # 3. Geospatial Scoring
        if user_lat and user_lng and c.get("lat") and c.get("lng"):
            try:
                dist = calculate_distance(float(user_lat), float(user_lng), float(c["lat"]), float(c["lng"]))
                c["distance"] = round(dist, 1)

                if dist <= 1.5:
                    final_score += 0.30  # Very close — big boost
                elif dist <= 5.0:
                    final_score += 0.20
                elif dist <= 15.0:
                    final_score += 0.10
                elif dist <= 50.0 and c.get("visibility") != "Remote Service":
                    final_score -= 0.05  # Mild penalty
                elif dist > 100.0 and c.get("visibility") != "Remote Service":
                    final_score -= 0.30  # Far away — penalty
                final_score = max(0.0, min(final_score, 1.0))  # Keep in 0-1 range
            except:
                c["distance"] = 99999
        else:
            c["distance"] = 99999

        # Filter out low-relevance results
        c["final_score"] = final_score
        scored.append(c)

    # --- STEP 3: Sorting & Pagination ---
    if not scored:
        return [], 0

    # Sort based on marketplace type
    if entry_type in ["product", "quick_sale"]:
        # Relevance first
        scored.sort(key=lambda x: (-x.get("final_score", 0.0), x.get("distance", 99999), x.get("priority", 2)))
    else:
        # Distance first
        scored.sort(key=lambda x: (x.get("distance", 99999), x.get("priority", 2), -x.get("final_score", 0.0)))

    # Pagination logic
    start = offset
    end = offset + top_k

    strategy = "Relevance-First" if entry_type in ["product", "quick_sale"] else "Distance-First"
    logger.info(f"📊 {strategy} Pagination: Returning results {start} to {end} (Total Scored: {len(scored)})")

    return scored[start:end], len(scored)


# -------------------------
# Session helpers (Firestore-backed)
# -------------------------
def get_session(whatsapp_number):
    try:
        db = get_db()
        doc_ref = db.collection(FIRESTORE_SESSIONS).document(whatsapp_number)
        doc = doc_ref.get(timeout=10)

        return doc.to_dict() if doc.exists else {}
    except Exception as e:
        logger.error(f"⚠️ CRITICAL: Firestore Read Failed for {whatsapp_number}: {e}")
        # Raise the error instead of returning empty!
        # This prevents the bot from treating a database crash as a "New User"
        raise Exception("Database unavailable")



def save_session(whatsapp_number, session_obj):
    try:
        db = get_db()
        session_obj["updated_at"] = time.time()
        db.collection(FIRESTORE_SESSIONS).document(whatsapp_number).set(session_obj, merge=True)
        logger.info("Saved session for %s", whatsapp_number)
    except Exception:
        logger.exception("Failed to save session for %s", whatsapp_number)


def clear_session(whatsapp_number):
    """
    Clears flow data but PRESERVES registration, gender, and deduplication history.
    """
    db = get_db()
    doc_ref = db.collection(FIRESTORE_SESSIONS).document(whatsapp_number)
    doc = doc_ref.get()

    if doc.exists:
        data = doc.to_dict()

        preserved_gender = data.get("user_gender") or data.get("gender")

        preserved_data = {
            "gender": preserved_gender,
            "user_gender": preserved_gender,

            "has_agreed_terms": data.get("has_agreed_terms"),
            "is_registered": data.get("is_registered"),

            "detected_lang": data.get("detected_lang", "English"),
            "first_seen": data.get("first_seen"),

            "recent_message_ids": data.get("recent_message_ids", []),
            "sent_message_id": data.get("sent_message_id"),

            "updated_at": time.time(),

            # Reset flow safely
            "flow": None
        }

        doc_ref.set(preserved_data)

    logger.info("Reset session for %s (preserved Identity & dedupe IDs)", whatsapp_number)


# -------------------------
# Interaction logging (for offline learning)
# -------------------------
def log_interaction(record: dict):
    record["timestamp"] = time.time()
    try:
        db = get_db()
        # Ensure serializable types
        emb = record.get("query_embedding")
        if emb is not None:
            try:
                # handle numpy arrays
                import numpy as _np
                if isinstance(emb, _np.ndarray):
                    record["query_embedding"] = emb.tolist()
            except Exception:
                # fallback: if .tolist exists, use it
                if hasattr(emb, "tolist"):
                    record["query_embedding"] = emb.tolist()

        doc_ref = db.collection(FIRESTORE_INTERACTIONS).document()  # deterministic ref
        doc_ref.set(record)
        return doc_ref.id
    except Exception:
        logger.exception("Failed to log interaction to Firestore; falling back to local log")
        logger.info("Interaction: %s", record)
        return None

# -------------------------
# Intent classifier (optional) and prediction helper
# -------------------------
intent_clf = None
model_lock = threading.Lock()



# -------------------------
# Helper: send a reply via Graph API
# -------------------------

def send_message(phone_number_id: str, to_number: str, content: Union[str, dict]):
    """
    Sends either a text message or an interactive payload to WhatsApp.
    'content' can be a simple string or a full dictionary (for Lists/Buttons).
    """
    # Using your existing variables for versioning and tokens
    url = f"https://graph.facebook.com/{FB_API_VERSION}/{phone_number_id}/messages"

    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

    # 1. Build the payload based on the input type
    if isinstance(content, str):
        # Standard text message
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to_number,
            "type": "text",
            "text": {"body": content}
        }
    elif isinstance(content, dict):
        payload = content.copy()  # ← important
        payload["to"] = to_number
    else:
        logger.error("Unsupported content type for send_message: %s", type(content))
        return None

    # 2. Execute the request
    for attempt in range(3):  # Try up to 3 times
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=15)

            logger.info("Graph API response status: %s", resp.status_code)

            if not resp.ok:
                logger.error("Graph API error body: %s", resp.text)
            else:
                logger.info("Graph API success body: %s", resp.text)

            return resp  # Success! Exit the function

        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            if attempt < 2:
                logger.warning(f"⚠️ Network glitch (Attempt {attempt + 1}/3). Retrying...")
                time.sleep(1)  # Wait a second before retrying
            else:
                logger.error(f"❌ Max retries reached for {to_number}: {e}")
                raise
        except Exception:
            logger.exception("Error sending message to %s", to_number)
            raise





def guarded_send(phonenumber_id, to_number, reply_payload, message_id):
    """
    Prevents double-billing and handles both Text and Interactive payloads.
    Returns the JSON response dict on success, or False on failure.
    """
    session = get_session(to_number) or {}

    # 1. Prevent duplicate sends
    if message_id and session.get("sent_message_id") == message_id:
        logger.info("✅ Message %s already confirmed sent.", message_id)
        return True

    try:
        logger.info("🚀 Attempting Graph API send for 4ound: ID=%s", message_id)
        resp = send_message(phonenumber_id, to_number, reply_payload)

        if resp and resp.ok:
            session["sent_message_id"] = message_id
            save_session(to_number, session)

            # ✅ CHANGE: Return the actual JSON dictionary from Meta
            return resp.json()
        else:
            logger.error(
                "❌ Graph API Error %s: %s",
                resp.status_code if resp else "No Response",
                resp.text if resp else "No Body"
            )
            return False

    except Exception as e:
        logger.exception("🔥 Critical failure in guarded_send: %s", e)
        return False


def get_country_by_prefix(phone_number):
    """
    Returns the country name based on the international calling code.
    """
    if phone_number.startswith("234"): return "Nigeria"
    if phone_number.startswith("44"):  return "United Kingdom"
    if phone_number.startswith("1"):   return "United States"
    if phone_number.startswith("27"):  return "South Africa"
    if phone_number.startswith("233"): return "Ghana"
    if phone_number.startswith("254"): return "Kenya"

    return "Nigeria"  # Keep your current home base as the final fallback


def extract_global_location(text=None, from_number=None, context_country=None, lat=None, lng=None):
    """
    Globally extracts geographic context via two paths:
    Path 1: GPS Coordinates -> Address Components (Reverse Geocoding)
    Path 2: Text Phrase -> Service + Address Components (Forward Geocoding)

    NO database side-effects. Returns raw data for the caller (Section 8) to manage.
    """
    GEOCODE_CACHE = {}  # { "lat,lng": result_dict }
    GEOCODE_CACHE_TTL = 3600  # 1 hour
    GEOCODE_CACHE_TIMESTAMPS = {}


    # 🔄 PATH 1: Reverse Geocode from GPS Coordinates (Fixes the Akure Bug)
    if lat and lng:
        cache_key = f"{round(lat, 3)},{round(lng, 3)}"  # Round to ~100m precision
        current_time = time.time()

        # Check cache first
        if cache_key in GEOCODE_CACHE:
            if current_time - GEOCODE_CACHE_TIMESTAMPS.get(cache_key, 0) < GEOCODE_CACHE_TTL:
                logger.info(f"📦 Geocode cache hit: {cache_key}")
                return GEOCODE_CACHE[cache_key]
        try:
            location = geolocator.reverse((lat, lng), addressdetails=True, language='en', timeout=10)
            if location:
                addr = location.raw.get('address', {})

                # Global administrative cascade
                town = (
                        addr.get('suburb') or
                        addr.get('neighbourhood') or
                        addr.get('quarter') or
                        addr.get('city_district') or
                        addr.get('town') or
                        addr.get('city') or
                        addr.get('municipality') or
                        addr.get('county')
                )

                if town:
                    return {
                        "found": True,
                        "town": town,
                        "city": addr.get('city') or addr.get('town') or addr.get('municipality'),
                        "state": addr.get('state') or addr.get('region') or addr.get('province'),
                        "country": addr.get('country'),
                        "country_code": addr.get('country_code', '').upper(),
                        "name": town,
                        "display_name": location.address,
                        "lat": lat,
                        "lng": lng,
                        "service": None
                    }
                    # Store in cache
                    GEOCODE_CACHE[cache_key] = result
                    GEOCODE_CACHE_TIMESTAMPS[cache_key] = current_time
                    return result

        except Exception as e:
            logger.error(f"Reverse geocoding error for coordinates ({lat}, {lng}): {e}")
        return {"found": False}

    # 📝 PATH 2: Text-Based Logic (Forward Geocoding)
    if text:
        cache_key = f"text:{text.lower().strip()}"
        current_time = time.time()

        if cache_key in GEOCODE_CACHE:
            if current_time - GEOCODE_CACHE_TIMESTAMPS.get(cache_key, 0) < GEOCODE_CACHE_TTL:
                logger.info(f"📦 Geocode cache hit: {cache_key}")
                return GEOCODE_CACHE[cache_key]

    if not text:
        return {"country": None, "state": None, "town": None, "service": "Service", "found": False}

    # --- NOISE FILTER ---
    ignored_words = ["hello", "hi", "hey", "test", "ok", "yes", "no", "thanks", "reset", "menu"]
    clean_input = text.lower().strip()

    if clean_input in ignored_words or len(clean_input) < 3:
        return {"country": None, "state": None, "town": None, "service": text, "found": False}

    result = {"country": None, "state": None, "town": None, "service": text, "found": False, "lat": None, "lng": None}

    # Determine country context bias
    home_country = context_country or get_country_by_prefix(from_number)

    # Pivot Logic: Split service from location
    clean_text = text.lower().strip()
    pivot_pattern = r'\b(in|at|near|around|based in|within|close to)\b|[,|-]'
    pivot_match = re.search(pivot_pattern, clean_text)

    service_part = clean_text
    location_part = ""

    if pivot_match:
        pivot_index = pivot_match.start()
        service_part = clean_text[:pivot_index].strip()
        location_part = clean_text[pivot_match.end():].strip()

    search_query = location_part if location_part else clean_text

    if home_country and home_country.lower() not in search_query.lower():
        search_query += f", {home_country}"

    try:
        location = geolocator.geocode(search_query, addressdetails=True, language='en', timeout=10)

        if location:
            addr = location.raw.get('address', {})
            town = (
                    addr.get('suburb') or
                    addr.get('neighbourhood') or
                    addr.get('quarter') or
                    addr.get('city_district') or
                    addr.get('town') or
                    addr.get('city') or
                    addr.get('municipality') or
                    addr.get('county')
            )

            if town:
                result["town"] = town
                result["city"] = addr.get('city') or addr.get('town') or addr.get('municipality')
                result["state"] = addr.get('state') or addr.get('region') or addr.get('province')
                result["country"] = addr.get('country')
                result["country_code"] = addr.get('country_code', '').upper()
                result["lat"] = location.latitude
                result["lng"] = location.longitude
                result["found"] = True
            else:
                result["found"] = False

            # Service Name Cleanup
            if not location_part:
                location_keywords = [result["town"], result["state"], result["country"]]
                for val in location_keywords:
                    if val:
                        pattern = r'\b' + re.escape(val.lower()) + r'\b'
                        service_part = re.sub(pattern, "", service_part, flags=re.IGNORECASE).strip()

    except Exception as e:
        logger.error(f"Global forward geocoding error for query '{search_query}': {e}")

    final_touch = service_part.strip().capitalize() if service_part else "Service"
    result["service"] = final_touch
    result["name"] = result["town"]
    result["display_name"] = location.address if location else None

    return result


def get_targeted_ad(from_number, user_city="EVERYWHERE", user_category="ALL", user_gender="All", user_category_id=None, search_query=None, user_state=None, user_country=None):
    """
    Finds a single best-matched ad using keyword matching, targeting rules,
    and weighted rotation based on remaining budget.
    """
    try:
        ads_ref = db.collection("active_ads").where("is_active", "==", True).stream()

        matched_ads = []

        for doc in ads_ref:
            ad = doc.to_dict()
            doc_id = doc.id

            # Clean categories & locations
            ad_categories = [str(c).lower().strip() for c in ad.get("target_categories", []) if c and str(c).strip()]
            ad_locations = [str(l).lower().strip() for l in ad.get("target_locations", []) if l and str(l).strip()]

            # 🛑 Rule A: Reach Check
            current_reach = ad.get("current_reach", 0)
            budgeted_reach = ad.get("budgeted_reach", 0)

            try:
                budgeted_reach = int(budgeted_reach)
            except (ValueError, TypeError):
                budgeted_reach = 999999

            if current_reach >= budgeted_reach:
                db.collection("active_ads").document(doc_id).update({"is_active": False})
                continue

            # 🛑 Rule B: Anti-Spam
            if from_number in ad.get("seen_by", []):
                continue

            # 🎯 Rule C: Targeting Match
            # 🎯 Rule C: Targeting Match (Multi-level location awareness)
            user_city_low = str(user_city or "EVERYWHERE").lower()
            user_state_low = str(user_state or "").lower()
            user_country_low = str(user_country or "").lower()

            # Build a combined location string for matching
            # This allows an ad targeting "Abuja" to match users in "Wuse 2, Abuja"
            location_context = " ".join(filter(None, [
                user_city_low,
                user_state_low,
                user_country_low
            ]))

            loc_match = (
                    "everywhere" in ad_locations or
                    any(loc in location_context for loc in ad_locations) or
                    any(loc in user_city_low for loc in ad_locations) or
                    any(loc in user_state_low for loc in ad_locations) or
                    any(loc in user_country_low for loc in ad_locations)
            )

            # Gender Match
            ad_gen = str(ad.get("target_gender", "All")).capitalize()
            usr_gen = str(user_gender or "All").capitalize()
            gen_match = ad_gen == "All" or ad_gen == usr_gen

            # Category Match (keyword-aware)
            user_cat_low = str(user_category or "ALL").lower()
            query_words = [w.strip() for w in (search_query or "").lower().split() if w.strip()]

            cat_match = (
                "all" in ad_categories or
                user_cat_low in ad_categories or
                (user_category_id and str(user_category_id).lower() in ad_categories) or
                any(word in ad_categories for word in query_words)
            )

            if loc_match and gen_match and cat_match:
                matched_ads.append((doc_id, ad))

        # 🎯 No matches found
        if not matched_ads:
            logger.info(f"📭 No matching ad found for {from_number} | city={user_city} | query={search_query}")
            return None

        # 🎲 WEIGHTED ROTATION: Ads with more remaining budget get more impressions
        weights = [
            max(1, int(ad.get("budgeted_reach", 1)) - int(ad.get("current_reach", 0)))
            for _, ad in matched_ads
        ]

        doc_id, ad = random.choices(matched_ads, weights=weights, k=1)[0]

        # 📊 Update reach and seen_by
        db.collection("active_ads").document(doc_id).update({
            "current_reach": firestore.Increment(1),
            "seen_by": firestore.ArrayUnion([from_number])
        })

        logger.info(f"🎯 Ad selected: {ad.get('ad_id') or doc_id} | city={user_city} | query={search_query} | remaining_budget={weights[matched_ads.index((doc_id, ad))]}")
        return ad

    except Exception as e:
        logger.error(f"❌ Error fetching ad: {e}")
        return None



def finalize_listing_to_db(from_number, listing_doc, user_session):
    try:
        # 🛡️ THE IDENTITY FALLBACK
        # Ensure we have a string for title
        raw_title = listing_doc.get("title")

        # If it's truly empty, give it a context-aware fallback
        if not raw_title:
            listing_type = listing_doc.get("listing_type")
            if listing_type == "quick_sale":
                clean_title = "Item for Sale"
            elif listing_type == "quick_job":
                clean_title = "Job Opening"
            else:
                clean_title = "Professional Service"
        else:
            clean_title = raw_title

        # Update the doc with our guaranteed string
        listing_doc["title"] = clean_title

        # 1. Technical Metadata
        listing_type = listing_doc.get("listing_type", "quick_sale")

        # 🆕 SYMMETRIC MAPPING
        # This matches the filter logic you implemented in Step 1
        mapping = {
            "quick_sale": "product",
            "quick_job": "job",
            "professional": "service"
        }

        # Only "quick_sale" items need the pending queue for image checks
        needs_review = (listing_type == "quick_sale")

        # Capture contact number BEFORE owner_phone gets overwritten
        contact_phone = listing_doc.get("owner_phone") or user_session.get("biz_phone")

        listing_doc.update({
            "owner_phone": from_number,
            "user_id": from_number,
            "contact_phone": contact_phone,
            "created_at": user_session.get("created_at") or firestore.SERVER_TIMESTAMP,
            "is_active": True,
            "is_verified": not needs_review,
            "verification_status": "pending" if needs_review else "approved",
            "indexed": True,
            "current_reach": 0,
            "target_reach": 1000,
            "category": mapping.get(listing_type, "service")
        })

        # --- 🛡️ SAFETY NET: Add Expiry Check ---
        if "expires_at" not in listing_doc or listing_doc["expires_at"] is None:
            listing_type = listing_doc.get("listing_type")

            if listing_type == "quick_job":
                # For Quick Jobs (14 Days)
                listing_doc["expires_at"] = (datetime.now(timezone.utc) + timedelta(days=14)).isoformat()
            elif listing_type == "quick_sale":
                # For Quick Sales (7 Days)
                listing_doc["expires_at"] = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
            # Professional stays None (Eternal)

        # 3. Final Firestore Commit
        doc_id = (listing_doc.get("listing_id") or str(uuid.uuid4())[:8]).upper()
        listing_doc["listing_id"] = doc_id

        # Commit to Firestore
        db.collection(FIRESTORE_OFFERS).document(doc_id).set(listing_doc)

        # 🚀 4. TRIGGER THE SMART SAFETY CHECK
        image_id = listing_doc.get("image_id")
        if image_id:
            start_safety_check_thread(doc_id, image_id)

        display_name = listing_doc.get("title", "Listing")

        # --- 🟢 NEW: TARGETED AD INJECTION ---
        user_city = user_session.get("location", "EVERYWHERE")
        user_gender = user_session.get("gender", "All")
        user_cat = listing_doc.get("category", "ALL")

        targeted_ad = get_targeted_ad(
            from_number,
            user_city,
            user_cat,
            user_gender,
            user_state=user_session.get("state"),
            user_country=user_session.get("country")
        )

        review_note = "Our safety AI is reviewing your photo." if image_id else "Your business is now live."

        # 💡 UPDATE: Added \n\n for better spacing between sections
        success_msg = (
            f"✅ *{display_name}* has been submitted!\n\n"
            f"🆔 *Ad ID:* `{doc_id}`\n\n"
            f"🛡️ {review_note} It usually takes a few minutes "
            f"to go live in search results.\n\n"
            f"📈 *Quick Status:* Type *'status {doc_id}'* to see your views.\n\n"
            f"⚙️ *Manage:* Type *'manage listings'* to see or delete all your posts. 🚀"
        )

        return success_msg

    except Exception as e:
        logging.error(f"Error finalizing to DB: {e}")
        return "⚠️ Technical glitch. Please type 'reset' and try again."


def deliver_ad(phone_number_id, from_number, ad_data, message_id):
    """
    Handles the delivery of the ad and updates reach metrics in Firestore.
    """
    raw_content = ad_data.get("body") or ad_data.get("content") or ""
    media_id = ad_data.get("media_id") or ad_data.get("image_id")
    ad_type = ad_data.get("type", "text")
    ad_id = ad_data.get("ad_id") # We need this for the DB update

    # 🏷️ THE SPONSORED LABEL
    sponsored_label = "✨ *Featured* | "
    final_content = f"{sponsored_label}{raw_content}"

    try:
        # 1. SEND THE MESSAGE
        if ad_type == "image" and media_id:
            send_whatsapp_image(phone_number_id, from_number, media_id, final_content)
        else:
            guarded_send(phone_number_id, from_number, final_content, message_id)

        # 2. 📈 THE REACH INCREMENTER (NEW)
        if ad_id:
            # We use an 'Atomic Increment' so the count is 100% accurate
            ad_ref = db.collection("active_ads").document(ad_id)
            ad_ref.update({
                "current_reach": firestore.Increment(1),
                "seen_by": firestore.ArrayUnion([from_number]) # Prevents spamming the same user
            })
            logger.info(f"📢 Ad {ad_id} Delivered & Reach Incremented for {from_number}")

    except Exception as e:
        logger.error(f"❌ Failed to deliver or track ad {ad_id}: {e}")



def calculate_distance(lat1, lon1, lat2, lon2):
    """
    Calculate the great circle distance in kilometers between two points
    on the earth (specified in decimal degrees).
    """
    if None in [lat1, lon1, lat2, lon2]:
        return None

    # convert decimal degrees to radians
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])

    # haversine formula
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = math.sin(dlat/2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon/2)**2
    c = 2 * math.asin(math.sqrt(a))
    r = 6371 # Radius of earth in kilometers. Use 3956 for miles
    return c * r



def send_location_request(recipient_number: str, body_text: str) -> dict:
    """
    Sends a plain text location request — works universally across all accounts.
    """
    # 🧼 Clean number
    clean_number = re.sub(r"\D", "", recipient_number.strip())
    if clean_number.startswith("0") and len(clean_number) == 11:
        clean_number = f"234{clean_number[1:]}"

    api_version = globals().get("FB_API_VERSION", "v17.0")
    phone_id = globals().get("PHONE_NUMBER_ID")
    access_token = globals().get("ACCESS_TOKEN") or globals().get("WHATSAPP_TOKEN")

    if not phone_id or not access_token:
        logger.error("❌ Configuration Error: PHONE_NUMBER_ID or ACCESS_TOKEN missing.")
        return {"error": "Missing API configuration credentials"}

    url = f"https://graph.facebook.com/{api_version}/{phone_id}/messages"

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    # ✅ Interactive Payload — Triggers the native location picker
    payload = {
        "messaging_product": "whatsapp",
        "to": clean_number,
        "type": "interactive",
        "interactive": {
            "type": "location_request_message",
            "body": {
                "text": body_text
            },
            "action": {
                "name": "send_location"
            }
        }
    }

    try:
        logger.info(f"📍 Dispatching location request to: {clean_number}")
        response = requests.post(url, json=payload, headers=headers, timeout=5.0)

        if response.status_code != 200:
            logger.error(f"❌ Meta API Error {response.status_code}: {response.text}")

        response.raise_for_status()
        res_data = response.json()

        # ✅ Fixed: messages is a list, access index 0
        messages = res_data.get('messages', [])
        msg_id = messages[0].get('id', 'N/A') if messages else 'N/A'
        logger.info(f"✅ Location request sent. Message ID: {msg_id}")
        return res_data

    except requests.exceptions.Timeout:
        logger.error(f"⏳ Timeout sending location request to {clean_number}.")
        return {"error": "Meta API request timed out"}

    except Exception as e:
        logger.error(f"💥 Failed to send location request to {clean_number}: {e}")
        return {"error": str(e)}


def get_foursquare_data(query=None, category_id=None, location_text=None, lat=None, lng=None):
    """
    4ound Unified Search: Supports Query, Category, GPS, and Text.
    Uses official Foursquare Category Taxonomy dataset mappings for high precision results.
    """
    api_key = os.getenv("FOURSQUARE_SERVICE_KEY") or os.getenv("FOURSQUARE_API")
    if not api_key:
        logger.error("❌ Foursquare API key missing.")
        return []

    url = "https://places-api.foursquare.com/places/search"

    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key.strip()}",
        "X-Places-Api-Version": "2025-06-17"
    }

    params = {
        "fields": "fsq_place_id,name,location,tel,distance,categories,latitude,longitude",
        "limit": 5,
        "radius": 10000
    }

    if query:
        params["query"] = query.strip()

    # --- 🟢 TRANSLATION LAYER: Map 4ound tags to Taxonomies ---
    FSQ_CATEGORY_MAP = {
        # 🍔 FOOD, DRINKS & DINING
        "food": "56aa371be4b08b9a8d573550,63be6904847c3692a84b9b46",
        "restaurant": "56aa371be4b08b9a8d573550",
        "cafe": "4bf58dd8d48988d18d941735,4bf58dd8d48988d1f0941735",
        "bakery": "56aa371be4b08b9a8d573550",

        # 🛠️ SERVICES, ARTISANS & TRADES
        "service": "5453de49498eade8af355881",
        "plumbing": "63be6904847c3692a84b9b5f",
        "electrician": "63be6904847c3692a84b9b52",
        "carpenter": "63be6904847c3692a84b9b4d",
        "mechanic": "52f2ab2ebcbc57f1066b8b44",
        "cleaning": "63be6904847c3692a84b9b60",
        "laundry": "4bf58dd8d48988d1fc941735,52f2ab2ebcbc57f1066b8b33",
        "barber": "63be6904847c3692a84b9b49",
        "salon": "4bf58dd8d48988d110951735,4f04aa0c2fb6e1c99f3db0b8",

        # 🏢 BUSINESSES, COMMERCIAL ENTITIES & FALLBACKS
        "business": "4d4b7105d754a06375d81259",
        "job": "52f2ab2ebcbc57f1066b8b57",
        "office": "4bf58dd8d48988d124941735",
        "startup": "4bf58dd8d48988d125941735",
        "construction": "63be6904847c3692a84b9b36",

        # 🛒 RETAIL & PRODUCTS
        "product": "52f2ab2ebcbc57f1066b8b38",
        "atm": "52f2ab2ebcbc57f1066b8b56",
        "bank": "4bf58dd8d48988d10a951735",

        # ✅ 5. HIGH RESOLUTION ELECTRONICS & MOBILE TAIL VARIATIONS
        "phone": "4bf58dd8d48988d122951735",
        "iphone": "4bf58dd8d48988d122951735",
        "android": "4bf58dd8d48988d122951735",
        "electronics": "4bf58dd8d48988d122951735",
        "laptop": "4bf58dd8d48988d1f6941735"
    }

    # --- 🎯 SMART CATEGORY FILTERS ---
    mapped_cat = None

    # Check A: See if the keyword query string itself matches an entry directly
    if query:
        mapped_cat = FSQ_CATEGORY_MAP.get(query.lower().strip())

    # Check B: Fallback to evaluating the structural fallback token input
    if not mapped_cat and category_id:
        mapped_cat = FSQ_CATEGORY_MAP.get(category_id.lower().strip())

    # Check C: Set endpoint constraint values dynamically
    if mapped_cat:
        params["categories"] = mapped_cat
        logger.info(f"🎯 Applied taxonomy constraint: '{mapped_cat}'")
    else:
        # Avoid forcing arbitrary fallbacks like "11000" or "13000" when it isn't matched.
        # Keeping parameters empty forces text engine relevancy processing.
        logger.info("ℹ️ No static mapping matched. Utilizing pure context keyword processing.")

    # Location Constraint Management
    if lat and lng:
        params["ll"] = f"{lat},{lng}"
        params["sort"] = "DISTANCE"
    elif location_text:
        params["near"] = location_text.strip()
        params["sort"] = "RELEVANCE"
    else:
        logger.warning("⚠️ Location telemetry metrics missing.")
        return []

    try:
        # Clean parameter matrix configuration maps before execution
        params = {k: v for k, v in params.items() if v is not None}

        resp = requests.get(url, params=params, headers=headers, timeout=20)
        logger.info(f"📡 API Transaction Code: {resp.status_code} | Payload Context: {params}")

        if resp.status_code == 200:
            results = resp.json().get("results", [])
            logger.info(f"✅ Found {len(results)} valid business opportunities.")
            processed = []

            for place in results:
                name = place.get("name") or "Nearby Business"

                distance = place.get("distance")
                if not isinstance(distance, (int, float)) or distance < 0:
                    distance = 999999

                tel = place.get("tel")
                location = place.get("location", {})

                street = location.get("address")
                locality = location.get("locality")
                region = location.get("region")
                country_code = location.get("country", "NG")

                country_map = {
                    "NG": "Nigeria", "GH": "Ghana", "KE": "Kenya",
                    "US": "USA", "GB": "UK", "ZA": "South Africa"
                }
                country_name = country_map.get(country_code, country_code)

                address_parts = [part for part in [street, locality, region] if part]
                address = ", ".join(address_parts) if address_parts else f"{locality or 'Nearby'}, {country_name}"

                lat_val = place.get("latitude")
                lng_val = place.get("longitude")
                maps_link = None
                if lat_val is not None and lng_val is not None:
                    maps_link = f"https://www.google.com/maps/search/?api=1&query={lat_val},{lng_val}"

                wa_link = None
                if tel:
                    clean_phone = "".join([char for char in tel if char.isdigit()])

                    # 🛑 STEP 1: Fast-fail known garbage placeholder structures
                    garbage_placeholders = {"1234567890", "0000000000", "1111111111", "9876543210"}
                    if clean_phone in garbage_placeholders:
                        clean_phone = None

                    if clean_phone:
                        if country_code == "NG":
                            # Standard Nigerian mobile normalization
                            if clean_phone.startswith("0") and len(clean_phone) == 11:
                                clean_phone = "234" + clean_phone[1:]
                            elif len(clean_phone) == 10 and clean_phone.startswith(('7', '8', '9')):
                                clean_phone = "234" + clean_phone
                        else:
                            # 🌍 STEP 2: Strict International Confidence Guard
                            # If a foreign number doesn't have an international-length dial track
                            # or is too short to contain a real country prefix, discard it.
                            if len(clean_phone) <= 10:
                                clean_phone = None
                            elif clean_phone.startswith("0"):
                                # Strip localized trunks safely
                                clean_phone = clean_phone[1:]

                    # 🎯 STEP 3: Safe Link Dispatch
                    if clean_phone and 10 <= len(clean_phone) <= 15:
                        msg = f"Hi {name}, I found your business on 4ound. Are you available for service?"
                        wa_link = f"https://wa.me/{clean_phone}?text={urllib.parse.quote(msg)}"

                processed.append({
                    "id": place.get("fsq_place_id"),
                    "provider_name": name,
                    "address": address,
                    "contact_whatsapp": wa_link,
                    "maps_link": maps_link,
                    "distance": distance,
                    "priority": 2,
                    "source": "foursquare"
                })

            return processed

        logger.error(f"❌ Foursquare execution failure: {resp.status_code} - {resp.text}")
        return []

    except requests.exceptions.RequestException as e:
        logger.error(f"⚠️ Networking runtime disruption: {e}")
        return []
    except Exception as e:
        logger.exception(f"⚠️ Core integration fault: {e}")
        return []


def send_whatsapp_image(phone_number_id, to_number, image_id, caption=None):
    """
    Sends an image to a user via the WhatsApp Business API using a media ID.
    """
    url = f"https://graph.facebook.com/v18.0/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }

    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "image",
        "image": {
            "id": image_id
        }
    }

    if caption:
        payload["image"]["caption"] = caption

    response = requests.post(url, json=payload, headers=headers)
    return response.json()


def present_foursquare_results(phone_number_id, from_number, results, message_id, targeting_meta=None,
                               item_name=None):
    session = get_session(from_number)
    detected_lang = session.get("detected_lang", "English")

    if not results:

        # 🧠 Human-friendly fallback resolution
        human_query = (
                item_name
                or session.get("query")
                or session.get("pending_search")
                or session.get("last_search_query")
                or "that"
        )

        bad_values = ["results", "result", "item", "something", "search"]

        if isinstance(human_query, str) and human_query.lower() in bad_values:
            human_query = "that"

        no_res = get_local_response(
            "search",
            human_query,
            sub_key="no_results",
            language=detected_lang
        )

        guarded_send(
            phone_number_id,
            from_number,
            no_res,
            message_id
        )

        # 👇 HELP HINT AFTER FAILURE
        help_hint = (
            "💡 *Try searching differently:*\n"
            "• I need a plumber\n"
            "• Looking for shoes\n"
            "• Chef jobs nearby\n"
            "• Find me a mechanic\n\n"
            "📍 You can also send a location pin.\n"
            "Type *help* for more examples."
        )

        guarded_send(
            phone_number_id,
            from_number,
            help_hint,
            f"{message_id}_help"
        )

        return

    # 1. Sort: Verified first (Priority 1), then Foursquare/Local (Priority 2)
    # No sorting here; we trust the order from perform_smart_search
    current_offset = session.get("search_offset", 0)

    # 2. Initial Header (Short and clean)
    if current_offset == 0:
        header_text = get_local_response("search", item_name, sub_key="search_header", language=detected_lang)
        guarded_send(phone_number_id, from_number, header_text, f"{message_id}_header")

    # 3. Individual Result Cards (Preview)
    page_results = results

    for idx, res in enumerate(page_results, start=current_offset + 1):
        unique_msg_id = f"{message_id}_res_{idx}"

        try:
            # 🟢 CHANGE 1: Use the keys you just defined in get_foursquare_data
            listing_id = res.get("id")
            source = res.get("source", "local")
            is_verified = res.get("priority") == 1

            # Use 'provider_name' (from FSQ) or 'title' (from Local)
            raw_name = res.get("provider_name") or res.get("title") or res.get("name") or "Item"

            # 🟢 CHANGE 2: Get the pre-formatted address and links
            # 🟢 SMART ADDRESS LOGIC
            db_addr = res.get("address")
            visibility = res.get("visibility", "On-site")
            # Check both top-level and nested location object
            lat = res.get("lat") or res.get("location", {}).get("lat")
            lng = res.get("lng") or res.get("location", {}).get("lng")

            if db_addr and str(db_addr).lower() != "null":
                addr = db_addr
            elif visibility == "On-site":
                # Use the city from meta or a generic 'Nearby'
                city_label = targeting_meta.get("city", "Nearby") if targeting_meta else "Nearby"
                addr = f"{city_label.title()} (Exact address on request)"
            elif visibility == "Remote":
                addr = "Remote / Online"
            else:
                addr = "Location hidden"

            wa_link = res.get("contact_whatsapp")
            maps_link = res.get("maps_link")

            # ✅ FIXED: Standardized kilometer layout metric evaluation
            dist = res.get("distance")
            try:
                d_val = float(dist)
            except (TypeError, ValueError):
                d_val = None

            if d_val is not None and d_val < 99999:
                # If distance is less than 1km (e.g. 0.35), show as clean meters (350m)
                if d_val < 1.0:
                    dist_text = f" ({int(d_val * 1000)}m)"
                else:
                    dist_text = f" ({d_val:.1f}km)"
            else:
                dist_text = ""

            # --- THE UI DISPATCHER ---
            if is_verified and listing_id:
                # 🟢 PATH A: LOCAL (Verified) - UPDATED TO BE FULL/RICH

                # 1. Pull the new fields from Firestore
                desc = res.get("description", "")
                # Use 'compensation' (which now stores ₦5k/hr)
                price_val = res.get("compensation") or res.get("price") or "Contact for Quote"

                # 2. Format the display components
                desc_text = f"\n📝 _{desc}_" if desc else ""
                # We trust the 'addr' we calculated in Step 1
                addr_text = f"\n📍 {addr}"

                # 3. Build the Rich Preview
                rich_preview = (
                    f"✅ *{raw_name}*{dist_text}"
                    f"{desc_text}"
                    f"{addr_text}"
                    f"\n💰 *Rate:* {price_val}"
                )

                send_interactive_button(
                    phone_number_id,
                    from_number,
                    rich_preview,  # 👈 Send the full info here
                    buttons_list=[{"id": f"view_{listing_id}", "title": "View Details 👁️"}],
                    message_id=unique_msg_id
                )
            else:
                # 🌐 PATH B: FOURSQUARE (Full Text Card)
                links_text = ""

                if wa_link:
                    links_text += f"\n🟢 *WhatsApp:* {wa_link}"

                if maps_link:
                    links_text += f"\n🗺️ *Directions:* {maps_link} _(nearby location)_"

                full_card = f"*{idx}. {raw_name}*{dist_text}\n🏢 {addr}{links_text}"

                guarded_send(phone_number_id, from_number, full_card, unique_msg_id)

        except Exception as e:
            logger.error(f"Error processing loop result {idx}: {e}")

        # ----------------------------------------

    # --- REVISED CAPTION LOGIC (SMART PAGINATION VERSION) ---

    # ✅ ALWAYS use index 0 because 'results' is already the sliced current page
    top_result = results[0] if results else {}

    if top_result.get("source") == "local":

        image_id = (
                top_result.get("media", {}).get("id")
                or top_result.get("image_id")
        )

        if image_id:
            clean_top_name = (
                    top_result.get("title")
                    or top_result.get("provider_name")
                    or top_result.get("name")
                    or "Item"
            )

            dist = top_result.get("distance", 0)

            # Convert to a pretty label (meters if < 1.0, otherwise km)
            try:
                td_val = float(dist)
                if td_val < 1.0:
                    dist_label = f"{int(td_val * 1000)}m"
                else:
                    dist_label = f"{td_val:.1f}km"
                dist_label += " away"
            except:
                dist_label = "nearby"

            caption = f"⭐ Top Match: {clean_top_name} ({dist_label} away)"

            send_whatsapp_image(
                phone_number_id,
                from_number,
                image_id,
                caption
            )


    # 7. PAGINATION (Optimized for Pre-Sliced Results)

    # If we got a full batch (e.g., 3), assume there are more to show
    total_count = (targeting_meta or {}).get("total_count", 0)
    has_more = (current_offset + len(results)) < total_count

    if len(results) >= 3 and has_more:
        next_offset = current_offset + 3

        # Since we don't know the 'total_count' anymore (to save memory/speed),
        # we use a catchy, generic call-to-action.
        more_text = f"There's more to see! Click below to keep exploring *4ound* 👇"

        send_interactive_button(
            phone_number_id,
            from_number,
            more_text,
            buttons_list=[
                {
                    "id": f"more_results_{next_offset}",
                    "title": "See More 🔄"
                }
            ],
            message_id=f"{message_id}_more"
        )

        logger.info(
            f"➕ Added More button for {from_number} (Next Offset: {next_offset})"
        )

        # 🆘 Persistent Help Hint
        help_footer = (
            "🆘 *Quick Help*\n"
            "• Type *help* for commands\n"
            "• Type *manage listings* to manage your posts"
        )

        guarded_send(
            phone_number_id,
            from_number,
            help_footer,
            f"{message_id}_help"
        )

    return True


def search_jobs_firestore(query, user_lat, user_lng, radius_km=50, offset=0, top_k=3):
    """
    Queries the unified 'listings' collection for active jobs near the user.
    Now supports pagination via offset and top_k.
    """
    try:
        listings_ref = db.collection("listings")
        now = datetime.now(timezone.utc)

        # 1. Fetch a pool of candidates
        # Increasing the limit to 100 ensures we don't miss closer jobs
        # that might be sitting further down the Firestore index.
        query_ref = (listings_ref
                     .where("category", "==", "job")
                     .where("is_verified", "==", True)
                     .where("expires_at", ">", now)
                     .limit(100))

        results = []
        docs = query_ref.stream()

        search_term = query.lower().strip()

        for doc in docs:
            job_data = doc.to_dict()
            job_title = job_data.get("title", "").lower()
            job_desc = job_data.get("description", "").lower()

            # Flexible keyword matching
            # Split search term into meaningful words, ignore generic noise
            ignore = {"job", "work", "vacancy", "employment", "hiring"}
            search_words = [w for w in search_term.split() if w not in ignore]

            if search_words:
                # Match if any meaningful keyword appears in title or description
                is_match = any(
                    w in job_title or w in job_desc
                    for w in search_words
                )
            else:
                # Query was only generic words like "job" — return all jobs
                is_match = True

            if is_match:
                loc_map = job_data.get("location", {})
                job_lat = loc_map.get("lat") or job_data.get("lat")
                job_lng = loc_map.get("lng") or job_data.get("lng")

                if job_lat and job_lng:
                    dist = calculate_distance(user_lat, user_lng, job_lat, job_lng)

                    if dist <= radius_km:
                        # 🟢 UNIFY DATA STRUCTURE
                        # We create a new dictionary with specific keys that your
                        # display function (present_foursquare_results) expects.
                        results.append({
                            "id": doc.id,
                            "priority": 1,  # Jobs are verified by default in this flow
                            "distance": round(dist, 2),
                            "provider_name": job_data.get("job_title") or job_data.get("title") or "Job Opening",
                            "compensation": job_data.get("compensation") or job_data.get("salary") or "Negotiable",
                            "address": job_data.get("address") or "Location on request",
                            "lat": job_lat,
                            "lng": job_lng,
                            "description": job_data.get("description", ""),
                            "source": "local",
                            "visibility": job_data.get("visibility", "On-site"),
                            "image_id": job_data.get("image_id") or job_data.get("media_id")
                        })

        # 2. Sort by distance (closest jobs first)
        # Using 99999 for the fallback ensures jobs without coordinates
        # sink to the bottom of the list rather than floating at the top.
        results.sort(key=lambda x: x.get("distance", 99999))

        # 🚀 3. THE PAGINATION UPDATE
        # Calculate the slice based on the current offset
        start = offset
        end = offset + top_k

        # Slice the list to return only the requested "page"
        paginated_results = results[start:end]

        total_found = len(results)
        paginated_results = results[start:end]
        logger.info(f"🔎 Jobs: Found {total_found} total. Returning slice {start}:{end} for '{query}'")
        return paginated_results, total_found

    except Exception as e:
        logger.error(f"❌ Error searching jobs in Firestore: {e}")
        return [], 0


def clean_for_display(text):
    if not text:
        return ""

    text = text.lower().strip()

    prefixes = [
        "i need",
        "i want",
        "i am looking for",
        "i'm looking for",
        "where can i get",
        "can i find",
        "show me",
        "find me",
        "i'm searching for",
        "search for",
        "looking for"
    ]

    pattern = r"^(" + "|".join(prefixes) + r")\s+"
    text = re.sub(pattern, "", text, flags=re.IGNORECASE)

    # Remove shopping/search fluff
    fluff = [
        r"\bto buy\b",
        r"\bfor sale\b",
        r"\bnear me\b",
        r"\baround here\b",
        r"\bplease\b"
    ]

    for f in fluff:
        text = re.sub(f, "", text, flags=re.IGNORECASE)

    text = re.sub(r"\s+", " ", text).strip()

    return text


def perform_smart_search(smart_data, lat, lng):
    # 1. Grab the raw input
    item = smart_data.get("item")
    raw_query = smart_data.get("search_query") or item

    # 2. CREATE THE SPLIT
    semantic_query = raw_query
    display_query = clean_for_display(raw_query)

    # 3. Log the "pretty" version
    logger.info(f"🔍 Processing search: {display_query}")

    # 4. Use the semantic_query for the rest of the logic
    search_query = semantic_query

    # --- 🆕 DISPATCHER OVERRIDE: Active Routing ---
    predicted_intent = smart_data.get("predicted_intent", "search")
    derived_category = resolve_category(predicted_intent, search_query)

    # Set the cat_id explicitly: AI resolver first, then upstream fallbacks
    cat_id = derived_category or smart_data.get("category_filter") or smart_data.get("category_id")

    # 🆕 THE SEMANTIC GUARD:
    product_triggers = ["shoes", "clothes", "sneakers", "bags", "laptop", "watch"]
    if any(word in search_query.lower() for word in product_triggers):
        if cat_id != "product":
            logger.info(f"🛡️ Guard: Overriding category '{cat_id}' to 'product' for query '{search_query}'")
            cat_id = "product"
            # Removed: search_key = "default"

    logger.info(
        f"⚡ Dispatcher Active Routing: Intent='{predicted_intent}' | Query='{search_query}' | Forced Category='{cat_id}'")

    # 🆕 THE BREAKOUT: Force exit from Job Mode if AI detects product/service intent
    mode = smart_data.get("mode", "MARKET_SEARCH")
    if mode == "EMPLOYMENT_SEARCH" and cat_id in ["product", "service", "vendor"]:
        logger.info(f"🔄 BREAKOUT: Redirecting '{search_query}' (Category: {cat_id}) to Market Search")
        mode = "MARKET_SEARCH"

    # 🆕 ADD THIS LOGGING BLOCK HERE
    from_num = smart_data.get("from_number")
    offset = smart_data.get("offset", 0)

    if from_num and offset == 0:  # Only log new searches
        try:
            log_col = "job_search_logs" if mode == "EMPLOYMENT_SEARCH" else "search_logs"
            # Get session for gender context
            user_session = get_session(from_num) or {}

            db.collection(log_col).add({
                "query": search_query,
                "from_number": from_num,
                "city": smart_data.get("city", "GLOBAL").upper(),
                "town": smart_data.get("town", "").upper(),
                "state": smart_data.get("state", "").upper() if smart_data.get("state") else "",
                "country": smart_data.get("country", "").upper() if smart_data.get("country") else "",
                "timestamp": firestore.SERVER_TIMESTAMP,
                "lat": lat,
                "lng": lng,
                "gender": user_session.get("gender") or user_session.get("user_gender") or "All"
            })
            logger.info(f"✅ Search logged to {log_col}")
        except Exception as e:
            logger.error(f"❌ Failed to log search: {e}")



    MAX_RESULTS = 3
    offset = smart_data.get("offset", 0)

    # 🛡️ Safety initialization for variables used across branches
    verified_results = []
    targeting_meta = {}
    total_count = 0  # ✅ Safe fallback — prevents NameError on employment search path

    if not search_query:
        logger.warning("⚠️ No search query provided to smart search.")
        return [], {"category": "ALL", "city": "EVERYWHERE"}

    # --- 1. BRANCHING LOGIC: JOBS vs MARKET ---



    # --- 2. EXECUTION BRANCHES ---
    if mode == "EMPLOYMENT_SEARCH":
        # Fetch the full pool (100) for the global distance sort
        job_response = search_jobs_firestore(
            query=search_query,
            user_lat=lat,
            user_lng=lng,
            offset=0,
            top_k=100
        )

        if isinstance(job_response, tuple):
            verified_results, total_count = job_response
        else:
            verified_results = job_response or []
            total_count = len(verified_results)

        targeting_meta = {"category": "jobs", "city": "EVERYWHERE", "total_count": total_count}




    else:

        # --- INTERNAL MARKET SEARCH (Services & Quick Sales) ---

        # No longer need to check an index dictionary.
        # The search_offers_firestore function now handles the search logic.
        search_key = "default"

        # ✅ STRIKE 1: Strict Category Search

        db_response = search_offers_firestore(
            query=search_query,
            user_lat=lat,
            user_lng=lng,
            offset=0,
            top_k=100,
            category_id=cat_id,
            entry_type=cat_id
        )

        if isinstance(db_response, tuple):
            raw_results, total_count = db_response
        else:
            raw_results = db_response or []
            total_count = len(raw_results)

        verified_results = raw_results
        logger.info(
            f"✅ Strict search returned {len(verified_results)} verified results"
        )

        # ✅ STRIKE 2: The Disciplined Rescue Search

        if not verified_results:
            logger.info(f"🔍 Category '{cat_id}' empty or low quality. Running Broad Search...")

            broad_type_filter = ['quick_sale', 'product'] if cat_id == 'product' else ['service', 'professional',
                                                                                       'provider']

            broad_response = search_offers_firestore(
                query=search_query,
                user_lat=lat,
                user_lng=lng,
                offset=0,
                top_k=100,
                category_id=None,
                search_key="default",
                entry_type=cat_id,
                required_types=broad_type_filter
            )

            if isinstance(broad_response, tuple):
                raw_broad_results, total_count = broad_response
            else:
                raw_broad_results = broad_response or []
                total_count = len(raw_broad_results)

            verified_results = raw_broad_results
            logger.info(
                f"✅ Broad search returned {len(verified_results)} verified results"
            )

        # Set market-specific meta

        targeting_meta = {
            "category": (display_query or "ALL").lower().strip(),
            "category_id": cat_id,
            "city": "EVERYWHERE",
            "total_count": total_count
        }

    # 5. DATA NORMALIZATION (Distance & Priority)
    # Ensure all internal results are ready for the global sort
    for biz in verified_results:
        biz['priority'] = 1

        # Calculate distance only if it's missing or needs rounding
        b_lat, b_lng = biz.get('lat'), biz.get('lng')
        if lat and lng and b_lat and b_lng:
            dist = calculate_distance(lat, lng, b_lat, b_lng)
            biz['distance'] = round(float(dist), 2)
        elif biz.get('distance') is not None:
            biz['distance'] = round(float(biz['distance']), 2)
        else:
            biz['distance'] = 99999.0

    # --- 6. EXTERNAL SEARCH (Foursquare Enrichment) ---
    fsq_results = []

    # 🛑 GUARDRAIL: Skip if we have enough results or are in Employment mode
    if mode != "EMPLOYMENT_SEARCH" and not verified_results:

        # 🆕 THE SOFTENER: Clean query
        fsq_query = search_query.lower().strip()
        noise_words = ["vendor", "seller", "provider", "expert", "shop", "store"]
        for word in noise_words:
            fsq_query = fsq_query.replace(word, "").strip()

        # 🛡️ VALIDATION: Ensure we actually have a query left after cleaning
        if len(fsq_query) > 2:
            fsq_cat_id = None if cat_id == "product" else cat_id

            logger.info(f"🌐 Triggering Foursquare: Query='{fsq_query}'")

            # Step 1: Specific Query + Category (The most accurate)
            fsq_results = get_foursquare_data(query=fsq_query, category_id=fsq_cat_id, lat=lat, lng=lng)

            # Step 2: Specific Query Only (If category was too restrictive)
            if not fsq_results:
                logger.info(f"🔄 Retrying Foursquare: Query only ('{fsq_query}')")
                fsq_results = get_foursquare_data(query=fsq_query, lat=lat, lng=lng)

            # 🛑 REMOVED: The 3rd step (Category-only) is deleted.
            # It is better to show 0 results than to show unrelated shopping malls.
        else:
            logger.warning(f"⚠️ Foursquare skipped: Cleaned query '{fsq_query}' too short.")

    # --- 7. SMART MERGE & DEDUPLICATION ---
    final_results = list(verified_results)

    for lead in (fsq_results or []):
        lead['priority'] = 2

        # ✅ FIXED: Convert Foursquare raw meters to Kilometers safely before tracking
        raw_meters = lead.get('distance')
        if raw_meters is not None and float(raw_meters) >= 0:
            lead['distance'] = round(float(raw_meters) / 1000.0, 2)
        else:
            lead['distance'] = 99999.0

        # 🛡️ Deduplication against internal listings
        lead_name = (lead.get("name") or "").lower()
        lead_addr = (lead.get("address") or "")[:10].lower()
        lead_key = f"{lead_name}|{lead_addr}"

        already_exists = False
        for r in final_results:
            name_key = (r.get("provider_name") or r.get("name") or "").lower()
            addr_key = (r.get("address") or "")[:10].lower()

            if lead_key == f"{name_key}|{addr_key}":
                already_exists = True
                break

        if not already_exists:
            final_results.append(lead)

    # --- 8. FINAL SAFETY DEDUP (By ID) ---
    seen_ids = set()
    unique_results = []

    for r in final_results:
        unique_id = r.get("listing_id") or r.get("id")
        if unique_id:
            if unique_id not in seen_ids:
                seen_ids.add(unique_id)
                unique_results.append(r)
        else:
            # Fallback to key-based dedup for results without IDs
            name_key = (r.get("provider_name") or r.get("name") or "").lower()
            addr_key = (r.get("address") or "")[:10].lower()
            if f"{name_key}|{addr_key}" not in seen_ids:
                seen_ids.add(f"{name_key}|{addr_key}")
                unique_results.append(r)

    # --- 9. GLOBAL DISTANCE SORT & PAGINATION ---
    # ✅ FIX: Hybrid Waterfall Sort
    # Tier 1: Group by Source Priority (Internal priority=1 always wins over Foursquare priority=2)
    # Tier 2: Within those groups, sort perfectly by closest distance
    unique_results.sort(key=lambda x: (
        x.get("priority", 2),
        x.get("distance") if x.get("distance") is not None else 99999.0
    ))

    # Apply global pagination
    start = offset
    end = offset + MAX_RESULTS
    paginated_final = unique_results[start:end]

    # Final Meta Update — use len(unique_results) as the true total after merge
    targeting_meta.update({
        "category": (display_query or "ALL").lower().strip(),
        "category_id": cat_id,
        "city": "EVERYWHERE",
        "total_count": len(unique_results)  # ✅ Reflects merged internal + Foursquare count
    })

    logger.info(f"📊 Smart Search: Returning slice {start}:{end} of {len(unique_results)} results.")

    return paginated_final, targeting_meta


def smart_intent_predictor(text):
    """
    Hybrid Intent Engine:
    1. Local DeBERTa (Instant/Free)
    2. 4ound Unified Brain (Gemini/Firestore Cache)
    """
    try:
        # 1. Try local DeBERTa first (Best for privacy and speed)
        local_intent, confidence, _ = predict_intent_prototype(text)

        # 2. High Confidence Local Match
        if confidence > 0.85:
            logger.info(f"✅ Local Match ({confidence:.2f}): {local_intent}")
            # Return a structure consistent with the Brain's output
            return {
                "intent": local_intent,
                "item": text,
                "category_id": None
            }

        # 3. Low Confidence or Local Failure -> Call the Unified Brain
        logger.info(f"🧠 Local unsure ({confidence:.2f}). Consulting Unified Brain...")

        # This calls the updated call_4ound_brain we just finished
        brain_result = call_4ound_brain(text)

        # Validate the Brain's response
        if isinstance(brain_result, dict):
            intent = brain_result.get("intent", "unknown")

            # Final check: Ensure the intent is one 4ound recognizes
            if intent in ["search", "offer", "greeting"]:
                return brain_result

        return {"intent": "unknown", "item": text, "category_id": None}

    except Exception as e:
        logger.error(f"⚠️ TOTAL BRAIN FAILURE in Predictor: {e}")
        return {"intent": "unknown", "item": text, "category_id": None}


def call_4ound_brain(user_message, system_prompt=UNIFIED_4OUND_PROMPT):
    # Ensure user_message is a string even if None/Empty comes from a location pin
    user_message = str(user_message or "nearby services").strip()

    primary_model = "gemini-2.5-flash"
    fallback_model = "gemini-2.5-flash-lite"  # Optimized for even lower cost

    max_retries = 3

    search_key = user_message.lower().strip()

    # --- STEP A: CHECK GLOBAL CLOUD MEMORY ---
    try:
        doc_ref = db.collection("intent_cache").document(search_key)
        cached_doc = doc_ref.get()
        if cached_doc.exists:
            data = cached_doc.to_dict()

            # 🛡️ THE VALIDATION SHIELD
            # Ignore cache if it's missing the new flags or if 'listing' words are stuck in 'search'
            is_stale = "is_physical_product" not in data or "is_job" not in data
            listing_words = ["list", "business", "sell", "hire", "offer", "job"]
            is_mismatched = data.get("intent") == "search" and any(w in search_key for w in listing_words)

            if is_stale or is_mismatched:
                logger.info(f"🗑️ Stale/Mismatched cache for '{search_key}' ignored. Re-processing with Gemini...")
                # Do NOT return here. Let the code fall through to the actual Gemini call (Step B).
            else:
                logger.info(f"🌍 [GLOBAL CACHE HIT] '{search_key}' recovered.")
                return {
                    "item": data.get("item"),
                    "category_id": data.get("category_id"),
                    "intent": data.get("intent", "search"),
                    "reply": data.get("reply"),
                    "is_physical_product": data.get("is_physical_product", False),
                    "is_job": data.get("is_job", False),
                    "language": data.get("language", "English")
                }
    except Exception as e:
        logger.warning(f"⚠️ Global cache read skipped: {e}")

    # --- STEP B: CALL THE BRAIN ---
    for attempt in range(max_retries):
        current_model = primary_model if attempt < 2 else fallback_model
        try:
            # 1. Detailed Instruction for Tri-Flow Logic
            brain_instruction = (
                f"{system_prompt}\nReturn JSON: {{"
                f"'item':str, 'category_id':str, 'intent':str, 'language':str, "
                f"'is_physical_product':bool, 'is_job':bool"
                f"}}"
            )

            response = client.models.generate_content(
                model=current_model,
                contents=user_message,
                config={
                    'system_instruction': brain_instruction,
                    'response_mime_type': 'application/json'
                }
            )

            # --- ✅ THE SHIELD: Safe Data Extraction ---
            data = {}
            if hasattr(response, 'parsed') and response.parsed is not None:
                data = response.parsed
            elif hasattr(response, 'text') and response.text:
                try:
                    data = json.loads(response.text)
                except:
                    data = {}

            # 🛠️ THE FIX: Define all variables BEFORE creating the 'result' or 'reply'
            intent_extracted = data.get("intent", "search")
            item_extracted = data.get("item") or user_message
            detected_lang = data.get("language", "English")
            category_id = data.get("category_id")
            is_product = data.get("is_physical_product", False)
            is_job = data.get("is_job", False)

            # --- STEP C: GENERATE REPLY (LOCAL + TRANSLATION) ---
            base_reply = get_local_response(intent_extracted, item_extracted)

            if detected_lang.lower() != "english":
                translation_prompt = f"Translate this to {detected_lang} naturally: {base_reply}"
                trans_resp = client.models.generate_content(
                    model=fallback_model,
                    contents=translation_prompt
                )
                # Defensive check for translation text
                final_reply = trans_resp.text.strip() if hasattr(trans_resp, 'text') else base_reply
            else:
                final_reply = base_reply

            # 🛠️ THE DATA FIX: We include the flags for Section 7
            # while keeping all your original fields.
            result = {
                "item": item_extracted,
                "category_id": category_id,
                "intent": intent_extracted,
                "language": detected_lang,
                "reply": final_reply,
                "is_physical_product": is_product,  # Required for Tri-Flow logic
                "is_job": is_job  # Required for Tri-Flow logic
            }

            # --- STEP D: UPDATE GLOBAL MEMORY ---
            try:
                # 🧼 The "Pro" Clean
                raw_intent = result.get("intent", "search")
                clean_intent = str(raw_intent).lower().split()[0].strip()

                # 🛑 THE POISON SHIELD:
                # Check if the reply is a fallback. If it is, don't save it to the cache!
                fallbacks = ["not quite sure", "missed your point", "how to handle that"]
                is_fallback = any(f in result["reply"].lower() for f in fallbacks)

                if is_fallback:
                    logger.warning(f"⚠️ Refusing to cache fallback reply for: {search_key}")
                else:
                    doc_ref.set({
                        "item": result["item"],
                        "category_id": result["category_id"],
                        "intent": clean_intent,
                        "reply": result["reply"],
                        "language": result["language"],
                        "is_physical_product": result["is_physical_product"],
                        "is_job": result["is_job"],
                        "last_updated": firestore.SERVER_TIMESTAMP,
                        "global_hits": firestore.Increment(1)
                    }, merge=True)
                    logger.info(f"✅ Cache updated for '{search_key}' with intent '{clean_intent}'")

            except Exception as cache_e:
                logger.error(f"⚠️ Global cache update failed: {cache_e}")

            return result

        except Exception as e:
            logger.error(f"⚠️ Brain Attempt {attempt + 1} failed: {e}")
            if attempt < max_retries - 1:
                time.sleep((attempt + 1) * 2)
                continue
            else:
                logger.error(f"❌ Final Brain Failure after {max_retries} attempts.")

    # --- FINAL SAFETY FALLBACK (Hardcoded to prevent ANY KeyError) ---
    fallback_intent = "search"

    try:
        safe_reply = get_local_response(fallback_intent, user_message)
    except:
        safe_reply = f"I'm looking into '{user_message}' for you. One moment!"

    # 🛡️ UPDATED FALLBACK: Added booleans to ensure Section 7 doesn't crash
    # even if Gemini fails completely.
    return {
        "item": user_message,
        "category_id": None,
        "intent": fallback_intent,
        "reply": safe_reply,
        "is_physical_product": False,
        "is_job": False,
        "language": "English"
    }

def send_whatsapp_message(to_number, text_body):
    """
    Standard helper to send a simple text message via WhatsApp API.
    """
    try:
        # Accessing your global variables (ensure these are defined at the top of your script)
        url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
        headers = {
            "Authorization": f"Bearer {ACCESS_TOKEN}",
            "Content-Type": "application/json"
        }
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to_number,
            "type": "text",
            "text": {"body": text_body}
        }

        response = requests.post(url, json=payload, headers=headers, timeout=10)

        if response.status_code == 200:
            logger.info(f"📤 WhatsApp message sent to {to_number}")
            return True
        else:
            logger.error(f"❌ Failed to send WhatsApp: {response.text}")
            return False

    except Exception as e:
        logger.error(f"🌐 Network error sending WhatsApp: {e}")
        return False


# --- 🛠️ HELPER: Get the actual image from Meta ---
def get_whatsapp_media_url(media_id):
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    url = f"https://graph.facebook.com/v20.0/{media_id}"

    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        return response.json().get("url")
    except Exception as e:
        logger.error(f"❌ Meta Media URL Error: {e}")
        return None





def verify_image_safety(image_id, max_retries=3):
    """
    Analyzes image safety with a retry mechanism for network/API stability.
    """
    delay = 2

    for attempt in range(max_retries):
        try:
            download_url = get_whatsapp_media_url(image_id)
            if not download_url:
                return None

            image_res = requests.get(
                download_url,
                headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
                timeout=10
            )
            image_bytes = image_res.content

            safety_prompt = (
                "Analyze this marketplace image. "
                "Is there any nudity, blood, gore, or a weapon (gun, rifle, etc)? "
                "Reply ONLY with 'SAFE' or 'UNSAFE'."
            )

            # ✅ Now using the correct import and method
            image_part = types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg")

            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[
                    image_part,
                    safety_prompt
                ]
            )

            if not response.text:
                logger.warning(f"⚠️ Empty safety response on attempt {attempt + 1}.")
                return False

            verdict = response.text.strip().upper()

            if "UNSAFE" in verdict:
                return False
            if "SAFE" in verdict:
                return True

            return False

        except (requests.exceptions.RequestException, Exception) as e:
            logger.error(f"🌐 Attempt {attempt + 1} failed: {e}")
            if attempt == max_retries - 1:
                return None
            wait_time = (delay * (2 ** attempt)) + random.uniform(0, 1)
            logger.info(f"⏳ Retrying in {wait_time:.1f} seconds...")
            time.sleep(wait_time)

    return None


    # --- 🚀 TRIGGER: The Background Thread ---


def start_safety_check_thread(doc_id, image_id):
    def run_check():
        # 1. Fetch data
        doc_ref = db.collection("listings").document(doc_id)
        doc = doc_ref.get()
        if not doc.exists: return
        data = doc.to_dict()

        # 🚀 2. CALL THE SAFETY CHECK FIRST (Fixes the red line!)
        # This creates the 'status' variable that the rest of the code needs
        status = verify_image_safety(image_id)

        # 3. Convert Firestore timestamp safely
        user_last_msg_time = data.get("created_at")
        if isinstance(user_last_msg_time, str):
            user_last_msg_time = datetime.fromisoformat(user_last_msg_time)

        if not user_last_msg_time:
            user_last_msg_time = datetime.now(timezone.utc)

        # 4. Check the 24-hour window 🛡️
        time_elapsed = datetime.now(timezone.utc) - user_last_msg_time
        window_expired = time_elapsed > timedelta(hours=23)

        if window_expired:
            logger.warning(f"🕒 Window expired for {doc_id}. Skipping WhatsApp notification.")
            # Even if we don't message the user, we still update the DB
            doc_ref.update({
                "is_verified": status is True,
                "notification_status": "window_closed_no_msg"
            })
            return

        # 5. If the window is still open, proceed with the message
        # 'status' is now defined, so no more red lines!
        if status is True:
            doc_ref.update({"is_verified": True, "verification_status": "cleared"})
            send_whatsapp_message(data.get('user_id'), "✅ Your listing is now live!")
        elif status is False:
            doc_ref.update({"verification_status": "rejected"})
            send_whatsapp_message(data.get('user_id'), "🚫 Sorry, your image was rejected.")

    threading.Thread(target=run_check, daemon=True).start()


def process_pending_verifications():
    """
    Recurring Recovery: Finds items missed or stuck and pushes them live.
    Safety: Uses a 'checking' status to prevent duplicate processing.
    """
    logger.info("🛠️ Recovery: Scanning for pending safety reviews...")

    # Using the global constant FIRESTORE_OFFERS
    pending_items = db.collection(FIRESTORE_OFFERS) \
        .where("is_verified", "==", False) \
        .where(filter=FieldFilter("verification_status", "==", "pending")) \
        .stream()

    found_any = False
    for doc in pending_items:
        found_any = True

        # 🛡️ THE LOCK: Prevents race conditions
        doc.reference.update({"verification_status": "checking"})

        logger.info(f"🔄 Processing Listing ID: {doc.id}")

        try:
            verify_and_publish_listing(doc.id)
        except Exception as e:
            # Reset on failure
            logger.error(f"❌ Worker failed for {doc.id}: {e}")
            doc.reference.update({"verification_status": "pending"})

    if not found_any:
        logger.info("✅ Recovery check complete: No pending items found.")



def verify_and_publish_listing(doc_id):
    try:
        # Use your global constant FIRESTORE_OFFERS instead of hardcoded string
        doc_ref = db.collection(FIRESTORE_OFFERS).document(doc_id)
        doc = doc_ref.get()
        if not doc.exists:
            return
        data = doc.to_dict()

        image_id = data.get("image_id")
        user_id = data.get("user_id")

        if not image_id:
            return

        # 1. Run Gemini Safety Check
        status = verify_image_safety(image_id)

        # 2. Check 24-hour window for the notification
        user_last_msg_time = data.get("created_at")
        if hasattr(user_last_msg_time, "to_datetime"):
            user_last_msg_time = user_last_msg_time.to_datetime()
        elif isinstance(user_last_msg_time, str):
            try:
                user_last_msg_time = datetime.fromisoformat(user_last_msg_time.replace("Z", "+00:00"))
            except:
                user_last_msg_time = None

        can_notify = False
        if user_last_msg_time:
            # Ensure timezone awareness for comparison
            if user_last_msg_time.tzinfo is None:
                user_last_msg_time = user_last_msg_time.replace(tzinfo=timezone.utc)

            time_elapsed = datetime.now(timezone.utc) - user_last_msg_time
            if time_elapsed < timedelta(hours=23):
                can_notify = True

        # 3. Handle Approval/Rejection
        if status is True:
            now_live = datetime.now(timezone.utc)

            # Use the existing expiry logic or default to 7 days for products
            expiry_date = data.get("expires_at")
            if not expiry_date:
                expiry_date = now_live + timedelta(days=7)

            update_payload = {
                "is_verified": True,
                "verification_status": "approved",
                "approved_at": now_live,
                "expires_at": expiry_date,
            }

            # Update Firestore
            doc_ref.update(update_payload)
            logger.info(f"✅ Listing {doc_id} has been approved and is now live.")

            # Notify User
            if can_notify and user_id:
                send_whatsapp_message(user_id, "✅ Your listing is now live and searchable!")

        elif status is False:
            doc_ref.update({"verification_status": "rejected"})
            logger.info(f"🚫 Listing {doc_id} failed safety check.")

            if can_notify and user_id:
                send_whatsapp_message(user_id, "🚫 Your image didn't pass our safety check.")

    except Exception as e:
        logger.error(f"Error in background verification: {e}")


def get_local_response(intent, item, language="English", sub_key=None, **kwargs):
    """
    Picks a random template dynamically from the 4ound JSON structure.
    Supports dynamic variables (price, location, etc.) via **kwargs.
    """
    # 1. Get the language block (Default to English)
    lang_data = RESPONSES_DATA.get(language, RESPONSES_DATA.get("English", {}))

    # 2. Dynamic Intent Mapping
    actual_intent = "general" if intent == "greeting" else intent
    intent_block = lang_data.get(actual_intent, {})

    if not sub_key:
        if actual_intent == "general":
            target = "greeting"
        elif actual_intent == "search":
            target = "searching_start"
        else:
            target = "start"
    else:
        target = sub_key

    templates = intent_block.get(target)

    # 3. Recursive Fallback Logic
    if not templates and language != "English":
        return get_local_response(intent, item, language="English", sub_key=sub_key, **kwargs)

    # 4. Final Safety Fallback
    if not templates:
        logger.warning(f"⚠️ Template not found for Intent: {intent}, Sub-key: {target}.")
        templates = lang_data.get("general", {}).get("fallback", ["Looking into {item} for you!"])

    if isinstance(templates, str):
        templates = [templates]

    # 5. Pick and Format Safely
    chosen = random.choice(templates)

    # Prepare formatting dictionary
    fallback_word = "your business" if intent == "offer_pro" else "your item"
    format_data = {
        "item": item if item else fallback_word
    }

    # 🟢 MERGE ADDITIONAL DATA (This is where 'price' gets added)
    format_data.update(kwargs)

    try:
        # This will now correctly fill {item}, {price}, etc., if they exist in the JSON
        return chosen.format(**format_data)
    except Exception as e:
        # Manual fallback logic
        result = str(chosen)
        for key, value in format_data.items():
            tag = "{" + key + "}"
            if tag in result:
                result = result.replace(tag, str(value))
        return result


def send_typing_indicator(recipient_id, message_id, emoji="🔍"):
    """
    Handles all 'Thinking' states for 4ound:
    1. Marks message as 'Read' (Blue ticks).
    2. Adds a Reaction (e.g., 🔍).
    3. Removes Reaction (if emoji="").
    """
    url = f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }

    # Action 1: Read Receipt
    read_data = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": message_id
    }

    # Action 2: Reaction (Add or Remove)
    reaction_data = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient_id,
        "type": "reaction",
        "reaction": {
            "message_id": message_id,
            "emoji": emoji  # "" clears the emoji
        }
    }

    try:
        # We always send the 'read' status first
        res_read = requests.post(url, headers=headers, json=read_data)

        # Then we update the reaction
        res_react = requests.post(url, headers=headers, json=reaction_data)

        if res_react.status_code == 200:
            action = "Cleared" if emoji == "" else f"Sent '{emoji}'"
            logger.info(f"✨ Reaction {action} for {recipient_id}")
        else:
            logger.error(f"❌ Reaction failed: {res_react.text}")

    except Exception as e:
        logger.error(f"⚠️ Typing Indicator Error: {e}")


def format_unified_result(venue):
    """
    Final WhatsApp UI: Formats Firestore and Foursquare results into a clean card.
    """
    # 1. Name & Badge (Using the 'provider_name' key from our new searchers)
    name = (venue.get("provider_name") or venue.get("name", "Unknown Business")).upper()

    # 2. Distance Logic (Unifying Meters vs KM)
    # Foursquare gives meters, Firestore search gives KM
    dist_km = 0
    if venue.get("priority") == 1:  # Firestore (Verified)
        dist_km = float(venue.get("distance", 0))
    else:  # Foursquare (Web Leads)
        dist_m = venue.get("distance", 0)
        dist_km = float(dist_m) / 1000 if dist_m else 0

    # 3. Address Extraction
    address = venue.get("address") or "Location available on map"

    # Start building the UI
    result_text = f"📍 *{name}*\n"
    if dist_km > 0:
        result_text += f"📏 {dist_km:.1f} km away\n"

    result_text += f"🏠 {address}\n"

    # 4. WhatsApp & Contact Logic
    # We use 'contact_whatsapp' which we unified in the search functions
    wa_link = venue.get("contact_whatsapp")

    if wa_link:
        # If it's a full URL already (from Foursquare), use it.
        # If it's just a number (from Firestore), build the URL.
        if "wa.me" in str(wa_link):
            result_text += f"💬 *Chat:* {wa_link}\n"
        else:
            # Firestore numeric fallback
            clean_phone = "".join(filter(str.isdigit, str(wa_link)))
            if clean_phone.startswith("0") and len(clean_phone) == 11:
                clean_phone = "234" + clean_phone[1:]

            wa_url = f"https://wa.me/{clean_phone}?text=Hi, I found you on 4ound!"
            result_text += f"💬 *WhatsApp:* {wa_url}\n"
            result_text += f"📞 *Call:* +{clean_phone}\n"

    # 5. Fixed Map Link (Google Maps Standard)
    lat = venue.get("lat") or venue.get("latitude")
    lng = venue.get("lng") or venue.get("longitude")

    if lat and lng:
        # Standard Google Maps URL that opens the app on the phone
        map_url = f"https://www.google.com/maps/search/?api=1&query={lat},{lng}"
        result_text += f"🗺️ *View on Map:* {map_url}\n"

    return result_text


def send_interactive_button(phone_number_id, to_number, body_text, buttons_list, message_id=None):
    """
    Sends multiple Quick Reply buttons (up to 3) with optional context.
    """
    url = f"https://graph.facebook.com/{FB_API_VERSION}/{phone_number_id}/messages"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}

    formatted_buttons = []
    for btn in buttons_list:
        title = btn.get("title", "Select")
        if len(title) > 20: title = title[:20]  # WhatsApp Limit

        formatted_buttons.append({
            "type": "reply",
            "reply": {"id": btn.get("id"), "title": title}
        })

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_number,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body_text},
            "action": {"buttons": formatted_buttons}
        }
    }

    # --- ✅ ADD THIS: CONTEXT INJECTION ---
    if message_id:
        payload["context"] = {"message_id": message_id}

    try:
        response = requests.post(url, json=payload, headers=headers)
        if response.status_code == 200:
            logger.info(f"✅ Button sent (Context: {message_id}) to {to_number}")
        else:
            logger.error(f"❌ Meta API Error: {response.json()}")
    except Exception as e:
        logger.error(f"❌ Connection Error: {e}")



def get_user_listings(phone_number):
    """Retrieves all active listings belonging to a specific phone number."""
    try:
        # 1. 🔍 UPDATE: Query the 'listings' collection (unified for jobs/sales/pro)
        docs = db.collection("listings").where("owner_phone", "==", phone_number).stream()

        listings = []
        for doc in docs:
            data = doc.to_dict()

            # 2. 🛡️ NEW: Capture verification status
            is_live = data.get("is_verified", False)

            # Use 'title' or 'item_name' since 'provider_name' is usually for Pros
            display_name = data.get("title") or data.get("item_name") or "Unnamed Listing"

            listings.append({
                "id": doc.id,
                "name": display_name,
                "is_verified": is_live  # 👈 Pass this to your Interactive List logic
            })

        return listings
    except Exception as e:
        logger.error(f"Error fetching listings: {e}")
        return []


def perform_actual_deletion(offer_id, owner_phone):
    """Securely removes listing from Firestore."""
    try:
        # Use the global constant for your collection
        doc_ref = db.collection(FIRESTORE_OFFERS).document(offer_id)
        doc = doc_ref.get()

        if not doc.exists:
            logger.warning(f"Deletion failed: Document {offer_id} not found.")
            return False

        # Ownership verification
        data = doc.to_dict()
        if data.get("owner_phone") != owner_phone:
            logger.warning(f"Unauthorized deletion attempt for {offer_id} by {owner_phone}")
            return False

        # DELETE from Firestore
        doc_ref.delete()
        logger.info(f"🔥 Firestore document {offer_id} deleted successfully.")

        return True

    except Exception as e:
        logger.error(f"❌ Deletion failure for {offer_id}: {e}")
        return False



def nightly_cleanup():
    logger.info("🧹 Starting scheduled maintenance: Purging expired listings...")

    now_utc = datetime.now(timezone.utc)
    expired_docs = (
        db.collection(FIRESTORE_OFFERS)
        .where("expires_at", "<", now_utc)
        .stream()
    )

    count = 0
    for doc in expired_docs:
        # Calls the silent deletion function we cleaned earlier
        if perform_actual_deletion_silent(doc.id, doc.to_dict().get("owner_phone")):
            count += 1
            logger.info(f"🗑️ Purged expired: {doc.id}")

    if count > 0:
        logger.info(f"✨ Purge complete. {count} expired listings removed.")
    else:
        logger.info("✅ Cleanup check complete: No expired listings to remove.")



def perform_actual_deletion_silent(offer_id, owner_phone):
    """Internal version of deletion that unifies collection references."""
    try:
        # 1. DELETE from Firestore
        doc_ref = db.collection(FIRESTORE_OFFERS).document(offer_id)
        doc_ref.delete()
        logger.info(f"🗑️ Background Eviction: Removed doc {offer_id} from {FIRESTORE_OFFERS}")

        # The matrix update logic has been removed as it is no longer
        # required for the fuzzy search architecture.

        return True
    except Exception as e:
        logger.error(f"❌ Silent delete failed for {offer_id}: {e}")
        return False



def get_market_insight(user_city="GLOBAL"):
    global CACHED_MARKET_TIP, LAST_CACHE_TIME
    current_time = time.time()

    # Define target_city FIRST before any cache check
    target_city = str(user_city).upper().strip()

    # --- 1. CHECK CACHE ---
    if target_city in CACHED_MARKET_TIP and (current_time - LAST_CACHE_TIME.get(target_city, 0) < 3600):
        return CACHED_MARKET_TIP[target_city]

    try:
        # --- 2. FETCH RECENT LOGS ---
        one_day_ago = datetime.now(timezone.utc) - timedelta(days=1)
        recent_logs = db.collection("search_logs") \
            .where("timestamp", ">=", one_day_ago) \
            .limit(1000).get()

        blacklist = {
            "hello", "hi", "hey", "test", "results", "ok", "please",
            "job", "jobs", "work", "service", "someone", "help", "yes", "no"
        }

        local_counts = {}
        global_counts = {}

        for doc in recent_logs:
            data = doc.to_dict()
            q = data.get("query", "").strip().lower()
            log_city = str(data.get("city", "GLOBAL")).upper().strip()

            if not q or len(q) < 3 or q in blacklist:
                continue

            global_counts[q] = global_counts.get(q, 0) + 1

            if log_city == target_city:
                local_counts[q] = local_counts.get(q, 0) + 1

        # --- 3. THRESHOLD & SOURCE SELECTION ---
        trending_local = {k: v for k, v in local_counts.items() if v >= 3}

        if len(trending_local) >= 1:
            logger.info(f"🎯 INSIGHT: Showing LOCAL trends for {target_city}")
            final_source = trending_local
            location_label = f"around *{target_city}*"
        else:
            logger.info(f"🌍 INSIGHT: Showing GLOBAL fallback for {target_city}")
            final_source = {k: v for k, v in global_counts.items() if v >= 3}
            location_label = "locally"

        # --- 4. SORT TOP 3 ---
        trending = sorted(
            final_source.items(),
            key=lambda x: (-x[1], x[0])
        )[:3]

        # --- 5. EMPTY FALLBACK ---
        if not trending:
            return "💡 *PRO-TIP:* Be the first to list a unique service to dominate the local market! 🚀"

        # --- 6. BUILD MESSAGE ---
        tip_msg = f"🎊 *4OUND OPPORTUNITY ALERT* 🎊\nUsers {location_label} are hunting for:\n\n"
        medals = ["🥇", "🥈", "🥉"]

        for i, (item, count) in enumerate(trending):
            tip_msg += f"{medals[i]} *{item.upper()}* ({count} searches)\n"

        tip_msg += "\n⚡ *Tip:* If you provide these, list them now to get called first!"

        # --- 7. UPDATE CACHE ---
        CACHED_MARKET_TIP[target_city] = tip_msg
        LAST_CACHE_TIME[target_city] = current_time

        return tip_msg

    except Exception as e:
        logger.error(f"Insight Error: {e}")
        return CACHED_MARKET_TIP.get(target_city) or "💡 *Tip:* The more people search, the smarter 4ound becomes. 🚀"


def prepare_listing_data(session, lat, lng):
    listing_type = session.get("listing_type")
    visibility_raw = session.get("visibility", "physical")

    # 🕒 FIX: Pull the timestamp we saved in the session (Step 5)
    # Fallback to now only if session is missing it
    created_at_val = session.get("created_at")
    if created_at_val:
        now = datetime.fromisoformat(created_at_val)
    else:
        now = datetime.now(timezone.utc)

    # --- 1. Map Visibility Contextually (Your logic is great here) ---
    if listing_type == "quick_job":
        visibility = "On-site" if visibility_raw == "physical" else "Remote"
        expiry_days = 14
    elif listing_type == "quick_sale":
        visibility = "Physical Meetup" if visibility_raw == "physical" else "Delivery Only"
        expiry_days = 7
    else:  # professional
        visibility = "Physical Shop" if visibility_raw == "physical" else "Remote Service"
        expiry_days = None

    # --- 2. Build Base Document ---
    doc = {
        "listing_id": session.get("listing_id") or str(uuid.uuid4())[:8].upper(),
        "owner_phone": session.get("biz_phone"),
        "user_id": session.get("user_id"),  # 👈 CRITICAL: Added for the WhatsApp notification thread
        "image_id": session.get("image_id"),  # 👈 CRITICAL: Added at top level for Gemini safety check
        "is_verified": session.get("is_verified", False),
        "listing_type": listing_type,
        "media": {
            "type": "image",
            "source": "whatsapp",
            "id": session.get("image_id")
        },
        "location": {
            "lat": float(lat) if lat is not None else None,
            "lng": float(lng) if lng is not None else None,
            "address": session.get("manual_address") or session.get("detected_address"),

        },
        "visibility": visibility,
        "expiry_days": expiry_days,
        "created_at": now,  # 👈 Using the consistent timestamp
        "lang": session.get("detected_lang", "English")
    }

    # 🛡️ Fix: Expiry handling
    if expiry_days is not None:
        doc["expires_at"] = now + timedelta(days=expiry_days)
    else:
        doc["expires_at"] = None

    # --- 3. Inject Schema AND Build Searchable Text ---
    searchable_text = ""

    # A. JOBS
    if listing_type == "quick_job":
        job_role = session.get('item_name') or "Staff"
        title = f"Hiring: {job_role}"
        desc = session.get("job_details") or session.get("offer_description")
        # Use the formatted string (₦50k/month)
        sal = str(session.get("formatted_price") or session.get("price") or "Negotiable")

        doc.update({"title": title, "description": desc, "compensation": sal})
        searchable_text = f"{title} {desc} hiring vacancy work {sal} {job_role}"

    # 🟢 B. PROFESSIONAL SERVICES (This is what you were missing!)
    elif listing_type == "professional":
        biz_name = session.get("biz_name") or session.get("item_name") or "Professional Service"
        title = biz_name
        desc = session.get("offer_description") or "Professional services available."

        # 🛡️ MAP THE RATE: This pulls "₦5,000/hour" into the 'compensation' field
        rate = str(session.get("formatted_price") or session.get("price") or "Contact for Quote")

        doc.update({"title": title, "description": desc, "compensation": rate})
        searchable_text = f"{title} {desc} expert service professional {rate}"

    # 🔵 C. QUICK SALE (Products)
    elif listing_type == "quick_sale":
        item_name = session.get("item_name") or "Item for Sale"
        title = item_name
        desc = session.get("item_info") or "Quality item for sale."

        # Products usually don't have '/month', just the total price
        price = str(session.get("formatted_price") or session.get("price") or "Contact for Price")

        doc.update({"title": title, "description": desc, "compensation": price})
        searchable_text = f"{title} {desc} for sale price {price}"

    # --- 4. Final Search Index Injection ---
    # We clean and save the search text so the Semantic Search can find it
    doc["search_text"] = searchable_text.lower().strip()

    return doc


def commit_session(from_number, session):
    """Ensures every session save has a UTC timestamp for debugging."""
    if session:
        session["updated_at"] = datetime.utcnow()
        save_session(from_number, session)


def parse_nigerian_price(price_text, wa_id=None):
    if not price_text:
        return 0.0, "₦", "₦0"

    COUNTRY_CURRENCY_MAP = {
        "234": "₦", "1": "$", "44": "£", "233": "GH₵",
        "254": "KSh", "27": "R", "971": "AED", "91": "₹"
    }

    # --- GEO DETECTION ---
    home_symbol = "₦"
    if wa_id:
        clean_wa_id = re.sub(r"\D", "", str(wa_id))
        for code in sorted(COUNTRY_CURRENCY_MAP.keys(), key=len, reverse=True):
            if clean_wa_id.startswith(code):
                home_symbol = COUNTRY_CURRENCY_MAP[code]
                break

    lower_text = price_text.lower()

    # --- SYMBOL DETECTION ---
    detected_symbol = None
    for sym in COUNTRY_CURRENCY_MAP.values():
        if sym.lower() in lower_text:
            detected_symbol = sym
            break

    # --- TEXT CURRENCY DETECTION ---
    TEXT_MAP = {
        "ngn": "₦", "naira": "₦",
        "usd": "$", "dollar": "$",
        "gbp": "£", "pound": "£"
    }

    if not detected_symbol:
        for word, sym in TEXT_MAP.items():
            if word in lower_text:
                detected_symbol = sym
                break

    final_symbol = detected_symbol or home_symbol

    # --- CLEAN ---
    clean_text = re.sub(r"[^\d\.kmKbB-]", "", lower_text)

    if not clean_text:
        return 0.0, final_symbol, f"{final_symbol}0"

    # --- EXTRACT NUMBER ---
    nums = re.findall(r"\d*\.?\d+", clean_text)
    if not nums:
        return 0.0, final_symbol, f"{final_symbol}0"

    try:
        val = float(nums[0])
    except:
        return 0.0, final_symbol, f"{final_symbol}0"

    # --- MULTIPLIERS ---
    # Change r"k\b" to just r"k" or r"k" with a case-insensitive flag
    if "k" in clean_text:
        val *= 1_000
    elif "m" in clean_text:
        val *= 1_000_000
    elif "b" in clean_text:
        val *= 1_000_000_000

    # --- FORMAT ---
    formatted = f"{final_symbol}{int(val):,}" if val.is_integer() else f"{final_symbol}{val:,.2f}"

    return val, final_symbol, formatted

def format_currency(value, symbol):
    """
    Converts 50000 → ₦50,000
    Keeps decimals if needed.
    """
    if value.is_integer():
        return f"{symbol}{int(value):,}"
    else:
        return f"{symbol}{value:,.2f}"


def is_text_safe(text: str) -> bool:
    """
    Checks if text is clean of gruesome, ammunition, or sexual content.
    """
    if not text:
        return True

    # .contains_profanity() returns True if bad words (or variations) are found
    if profanity.contains_profanity(text):
        return False

    return True


def send_and_finish(phone_number_id, from_number, text, message_id=None):
    """
    Sends the message and prepares the logic to exit.
    """
    guarded_send(phone_number_id, from_number, text, message_id)

    # We return True so the calling function knows it's time to 'return'
    return True


def is_onboarding(flow):
    """
    Checks if the user is in the middle of providing data for a listing.
    """
    if not flow:
        return False

    # 🚨 THE FIX: If the flow is about SEARCHING, it's not onboarding!
    if "search" in str(flow):
        return False

    # This still catches 'awaiting_item_info', 'awaiting_offer_details', etc.
    return str(flow).startswith("awaiting_")


def startup_recovery():
    # Targets items that didn't finish due to network failure or server crash
    query = db.collection("listings").where("is_verified", "==", False).stream()

    for doc in query:
        data = doc.to_dict()
        status = data.get("verification_status")

        # Only retry if it hasn't been permanently rejected
        if status != "rejected":
            logger.info(f"🔄 Resuming safety check for: {doc.id}")
            start_safety_check_thread(doc.id, data.get("image_id"))


def resolve_category(intent: str, query: str = "") -> str:
    """
    4ound Category Resolver (V4 - Production Optimized)
    Includes Job Routing, Service Discovery, and Product Fallback.
    """
    # 1. CLEANING & NORMALIZATION
    intent = str(intent).lower().strip() if intent else "unknown"
    query_lower = str(query).lower().strip() if query else ""
    words = set(query_lower.split())

    # 2. TIER 1: HARD ROUTES (Intent-Based)
    if intent == "quick_sell":
        return "product"
    if intent in ["offer_job", "search_employment", "recruiter_onboarding"]:
        return "job"
    # 🆕 ADD THIS HARD ROUTE
    if intent in ["search_service", "offer_service"]:
        return "service"
    if intent == "greeting":
        return "system"

    # 3. TIER 2: KEYWORD BUCKETS
    keywords = {
        "job": {"job", "work", "hiring", "vacancy", "career", "apply", "employment", "recruitment", "intern", "salary"},

        # 🔹 CLEANED: Keep only specific professions (High Confidence)
        "service_professions": {"barber", "mechanic", "plumber", "repair", "cleaning", "delivery", "dispatch",
                                "painter", "tutor", "driver", "tailor", "designer", "developer", "electrician",
                                "salon", "laundry", "artisan", "tech", "person", "professional", "expert",
                                "consultant"},

        "product": {
    # Electronics
    "phone", "laptop", "tv", "computer", "tablet", "camera", "radio",
    "iphone", "samsung", "tecno", "infinix",
    # Power
    "generator", "inverter", "battery", "solar",
    # Vehicles
    "car", "bike", "motorcycle", "vehicle", "truck",
    # Fashion
    "shoe", "bag", "cloth", "shirt", "dress", "trouser", "sandal", "sneaker",
    # Home
    "furniture", "chair", "table", "bed", "mattress", "sofa", "fridge",
    "freezer", "washing", "blender", "cooker", "stove",
    # Food/consumables
    "food", "cake", "bread", "rice", "oil",
    # General
    "item", "gadget", "buy", "sell", "price"
},

        # 🔹 INTENT: These are the "triggers" that make the bot wake up
        "intent_service": {"looking", "need", "find", "hire", "want", "search", "looking for", "service"}
    }

    # 4. TIER 3: PRIORITY LOGIC
    # A. Check for Job Intent
    if (words & keywords["job"]):
        return "job"

    # B. Check for Service Intent
    # Only classify as "service" if it's a specific profession OR if the word "service" is used with intent
    is_prof = bool(words & keywords["service_professions"])
    is_service_intent = bool(words & {"service"}) and bool(words & keywords["intent_service"])

    if is_prof or is_service_intent:
        return "service"

    # C. Check for Product
    if words & keywords["product"] or any(
            any(kw in w for kw in keywords["product"])
            for w in words
    ):
        return "product"

    # 5. TIER 4: FALLBACKS
    # If the intent is 'search' but keywords are ambiguous, default to service
    # as it's safer for 4ound's AI goals than pushing products blindly.
    if intent == "search":
        # Check if query contains any product-like signals
        product_signals = ["buy", "sell", "price", "cost", "cheap", "new", "used", "brand"]
        if any(signal in query_lower for signal in product_signals):
            return "product"
        return "service"

    return "product"


def edit_message(phone_number_id, to_number, message_id, new_text):
    """
    Edits an existing WhatsApp message using its message_id.
    Note: Messages can only be edited within 15 minutes of sending.
    """
    url = f"https://graph.facebook.com/v18.0/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",  # Ensure ACCESS_TOKEN is defined in your script
        "Content-Type": "application/json"
    }

    data = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_number,
        "context": {"message_id": message_id},  # 👈 This is the secret sauce
        "type": "text",
        "text": {"body": new_text}
    }

    try:
        response = requests.post(url, headers=headers, json=data)
        response_data = response.json()

        if response.status_code == 200:
            logger.info(f"✅ Message {message_id} edited successfully.")
        else:
            logger.error(f"❌ Failed to edit message: {response_data}")

        return response_data
    except Exception as e:
        logger.error(f"⚠️ Error calling Edit API: {e}")
        return None


def extract_keyword(text):
    if not text:
        return ""

    text = re.sub(
        r"^(i am looking for|i am looking|looking for|find me|i need|need|i want|want)\s+",
        "",
        text,
        flags=re.IGNORECASE
    )

    return text.strip().lower()

def log_search_to_db(from_number, query, city="GLOBAL"):
    """
    Saves search query to Firestore, tagged with the user's axis (city).
    """
    try:
        db.collection("search_logs").add({
            "from_number": str(from_number),
            "query": query.strip().lower(),
            "city": str(city).upper().strip(), # 📍 Crucial: Store city for filtering
            "timestamp": datetime.now(timezone.utc)
        })
        logger.info(f"📝 Search Logged: '{query}' in {city}")
    except Exception as e:
        logger.error(f"❌ Failed to log search: {e}")


def get_ad_from_db(target_id):
    """
    Direct lookup for Ad/Listing performance stats.
    Used by the 'status' command.
    """
    try:
        # 1. Check the primary Admin Adverts collection
        ad_ref = db.collection("active_ads").document(target_id).get()
        if ad_ref.exists:
            return ad_ref.to_dict()

        # 2. Fallback: Check the User Listings collection
        # (This allows admins to check the status of regular user posts too!)
        listing_ref = db.collection("listings").document(target_id).get()
        if listing_ref.exists:
            data = listing_ref.to_dict()
            # Normalize 'title' to 'biz_name' so the status report doesn't break
            data["biz_name"] = data.get("title", "User Listing")
            return data

        return None
    except Exception as e:
        logger.error(f"❌ Error fetching ad for status: {e}")
        return None

def send_help_button(phone_number_id, to):

    try:

        send_interactive_button(
            phone_number_id,
            to,
            "Need help using 4ound?",
            buttons_list=[
                {
                    "id": "open_help_menu",
                    "title": "📘 Help"
                }
            ]
        )

    except Exception as e:
        logger.error(f"Help button failed: {e}")


def handle_whatsapp_logic(data):
    lat = None
    lng = None
    message_id = None
    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            phone_number_id = value.get("metadata", {}).get("phone_number_id")
            all_messages = value.get("messages", [])

            for message in all_messages:
                message_id = message.get("id")
                from_number = message.get("from")
                msg_type = message.get("type")

                # --- 🛑 1. MEDIA BOUNCER ---
                if msg_type in ["video", "document", "audio", "voice", "sticker"]:
                    session = get_session(from_number) or {}
                    lang = session.get("detected_lang", "English")
                    reject_msg = "⚠️ 4ound AI only accepts text, locations, and photos."
                    guarded_send(phone_number_id, from_number, reject_msg, message_id)
                    continue

                try:
                    if message_id in processed_message_ids:
                        continue

                    # 🛡️ Cap memory growth
                    if len(processed_message_ids) > 10000:
                        processed_message_ids.clear()

                    processed_message_ids.add(message_id)

                    # START Reaction
                    threading.Thread(target=send_typing_indicator, args=(from_number, message_id, "🔍")).start()

                    try:  # 👈 Start a 'Protected' block
                        # --- 2. SINGLE SOURCE OF TRUTH (Setup) ---
                        session = get_session(from_number) or {}

                        # Unified Extraction: Handles Text, Image Captions, and Buttons/Lists
                        text = ""
                        if msg_type == "text":
                            text = (message.get("text") or {}).get("body", "").strip()
                        elif msg_type == "image":
                            text = (message.get("image") or {}).get("caption", "").strip()
                        elif msg_type == "interactive":
                            interactive = message.get("interactive", {})
                            text = interactive.get("button_reply", {}).get("id") or \
                                   interactive.get("list_reply", {}).get("id") or ""

                        # --- 🟢 1.1 UNIVERSAL ADMIN ACCESS GATEWAY ---
                        if text == os.getenv("ADMIN_SECRET_CODE"):
                            session.update({
                                "flow": "admin_ad_name",
                                "ad_draft": {
                                    "created_at": time.time(),
                                    "current_reach": 0,
                                    "seen_by": [],
                                    "is_active": True
                                }
                            })
                            commit_session(from_number, session)
                            msg = "🔓 *Admin Access Granted.*\n\nStep 1: What is the **Business/Ad Name**?\n(This helps you track the ad later)."
                            guarded_send(phone_number_id, from_number, msg, message_id)
                            return


                        # --- 🛑 MASTER TRAFFIC CONTROLLER ---
                        if msg_type == "location":
                            logger.info(f"📍 Location pin received. Routing flow: {session.get('flow')}")

                            loc_data = message.get("location", {})
                            lat = loc_data.get("latitude")
                            lng = loc_data.get("longitude")

                            session = get_session(from_number) or {}
                            current_flow = session.get("flow")

                            # --- PATH A: SEARCH FLOW ---
                            if current_flow == "awaiting_search_location":
                                session.update({
                                    "user_lat": lat,
                                    "user_lng": lng,
                                    "flow": "searching",
                                    "search_executing": False  # 🧹 Clear any stale lock
                                })
                                commit_session(from_number, session)
                                logger.info("✅ Search coordinates cached. Falling through to Section 8.")
                                # NO RETURN

                            # --- PATH B: ONBOARDING FLOW ---
                            elif current_flow == "awaiting_offer_location":
                                # Section 7 owns the geocoding for this path
                                session.update({
                                    "lat": lat,
                                    "lng": lng,
                                    "user_lat": lat,
                                    "user_lng": lng
                                })
                                commit_session(from_number, session)
                                logger.info("✅ Onboarding coordinates cached. Falling through to Section 7.")
                                # NO RETURN

                            # --- PATH C: DEFAULT / RANDOM PIN ---
                            else:
                                # No downstream section owns this, so resolve city here
                                geo_refresh = extract_global_location(lat=lat, lng=lng, from_number=from_number)
                                refreshed_city = geo_refresh.get("town") or geo_refresh.get("name") or "nearby"

                                session.update({
                                    "user_lat": lat,
                                    "user_lng": lng,
                                    "location": geo_refresh.get("town") or refreshed_city,
                                    "city": geo_refresh.get("city"),
                                    "state": geo_refresh.get("state"),
                                    "country": geo_refresh.get("country")
                                })
                                commit_session(from_number, session)

                                guarded_send(phone_number_id, from_number,
                                             f"📍 I've updated your location to *{refreshed_city}*! What would you like to find? (e.g., 'Boutique')",
                                             message_id)
                                return

                        # --- 🛑 1.2 SAFETY GATE ---
                        # 🛡️ Profanity Check (Only for text messages)
                        if msg_type == "text" and profanity.contains_profanity(text):
                            logger.warning(f"🛡️ Blocked prohibited text from {from_number}")
                            reject_text = "❌ *Safety Alert:* This content is prohibited on 4ound. Please keep it safe and legal."
                            guarded_send(phone_number_id, from_number, reject_text, message_id)
                            return

                        # 📸 Log image receipt
                        if msg_type == "image":
                            # We don't return here; this lets the flow continue to Section 7/8
                            logger.info(f"🖼️ Image received from {from_number}. Bypassing text-safety gate.")

                        # --- 1.3 INTERACTIVE HANDLER (Lists & Buttons) ---
                        if msg_type == "interactive":
                            interactive = message.get("interactive", {})

                            # A. LIST SELECTIONS (Management Menu)
                            if interactive.get("type") == "list_reply":
                                selection_id = interactive.get("list_reply", {}).get("id")

                                # 📄 MANAGEMENT PAGINATION
                                if selection_id and selection_id.startswith("manage_page_"):

                                    offset = int(selection_id.replace("manage_page_", ""))

                                    MAX_ROWS = 9

                                    user_listings = get_user_listings(from_number)

                                    visible_listings = user_listings[offset:offset + MAX_ROWS]

                                    rows = []

                                    for biz in visible_listings:
                                        biz_id = biz.get('id', 'unknown')
                                        biz_name = biz.get('name', 'Unnamed Business')

                                        status_text = "✅ Live" if biz.get("is_verified") else "⏳ Checking..."

                                        rows.append({
                                            "id": f"select_{biz_id}",
                                            "title": biz_name[:24],
                                            "description": f"Status: {status_text} | Tap to manage"
                                        })

                                    # 👇 Add next-page button again if needed
                                    if len(user_listings) > offset + MAX_ROWS:
                                        next_offset = offset + MAX_ROWS

                                        rows.append({
                                            "id": f"manage_page_{next_offset}",
                                            "title": "➡️ Next Page",
                                            "description": "View more listings"
                                        })

                                    interactive_payload = {
                                        "messaging_product": "whatsapp",
                                        "recipient_type": "individual",
                                        "to": from_number,
                                        "type": "interactive",
                                        "interactive": {
                                            "type": "list",
                                            "header": {
                                                "type": "text",
                                                "text": "Manage Your Listings"
                                            },
                                            "body": {
                                                "text": "Select a listing below to manage."
                                            },
                                            "footer": {
                                                "text": f"Showing {offset + 1}-{offset + len(visible_listings)}"
                                            },
                                            "action": {
                                                "button": "More Listings",
                                                "sections": [{
                                                    "title": "Your Listings",
                                                    "rows": rows
                                                }]
                                            }
                                        }
                                    }

                                    guarded_send(
                                        phone_number_id,
                                        from_number,
                                        interactive_payload,
                                        message_id
                                    )

                                    return

                                if selection_id and selection_id.startswith("select_"):
                                    business_id = selection_id.replace("select_", "", 1)

                                    # Update existing session
                                    session["active_management_id"] = business_id
                                    session["flow"] = "awaiting_management_action"
                                    commit_session(from_number, session)

                                    send_interactive_button(
                                        phone_number_id, from_number,
                                        f"Listing ID: *{business_id}*\nWhat would you like to do?",
                                        buttons_list=[
                                            {"id": "btn_view", "title": "👁️ View Details"},
                                            {"id": "btn_delete", "title": "🗑️ Delete"}
                                        ]
                                    )
                                    return

                            # B. BUTTON REPLIES (Onboarding & Profile)
                            if interactive.get("type") == "button_reply":
                                btn_id = interactive.get("button_reply", {}).get("id")

                                # 1. Handle Terms Agreement
                                if btn_id == "user_onboarding_agree":
                                    session.update({
                                        "has_agreed_terms": True,
                                        "flow": "awaiting_onboarding_gender"
                                    })
                                    commit_session(from_number, session)

                                    gender_text = (
                                        "✅ *Terms Accepted.*\n\n"
                                        "To show you the most relevant local deals, services, and jobs, "
                                        "please select your gender:"
                                    )

                                    send_interactive_button(
                                        phone_number_id, from_number, gender_text,
                                        buttons_list=[
                                            {"id": "gender_male", "title": "Male 👨"},
                                            {"id": "gender_female", "title": "Female 👩"}
                                        ]
                                    )
                                    return


                                # 2. Handle Mandatory Gender Selection
                                elif btn_id.startswith("gender_"):
                                    selected_gender = btn_id.replace("gender_", "").capitalize()

                                    session.update({
                                        "user_gender": selected_gender,
                                        "gender": selected_gender,
                                        "has_agreed_terms": True,
                                        "is_registered": True,  # 👈 CRITICAL: Mark them as a member!
                                        "flow": None  # 🏁 Onboarding truly finished now
                                    })
                                    commit_session(from_number, session)

                                    reply = (
                                        f"Profile updated! I've set your gender to *{selected_gender}*. 🚀\n\n"
                                        "How can I help you today?\n"
                                        "👉 'I need a mechanic'\n"
                                        "👉 'Looking for a teaching job'\n\n"
                                        "🆘 *Quick Help*\n"
                                        "• Type *help* for commands\n"
                                        "• Type *manage listings* to manage posts\n"
                                        "• Type *reset* anytime to restart"
                                    )
                                    guarded_send(phone_number_id, from_number, reply, message_id)
                                    return

                                # 3. Handle Search Result "View Details"
                                elif btn_id.startswith("view_"):
                                    listing_id = btn_id.replace("view_", "")

                                    # A. TRACKING (Total vs Unique)
                                    try:
                                        # Reference to the specific viewer to check for duplicates
                                        # Path: listings/{listing_id}/viewers/{user_phone}
                                        viewer_ref = db.collection("listings").document(listing_id) \
                                            .collection("viewers").document(from_number)

                                        listing_ref = db.collection("listings").document(listing_id)

                                        # Use a transaction or a simple check-and-set
                                        if not viewer_ref.get().exists:
                                            # 🆕 First time this user has clicked this listing
                                            viewer_ref.set({
                                                "viewed_at": firestore.SERVER_TIMESTAMP
                                            })

                                            listing_ref.update({
                                                "total_views": firestore.Increment(1),  # Total engagement
                                                "unique_reach": firestore.Increment(1)  # Actual audience size
                                            })
                                            logging.info(f"✨ Unique reach recorded for {listing_id}")
                                        else:
                                            # 🔄 Repeat click: only increment total views
                                            listing_ref.update({
                                                "total_views": firestore.Increment(1)
                                            })
                                            logging.info(f"🔄 Repeat view recorded for {listing_id}")

                                    except Exception as e:
                                        logging.error(f"Failed to update tracking metrics: {e}")

                                    # B. FETCH
                                    doc = db.collection("listings").document(listing_id).get()

                                    if doc.exists:
                                        data = doc.to_dict()

                                        # C. DATA MAPPING (V4 - Dynamic Service/Job Logic)
                                        # 1. Identity & Context
                                        category = data.get("category", "product")
                                        title = data.get("title", "Listing Details")
                                        desc = data.get("description", "No description provided.")

                                        # 2. Location & Visibility (Using your 'visibility' key)
                                        loc_map = data.get("location", {})
                                        # Use .get() on data directly or loc_map depending on where it's stored
                                        addr = data.get("address") or loc_map.get("address")
                                        visibility = data.get("visibility", "On-site")
                                        lat = loc_map.get("lat")
                                        lng = loc_map.get("lng")

                                        # --- SMART RESOLVER ---
                                        if visibility == "Remote Service":
                                            addr_display = "🌐 Remote / Online"
                                            maps_link_display = "N/A (Online)"
                                        elif visibility == "Delivery Only":
                                            addr_display = "🚚 Delivery Only"
                                            maps_link_display = "N/A"
                                        elif addr and str(addr).lower() != "none":
                                            addr_display = addr
                                            maps_link_display = f"https://www.google.com/maps?q={lat},{lng}" if (
                                                        lat and lng) else "No link available"
                                        elif visibility == "On-site" and lat and lng:
                                            # This is the "Rescue" path for your Salon Job
                                            addr_display = "Abuja (Near your location)"
                                            maps_link_display = f"https://www.google.com/maps?q={lat},{lng}"
                                        else:
                                            addr_display = "Location on request"
                                            maps_link_display = "No link available"

                                        # 3. Dynamic Money Labeling (Using 'compensation' vs 'price')
                                        comp_val = data.get("compensation")  # For services/jobs
                                        price_val = data.get("price")  # For products

                                        if category == "job":
                                            label = "💰 *Salary:*"
                                            price_display = comp_val or "Disclosed on interview"
                                        elif category == "service":
                                            label = "💳 *Rate:*"
                                            price_display = comp_val or "Contact for Quote"
                                        else:
                                            label = "💰 *Price:*"
                                            try:
                                                # Only try to format as ₦ if it's a number
                                                price_display = f"₦{float(price_val):,.0f}" if price_val else "Negotiable"
                                            except:
                                                price_display = price_val or "Negotiable"

                                        # 4. Contact
                                        contact = data.get("contact_phone") or data.get("biz_phone") or "N/A"

                                        # D. BUILD & SEND (V4 - Categorized Display)

                                        # 1. Extract Media ID
                                        media_data = data.get("media", {})
                                        image_id = media_data.get("id")

                                        # 2. Send Image first if it exists
                                        if image_id:
                                            # Sending the image separately to keep the detail text clean
                                            send_whatsapp_image(phone_number_id, from_number, image_id, caption=None)

                                        # 3. Assemble Text Details
                                        # Note: We use the 'label' and 'desc' variables created in Part C
                                        details = (
                                            f"📋 *{title}*\n"
                                            f"📝 _{desc}_\n\n"
                                            f"📍 *Location:* {addr_display}\n"
                                            f"{label} {price_display}\n"
                                            f"📱 *Contact:* {contact}\n"
                                            f"🗺️ *Directions:* {maps_link_display}\n\n"
                                            f"🆔 *REF ID:* `{listing_id}`"
                                        )

                                        # 4. Final Delivery
                                        guarded_send(phone_number_id, from_number, details, message_id=None)

                                    else:
                                        # Safety fallback if the document was deleted while user was browsing
                                        guarded_send(phone_number_id, from_number,
                                                     "⚠️ This listing is no longer available.",
                                                     message_id=None)
                                    return




                                # 4. NEW: Handle Management Actions (View/Delete)
                                elif btn_id in ["btn_view", "btn_delete"]:
                                    business_id = session.get("active_management_id")

                                    if not business_id:
                                        guarded_send(phone_number_id, from_number,
                                                     "Session expired. Please type 'manage listings' again.", message_id)
                                        return

                                    # Action: Delete
                                    if btn_id == "btn_delete":
                                        success = perform_actual_deletion(business_id, from_number)
                                        if success:
                                            guarded_send(phone_number_id, from_number, "✅ Listing successfully deleted.",
                                                         message_id)
                                        else:
                                            guarded_send(phone_number_id, from_number,
                                                         "❌ Error: Could not delete this listing.", message_id)

                                    # Action: View Details
                                    elif btn_id == "btn_view":
                                        doc = db.collection("listings").document(business_id).get()
                                        if doc.exists:
                                            data = doc.to_dict()

                                            # 1. DYNAMIC MEDIA (Image Display)
                                            media = data.get('media', {})
                                            image_id = media.get('id')
                                            if image_id:
                                                # We send the image as a standalone, then the text follows
                                                send_whatsapp_image(phone_number_id, from_number, image_id,
                                                                    "Listing Preview")

                                            # 2. DYNAMIC FIELD BUILDING
                                            # We build the list of lines to ensure we don't show blank/N/A values
                                            lines = [f"🏢 *{data.get('title', 'Untitled Listing').title()}*"]

                                            # Conditional Fields
                                            if data.get('listing_type'):
                                                lines.append(
                                                    f"🏷️ *Type:* {data.get('listing_type').replace('_', ' ').capitalize()}")

                                            # Location Logic (Check nested map)
                                            loc = data.get('location', {})
                                            address = loc.get('address')
                                            lat = loc.get('lat')
                                            lng = loc.get('lng')

                                            # Check if we have a valid text address
                                            if address and address != "Unknown location":
                                                lines.append(f"📍 *Location:* {address}")
                                            # If no address, check if we have pin coordinates
                                            elif lat and lng:
                                                # Google Maps link for the user to click
                                                google_maps_link = f"https://www.google.com/maps/search/?api=1&query={lat},{lng}"
                                                lines.append(f"📍 *Location:* [View Location Pin]({google_maps_link})")
                                            else:
                                                lines.append(f"📍 *Location:* Not provided")

                                            if data.get('compensation'):
                                                lines.append(f"💰 *Price/Comp:* {data.get('compensation')}")

                                            contact = data.get("contact_phone") or data.get("biz_phone")
                                            if contact:
                                                lines.append(f"📞 *Contact:* {contact}")

                                            total_views = data.get("total_views", 0)
                                            unique_reach = data.get("unique_reach", 0)
                                            lines.append(f"👀 *Total Views:* {total_views} | 👥 *Unique:* {unique_reach}")

                                            # Visibility Logic
                                            visibility = data.get('visibility', 'Standard')
                                            lines.append(f"🛡️ *Visibility:* {visibility}")

                                            # Status
                                            status = data.get('verification_status', 'Checking').capitalize()
                                            lines.append(f"✅ *Status:* {status}")

                                            # Description
                                            desc = data.get('description') or data.get('search_text', 'No description.')
                                            lines.append(f"\n📝 *Details:*\n{desc}")

                                            # 3. SEND
                                            guarded_send(phone_number_id, from_number, "\n".join(lines), message_id)
                                        else:
                                            guarded_send(phone_number_id, from_number, "⚠️ Listing no longer exists.",
                                                         message_id)

                                    # --- CLEANUP (DEDENTED) ---
                                    # This now aligns with the 'if/elif' blocks above,
                                    # ensuring it runs for BOTH Delete and View actions.
                                    session.pop("active_management_id", None)
                                    session["flow"] = None
                                    commit_session(from_number, session)
                                    return


                                # 5. Handle Help Menu
                                elif btn_id == "open_help_menu":

                                    help_text = (
                                        "🤖 *Welcome to 4ound AI*\n"
                                        "━━━━━━━━━━━━━━\n\n"

                                        "4ound helps you find nearby:\n"
                                        "🛠️ Services\n"
                                        "🛍️ Products\n"
                                        "💼 Jobs\n\n"

                                        "🔍 *Search Examples*\n"
                                        "• Looking for a plumber near me\n"
                                        "• Find me a fashion designer\n"
                                        "• Need an iPhone 13 in Abuja\n"
                                        "• Chef jobs nearby\n"
                                        "• Looking for a mechanic around\n\n"

                                        "📢 *Sell / List Examples*\n"
                                        "• I want to sell my iPhone 12\n"
                                        "• Selling generator\n"
                                        "• I am a plumber\n"
                                        "• I do catering services\n"
                                        "• Register my business\n"
                                        "• List my fashion business\n\n"

                                        "💼 *Job Posting Examples*\n"
                                        "• Hiring a driver\n"
                                        "• I want to post a chef job\n"
                                        "• Need workers for my shop\n"
                                        "• Looking to hire a sales rep\n\n"

                                        "📍 *Location Tips*\n"
                                        "Send a location pin 📌 or type your area.\n\n"

                                        "⚙️ *Useful Commands*\n"
                                        "• reset\n"
                                        "• manage listings\n"
                                        "• status ABC123\n\n"

                                        "🗑️ *Delete Listings*\n"
                                        "Use the manage listings menu.\n\n"

                                        "💬 *Need Human Support/Something Else?*\n"
                                        "Telegram: https://t.me/Human4ound"
                                    )

                                    guarded_send(
                                        phone_number_id,
                                        from_number,
                                        help_text,
                                        message_id
                                    )

                                    return

                                # 6. Handle Pagination (See More Results)
                                elif btn_id.startswith("more_results_"):
                                    match = re.search(r"(\d+)$", btn_id)
                                    offset = int(match.group(1)) if match else 3

                                    session.update({
                                        "search_offset": offset,
                                        "route_intent": "search",
                                        "flow": "searching"
                                    })

                                    # 🟢 STEP 1: Send the message and capture the response
                                    response = guarded_send(
                                        phone_number_id,
                                        from_number,
                                        "_fetching more results..._ 🔄",
                                        None
                                    )

                                    # 🟢 STEP 2: Store the ID so Section 8 can find it
                                    # ✅ Fix: Use isinstance to check if it's a dictionary
                                    if isinstance(response, dict) and "messages" in response:
                                        session["loading_msg_id"] = response["messages"][0]["id"]
                                    else:
                                        # If guarded_send returned True/False, we can't get an ID
                                        session["loading_msg_id"] = None
                                        logger.warning(
                                            f"⚠️ Search bridge sent but no ID captured. Response type: {type(response)}")

                                    commit_session(from_number, session)

                                    # ❌ REMOVED: return (This allows the code to fall through to Section 8)


                        try:
                            # --- 5. GREETING / RESET / CONSENT ---
                            current_flow = session.get("flow")
                            text_lower = text.lower().strip()

                            # --- 0. ADMIN DELETE AD LOGIC ---
                            if text_lower.startswith("delete ad "):
                                # 🛡️ Only allow your number to delete ads
                                allowed_admins = [os.getenv("ADMIN_PHONE")]
                                if from_number not in allowed_admins:
                                    guarded_send(phone_number_id, from_number,
                                                 "❌ You are not authorized to delete ads.", message_id)
                                    return

                                target_id = text.lower().replace("delete ad ", "").strip().upper()
                                try:
                                    ad_ref = db.collection("active_ads").document(target_id)
                                    if ad_ref.get().exists:
                                        ad_ref.delete()
                                        guarded_send(phone_number_id, from_number,
                                                     f"🗑️ Ad `{target_id}` has been permanently deleted.", message_id)
                                    else:
                                        guarded_send(phone_number_id, from_number,
                                                     f"❓ Could not find an ad with ID: `{target_id}`", message_id)
                                except Exception as e:
                                    logger.error(f"Error deleting ad: {e}")
                                return

                            # --- 🔍 STATUS COMMAND (Unified - Ads + Listings) ---
                            if text_lower.startswith("status "):
                                try:
                                    parts = text.split(maxsplit=1)

                                    if len(parts) < 2:
                                        guarded_send(
                                            phone_number_id,
                                            from_number,
                                            "❌ Please provide an ID or Name (e.g., `status JOHNY_CAKES_758`)",
                                            message_id
                                        )
                                        return

                                    query_val = parts[1].strip()
                                    target_id = query_val.upper()
                                    data = None
                                    source = None

                                    # 1. Try direct ID lookup in active_ads
                                    ad_doc = db.collection("active_ads").document(target_id).get()
                                    if ad_doc.exists:
                                        data = ad_doc.to_dict()
                                        source = "ad"

                                    # 2. Try direct ID lookup in listings
                                    if not data:
                                        listing_doc = db.collection("listings").document(target_id).get()
                                        if listing_doc.exists:
                                            data = listing_doc.to_dict()
                                            source = "listing"

                                    # 3. Fallback: Search by name in active_ads
                                    if not data:
                                        name_query = db.collection("active_ads").where(
                                            "biz_name", "==", query_val).limit(1).stream()
                                        result = next(name_query, None)
                                        if result:
                                            data = result.to_dict()
                                            target_id = data.get("ad_id", target_id)
                                            source = "ad"

                                    # 4. Fallback: Search by title in listings
                                    if not data:
                                        name_query = db.collection("listings").where(
                                            "title", "==", query_val).limit(1).stream()
                                        result = next(name_query, None)
                                        if result:
                                            data = result.to_dict()
                                            target_id = data.get("listing_id", target_id)
                                            source = "listing"

                                    if data:
                                        title = data.get("biz_name") or data.get("title") or "Unnamed"
                                        goal = data.get("budgeted_reach") or data.get("target_reach") or "Unlimited"

                                        # Determine status accurately per type
                                        is_active_ad = data.get("is_active")
                                        is_live_listing = data.get("verification_status",
                                                                   "").lower() == "verified" or data.get("is_verified",
                                                                                                         False)
                                        active = "✅ Active" if (
                                                    is_active_ad or is_live_listing) else "🔴 Completed/Paused"

                                        # --- BASE HEADER ---
                                        report_lines = [
                                            f"📊 *4ound Status Report*",
                                            f"━━━━━━━━━━━━━━",
                                            f"🏢 *Name:* {title}",
                                            f"🚦 *Status:* {active}"
                                        ]

                                        # --- TYPE SPECIFIC METRICS ---
                                        if source == "listing":
                                            # Pull both listing metrics explicitly
                                            total_views = data.get("total_views", 0)
                                            unique_reach = data.get("unique_reach", 0)

                                            report_lines.extend([
                                                f"👀 *Current Reach (Total Views):* {total_views}",
                                                f"👥 *Unique Reach (Audience):* {unique_reach}"
                                            ])
                                        else:
                                            # Keep the unified performance metric for active ad campaigns
                                            ad_reach = data.get("current_reach", 0)
                                            report_lines.append(f"📈 *Unique Reach:* {ad_reach} / {goal}")

                                        # --- BASE FOOTER ---
                                        report_lines.extend([
                                            f"🆔 *ID:* {target_id}",
                                            f"━━━━━━━━━━━━━━",
                                            f"📢 *Note:* Status is public to anyone with the ID or Name."
                                        ])

                                        status_report = "\n".join(report_lines)

                                        # Send stats text report (Duplicate removed here)
                                        guarded_send(phone_number_id, from_number, status_report, message_id)

                                        # Send content preview (ads only)
                                        if source == "ad":
                                            ad_type = data.get("type", "text")
                                            ad_body = data.get("body", "")
                                            media_id = data.get("media_id")

                                            if ad_type == "image" and media_id:
                                                send_whatsapp_image(
                                                    phone_number_id,
                                                    from_number,
                                                    media_id,
                                                    caption=ad_body or "Ad Preview"
                                                )
                                            else:
                                                preview_text = f"_{ad_body}_" if ad_body else "_[No text content]_"
                                                guarded_send(phone_number_id, from_number, preview_text, message_id)

                                        # Send listing image if available (listings only)
                                        elif source == "listing":
                                            media_id = data.get("media", {}).get("id") or data.get("image_id")
                                            if media_id:
                                                send_whatsapp_image(
                                                    phone_number_id,
                                                    from_number,
                                                    media_id,
                                                    caption=f"Preview: {title}"
                                                )

                                    else:
                                        guarded_send(
                                            phone_number_id,
                                            from_number,
                                            f"❓ Nothing found for `{query_val}`. Check the ID or Name and try again.",
                                            message_id
                                        )

                                except Exception as e:
                                    logger.error(f"Status Command Error: {e}")
                                    guarded_send(
                                        phone_number_id,
                                        from_number,
                                        "❌ Error retrieving status. Please try again.",
                                        message_id
                                    )

                                return


                            # 🆘 HELP COMMAND
                            if text_lower in ["help", "commands", "menu", "what can you do"]:
                                help_text = (
                                    "🤖 *Welcome to 4ound AI*\n"
                                    "━━━━━━━━━━━━━━\n\n"

                                    "4ound helps you find nearby:\n"
                                    "🛠️ Services\n"
                                    "🛍️ Products\n"
                                    "💼 Jobs\n\n"

                                    "🔍 *Search Examples*\n"
                                    "• Looking for a plumber near me\n"
                                    "• Find me a fashion designer\n"
                                    "• Need an iPhone 13 in Abuja\n"
                                    "• Chef jobs nearby\n"
                                    "• Looking for a mechanic around\n\n"

                                    "📢 *Sell / List Examples*\n"
                                    "• I want to sell my iPhone 12\n"
                                    "• Selling generator\n"
                                    "• I am a plumber\n"
                                    "• I do catering services\n"
                                    "• Register my business\n"
                                    "• List my fashion business\n\n"

                                    "💼 *Job Posting Examples*\n"
                                    "• Hiring a driver\n"
                                    "• I want to post a chef job\n"
                                    "• Need workers for my shop\n"
                                    "• Looking to hire a sales rep\n\n"

                                    "📍 *Location Tips*\n"
                                    "Send a location pin 📌 or type your area.\n\n"

                                    "⚙️ *Useful Commands*\n"
                                    "• reset\n"
                                    "• manage listings\n"
                                    "• status ABC123\n\n"

                                    "🗑️ *Delete Listings*\n"
                                    "Use the manage listings menu.\n\n"

                                    "💬 *Need Human Support?*\n"
                                    "Telegram: https://t.me/Human4ound"
                                )

                                guarded_send(phone_number_id, from_number, help_text, message_id)
                                return


                            # A. RESET LOGIC (Soft Reset)
                            if text_lower in ["reset", "clear", "cancel", "stop", "none"]:
                                # 🟢 Update the session instead of deleting it
                                session.update({
                                    "flow": None,
                                    "pending_search": None,
                                    "pending_cat": None,
                                    "location_requested": False,
                                    "query_item": None,
                                    "biz_name": None,
                                    "biz_phone": None
                                })
                                commit_session(from_number, session)

                                reset_msg = (
                                    "🔄 Reset successful. What can I find for you now?\n\n"
                                    "🆘 *Quick Help*\n"
                                    "• Type *help* for all commands\n"
                                    "• Type *manage listings* to manage posts\n"
                                    "• Type *status YOUR_ID* to check views"
                                )

                                guarded_send(
                                    phone_number_id,
                                    from_number,
                                    reset_msg,
                                    message_id
                                )
                                return

                            # B. DELETE LISTING (OPT-OUT / MANAGEMENT)
                            if text_lower in ["delete my listing", "remove my business", "stop listing", "manage listings"]:
                                # 1. Fetch all listings owned by this phone number
                                user_listings = get_user_listings(from_number)

                                if not user_listings:
                                    reply = "Hmm, I couldn't find any active listings linked to your number. 🧐"
                                    guarded_send(phone_number_id, from_number, reply, message_id)
                                    continue

                                # 🚀 UNIFIED LOGIC: One or many, we show them a list so they can see the status first
                                # 🚀 PAGINATED MANAGEMENT LIST
                                MAX_ROWS = 9  # Keep 1 slot free for "Next Page"

                                # Start from first page
                                page_offset = 0

                                # Slice first batch
                                visible_listings = user_listings[page_offset:page_offset + MAX_ROWS]

                                rows = []

                                for biz in visible_listings:
                                    biz_id = biz.get('id', 'unknown')
                                    biz_name = biz.get('name', 'Unnamed Business')

                                    status_text = "✅ Live" if biz.get("is_verified") else "⏳ Checking..."

                                    rows.append({
                                        "id": f"select_{biz_id}",
                                        "title": biz_name[:24],
                                        "description": f"Status: {status_text} | Tap to manage"
                                    })

                                # 👇 Add pagination row if more listings exist
                                if len(user_listings) > MAX_ROWS:
                                    next_offset = MAX_ROWS

                                    rows.append({
                                        "id": f"manage_page_{next_offset}",
                                        "title": "➡️ Next Page",
                                        "description": "View more listings"
                                    })

                                interactive_payload = {
                                    "messaging_product": "whatsapp",
                                    "recipient_type": "individual",
                                    "to": from_number,
                                    "type": "interactive",
                                    "interactive": {
                                        "type": "list",
                                        "header": {"type": "text", "text": "Manage Your Listings"},
                                        "body": {"text": "Select a listing below to remove it from 4ound AI. 🗑️"},
                                        "footer": {"text": "Verification usually takes < 60s."},
                                        "action": {
                                            "button": "View My Posts",
                                            "sections": [{"title": "Your Active Listings", "rows": rows}]
                                        }
                                    }
                                }

                                guarded_send(phone_number_id, from_number, interactive_payload, message_id)
                                return

                            # C. ONBOARDING CONSENT (The "First Impression")
                            # If they haven't agreed yet AND they aren't already in the middle of a flow
                            if (not session.get("has_agreed_terms")) and not current_flow:
                                welcome_text = (
                                    "Welcome to *4ound AI*! 👋\n\n"
                                    "I help you find local services or list your own business in seconds.\n\n"
                                    "⚖️ *Terms:* By continuing, you agree to our terms and confirm you are "
                                    "authorized to provide the business details and location you share.\n\n"
                                    "🛡️ *Safety:* 4ound is a connector. Always verify sellers and meet in "
                                    "public places. Never pay before seeing a service!"
                                )

                                send_interactive_button(
                                    phone_number_id,
                                    from_number,
                                    welcome_text,
                                    buttons_list=[
                                        {"id": "user_onboarding_agree", "title": "✅ I Agree & Start"}
                                    ]
                                )
                                session["flow"] = "awaiting_onboarding"
                                commit_session(from_number, session)
                                return

                            # 🟢 STEP 2: ADMIN AD FLOW HANDLER
                            if current_flow and current_flow.startswith("admin_ad_"):
                                if current_flow == "admin_ad_name":
                                    biz_name = text.strip()
                                    # Clean name for the ID
                                    clean_name = re.sub(r'\W+', '', biz_name.replace(" ", "_"))
                                    ad_id = f"{clean_name}_{random.randint(100, 999)}"

                                    session["ad_draft"]["biz_name"] = biz_name
                                    session["ad_draft"]["ad_id"] = ad_id
                                    session["flow"] = "admin_ad_location"
                                    commit_session(from_number, session)

                                    msg = (
                                        f"✅ **Business Name Saved.**\n"
                                        f"🆔 Generated ID: `{ad_id}`\n\n"
                                        f"📍 *Step 2: Target Location*\n"
                                        f"Which cities should see this ad?\n"
                                        f"(e.g., 'Abuja, Lagos' or send 'All')."
                                    )
                                    guarded_send(phone_number_id, from_number, msg, message_id)
                                    return

                                # --- STEP 3: SAVE LOCATION (PIN, TEXT, OR EVERYWHERE) ---
                                elif current_flow == "admin_ad_location":
                                    locations = []
                                    input_text = text.strip().lower()

                                    if msg_type == "location":
                                        loc_data = message.get("location", {})
                                        lat, lng = loc_data.get("latitude"), loc_data.get("longitude")
                                        # ✅ Use new dual-path signature
                                        geo = extract_global_location(lat=lat, lng=lng, from_number=from_number)
                                        locations = [geo.get("town") or geo.get("name")] if geo.get("found") else [
                                            "Unknown"]
                                    elif input_text in ["everywhere", "world", "all", "global"]:
                                        locations = ["EVERYWHERE"]
                                    else:
                                        # 🔄 Geocode the typed address to extract clean city name
                                        geo = extract_global_location(text=text, from_number=from_number)
                                        if geo.get("found"):
                                            # Use city-level name for broad targeting, not street-level
                                            city = geo.get("state") or geo.get("town") or geo.get("name")
                                            locations = [city] if city else [loc.strip().title() for loc in
                                                                             text.split(",")]
                                        else:
                                            locations = [loc.strip().title() for loc in text.split(",")]

                                    session["ad_draft"]["target_locations"] = locations
                                    session["flow"] = "admin_ad_category"
                                    commit_session(from_number, session)

                                    msg = (
                                        f"📍 **Location(s) Set:** {', '.join(locations)}\n\n"
                                        f"🎯 *Step 3: Target Categories*\n"
                                        f"Which keywords trigger this?\n"
                                        f"(e.g., 'Real Estate' or send 'All' for any search)."
                                    )
                                    guarded_send(phone_number_id, from_number, msg, message_id)
                                    return

                                # --- STEP 4: SAVE CATEGORY -> ASK GENDER ---
                                elif current_flow == "admin_ad_category":
                                    input_text = text.strip().lower()
                                    if input_text in ["all", "any", "everything"]:
                                        categories = ["ALL"]
                                    else:
                                        categories = [cat.strip().lower() for cat in text.split(",")]

                                    session["ad_draft"]["target_categories"] = categories
                                    session["flow"] = "admin_ad_gender"
                                    commit_session(from_number, session)

                                    msg = (
                                        f"🎯 **Categories Set:** {', '.join(categories)}\n\n"
                                        f"👥 *Step 4: Target Gender*\n"
                                        f"Who should see this?\n"
                                        f"(Reply: 'Male', 'Female', or 'All')."
                                    )
                                    guarded_send(phone_number_id, from_number, msg, message_id)
                                    return

                                # --- STEP 5: SAVE GENDER -> ASK REACH ---
                                elif current_flow == "admin_ad_gender":
                                    gender_input = text.strip().capitalize()
                                    session["ad_draft"]["target_gender"] = gender_input
                                    session["flow"] = "admin_ad_reach"
                                    commit_session(from_number, session)

                                    msg = (
                                        f"👥 **Gender Set:** {gender_input}\n\n"
                                        f"📈 *Step 5: Reach*\n"
                                        f"How many unique people should see this ad before it stops?"
                                    )
                                    guarded_send(phone_number_id, from_number, msg, message_id)
                                    return

                                # --- STEP 6: SAVE REACH -> ASK FOR THE AD ITEM ---
                                elif current_flow == "admin_ad_reach":
                                    try:
                                        reach_limit = int(text.strip())
                                        session["ad_draft"]["budgeted_reach"] = reach_limit
                                        session["flow"] = "admin_ad_content"
                                        commit_session(from_number, session)

                                        msg = (
                                            f"📈 **Reach Set:** {reach_limit} people.\n\n"
                                            f"🖼️ *Step 6: The Ad Content*\n"
                                            f"Send the **Text** message or upload the **Photo** now.\n"
                                            f"(If you send a photo, I will also save the caption)."
                                        )
                                        guarded_send(phone_number_id, from_number, msg, message_id)
                                    except ValueError:
                                        guarded_send(phone_number_id, from_number, "❌ Please enter a valid number (e.g., 500).",
                                                     message_id)
                                    return

                                # --- STEP 7: RECEIVE CONTENT & FINALIZE ---
                                elif current_flow == "admin_ad_content":
                                    ad_draft = session.get("ad_draft", {})

                                    # 1. Capture Content (Text or Image)
                                    if msg_type == "image":
                                        ad_draft["type"] = "image"
                                        ad_draft["media_id"] = message.get("image", {}).get("id")
                                        ad_draft["body"] = message.get("image", {}).get("caption", "")
                                    else:
                                        ad_draft["type"] = "text"
                                        ad_draft["body"] = text
                                        ad_draft["media_id"] = None

                                    # 🛠️ CRITICAL SYNC: ID Normalization
                                    final_ad_id = str(ad_draft.get("ad_id", "UNKNOWN")).strip().upper()

                                    # 🟢 Initialize essential fields for the Status Command & Utility
                                    ad_draft.update({
                                        "ad_id": final_ad_id,
                                        "current_reach": 0,
                                        "seen_by": [],
                                        "is_active": True,
                                        "created_at": time.time()
                                    })

                                    # 2. Save to Firestore (Explicitly using 'active_ads')
                                    try:
                                        # We use final_ad_id as the DOCUMENT ID for instant lookup via 'status'
                                        db.collection("active_ads").document(final_ad_id).set(ad_draft)
                                        success = True
                                    except Exception as e:
                                        logger.error(f"Firestore Save Error: {e}")
                                        success = False

                                    if success:
                                        # Clean up locations for display
                                        loc_display = ", ".join(ad_draft.get("target_locations", ["Everywhere"]))

                                        report = (
                                            f"🚀 **ADVERT IS NOW LIVE**\n"
                                            f"━━━━━━━━━━━━━━\n"
                                            f"🆔 ID: `{final_ad_id}`\n"
                                            f"🏢 Biz: {ad_draft.get('biz_name')}\n"
                                            f"📍 Target: {loc_display}\n"
                                            f"📈 Goal: {ad_draft.get('budgeted_reach')} people\n"
                                            f"━━━━━━━━━━━━━━\n"
                                            f"📊 *Live Stats:* Type *'status {final_ad_id}'* to track performance.\n"
                                            f"✨ Send your secret code anytime to create another."
                                        )

                                        # 🧹 Session Cleanup
                                        session["flow"] = None
                                        if "ad_draft" in session:
                                            del session["ad_draft"]
                                        commit_session(from_number, session)

                                        guarded_send(phone_number_id, from_number, report, message_id)
                                    else:
                                        guarded_send(phone_number_id, from_number, "❌ Database Error. Ad not saved.",
                                                     message_id)

                                    return

                            # --- 6 --- INTENT PREDICTION ---
                            # --- REFRESH STATE AT THE VERY TOP ---
                            current_flow = session.get("flow")
                            is_registering = is_onboarding(current_flow)

                            # 🟢 FIX: Define 'interactive_data' using the 'message' variable from your loop
                            interactive_data = message.get("interactive", {})

                            # 🟢 SAFE INITIALIZATION: Define these BEFORE the bypass logic
                            # This prevents UnboundLocalError when AI block is skipped
                            predicted_intent = session.get("predicted_intent", "unknown")
                            confidence = session.get("confidence", 0)
                            detected_lang = session.get("detected_lang", "English")  # 👈 ADD THIS LINE HERE

                            # 🚨 --- NEW HARD BYPASS FOR PAGINATION --- 🚨
                            is_pagination = False  # 🛡️ Flag to prevent AI overwrite
                            if msg_type == "interactive":
                                # Safely get the ID from the interactive object
                                btn_id = (interactive_data.get("button_reply", {}).get("id") or
                                          interactive_data.get("list_reply", {}).get("id") or "")

                                if btn_id.startswith("more_results_"):
                                    is_pagination = True  # ✅ Set flag
                                    # 🛡️ Force search mode and stop AI from guessing 'quick_sell'
                                    predicted_intent = "search"
                                    session.update({
                                        "predicted_intent": "search",
                                        "route_intent": "search",
                                        "confidence": 1.0,
                                        "flow": "searching"
                                    })
                                    commit_session(from_number, session)
                                    logger.info(f"⚡ Hard Bypass: Pagination ({btn_id}) detected. Forcing SEARCH.")

                                    # We jump straight to Section 8 by setting is_registering to False
                                    is_registering = False

                            # 🛡️ ONLY predict if NOT already in a flow AND NOT a pagination click
                            current_flow = session.get("flow")
                            is_awaiting_location = current_flow == "awaiting_search_location"

                            if not is_registering and not is_pagination and msg_type != "location" and not is_awaiting_location:
                                res_intent, confidence, detected_lang = predict_intent_prototype(text)

                                # 🧼 Extract string from list (CORRECT FIX)
                                if isinstance(res_intent, list) and len(res_intent) > 0:
                                    predicted_intent = str(res_intent[0]).lower().strip()
                                elif isinstance(res_intent, str):
                                    predicted_intent = res_intent.lower().strip()
                                else:
                                    predicted_intent = "unknown"

                                # 🛡️ HARD-OVERRIDE: Explicit 'Sell' Check
                                if "sell" in text.lower() or "price of" in text.lower():
                                    predicted_intent = "quick_sell"
                                    logger.info("⚡ Manual Override: 'sell' detected. Forcing quick_sell intent.")

                                # 🛡️ SAVE & SYNC
                                session.update({
                                    "predicted_intent": predicted_intent,
                                    "confidence": confidence,
                                    "detected_lang": detected_lang
                                })

                                # 🟢 ONE-TIME RESOLUTION: Set the category globally
                                resolved_cat = resolve_category(predicted_intent, query=text)

                                # Safety Override: If it's a physical item, force 'product' even if AI is unsure
                                product_triggers = ["shoes", "clothes", "bags", "phone", "laptop", "watch", "furniture"]
                                if any(word in text.lower() for word in product_triggers):
                                    resolved_cat = "product"

                                session["category"] = resolved_cat
                                commit_session(from_number, session)
                                logger.info(f"🎯 Intent identified as: {predicted_intent} in {detected_lang}")

                            elif is_awaiting_location:
                                # 📍 User is responding to a location request — preserve search intent
                                predicted_intent = "search"
                                session.update({
                                    "predicted_intent": "search",
                                    "route_intent": "search"
                                })
                                commit_session(from_number, session)
                                logger.info(
                                    f"📍 Location text received. Preserving search intent, skipping intent engine.")

                            elif is_pagination:
                                # 1. Context Recovery: Restore original query
                                original_query = session.get("query")

                                if original_query:
                                    logger.info(f"🔄 Pagination: Restoring original query context: {original_query}")
                                    text = original_query
                                else:
                                    logger.warning("⚠️ Pagination triggered but no original query found in session.")

                                # 🚨 THE FIX: Restore coordinates to local variables immediately
                                lat = session.get("user_lat")
                                lng = session.get("user_lng")

                                logger.info(f"📍 Pagination: Restored coords from session: {lat}, {lng}")
                                logger.info(
                                    f"⏭️ Skipping Intent Engine for Pagination. Final Intent: {predicted_intent}")

                                # 2. Check for an override

                                route_override = session.get("route_intent")

                                if route_override and confidence < 0.85:
                                    predicted_intent = route_override

                                    session["category"] = resolve_category(predicted_intent, query=text)

                                    session["route_intent"] = None

                                    commit_session(from_number, session)

                                # 3. Handle unknown intents

                                if predicted_intent in ["unknown", "other", None] and confidence < 0.3:
                                    reply = get_local_response("general", "fallback", language=detected_lang)

                                    guarded_send(phone_number_id, from_number, reply, message_id)

                                    return

                                logger.info(f"🎯 Intent identified as: {predicted_intent} in {detected_lang}")

                            # 🚀 NEW: MULTI-INTENT BRIDGE (Syncing Brain to Logic Branches) 🚀

                            # CASE A: Provider/Seller/Recruiter Onboarding
                            if predicted_intent in ["provider_onboarding", "offer_job"]:

                                # 1. RECRUITER PATH
                                if session.get('mode') == "RECRUITER_ONBOARDING" or predicted_intent == "offer_job":
                                    predicted_intent = "recruiter_onboarding"

                                    # 🟢 FIX: Set flow to None.
                                    # This allows Section 7 to run the Gender Gate first.
                                    session.update({
                                        "flow": None,
                                        "mode": "RECRUITER_ONBOARDING",
                                        "predicted_intent": "recruiter_onboarding"
                                    })
                                    commit_session(from_number, session)
                                    logger.info(f"💼 Recruiter detected: Resetting flow for onboarding gate.")

                                # 2. PROVIDER/SELLER PATH
                                else:
                                    predicted_intent = "offer"
                                    session.update({
                                        "flow": None,
                                        "predicted_intent": "offer"
                                    })
                                    commit_session(from_number, session)
                                    logger.info(f"⚡ Provider detected: Resetting flow for onboarding gate.")



                            # CASE B: Market Search (Services/Items)
                            elif predicted_intent == "search":
                                # 🛡️ SELF-HEALING FALLBACK (Bug-Free Edition)
                                if not session.get("query"):
                                    raw_query = text or "General"  # Fix: Set default before assignments
                                    try:
                                        # 1. Capture the raw return from your cleaner
                                        raw_output = clean_user_text(text)

                                        # 🛡️ FIX: Ensure we are working with a STRING, not a LIST
                                        if isinstance(raw_output, list):
                                            fallback_clean = " ".join(raw_output)
                                        else:
                                            fallback_clean = str(raw_output) if raw_output else ""

                                        # ✅ Safety Check: Ensure text exists before splitting
                                        if fallback_clean.strip():
                                            # ✂️ Split service from location bridge
                                            parts = re.split(
                                                r"\b(in|at|near|around|inside|close\s+to|wey\s+dey|side|by)\b",
                                                fallback_clean,
                                                maxsplit=1,
                                                flags=re.IGNORECASE
                                            )
                                            # ✅ FIX: Use index because 'parts' is a list from re.split
                                            # ✅ THE FIX:
                                            if parts and isinstance(parts, list) and parts[0].strip():
                                                raw_query = parts[0].strip()
                                            else:
                                                raw_query = text or "General"

                                        # 🧠 SMART UPGRADE: Remove intent fluff and question words
                                        clean_query = re.sub(
                                            r"\b(i|need|want|get|find|looking|for|where|can|is|are|please|do|does|how|much|many|what|when|which)\b",
                                            "",
                                            raw_query,
                                            flags=re.IGNORECASE
                                        ).strip()

                                        session["query"] = clean_query.title() if clean_query else "General"
                                        logger.info(f"🩹 Healed query: {session['query']}")

                                    except Exception as e:
                                        logger.error(f"Error healing query: {e}")
                                        # Final fallback so the bot doesn't die
                                        session["query"] = text.title() if (
                                                    text and isinstance(text, str)) else "General"

                                # 📍 2. HEAL LOCATION + COORDINATES
                                if not session.get("location"):
                                    geo = extract_global_location(text, from_number=from_number)

                                    if geo.get("found"):
                                        session["location"] = (
                                                geo.get("town")
                                                or geo.get("state")
                                                or geo.get("country")
                                        )

                                        session["city"] = geo.get("city")
                                        session["state"] = geo.get("state")
                                        session["country"] = geo.get("country")

                                        # 🆕 Save coordinates too
                                        session["user_lat"] = geo.get("lat")
                                        session["user_lng"] = geo.get("lng")

                                        commit_session(from_number, session)

                                        logger.info(
                                            f"📍 Healed location + coords: "
                                            f"{session.get('location')} | "
                                            f"{geo.get('lat')}, {geo.get('lng')}"
                                        )


                                # 🔄 3. SYNC TO SESSION
                                session["query_item"] = session.get("query")
                                commit_session(from_number, session)

                            # CASE C: Employment Search (Job Seekers)
                            elif predicted_intent == "search_employment":
                                service_keywords = ["plumber", "mechanic", "tailor", "cleaner", "doctor", "barber",
                                                    "carpenter"]
                                # 🆕 ADD THESE:
                                product_keywords = ["shoes", "clothes", "sneakers", "bags", "laptop", "watch", "phone",
                                                    "iphone", "furniture"]
                                user_text_low = text.lower()

                                # 🛡️ THE PRODUCT REDIRECT (The Fix for Shoes)
                                if any(k in user_text_low for k in product_keywords):
                                    logger.info(f"🔄 Redirecting Product Request: {text} -> MARKET_SEARCH")
                                    session["mode"] = "MARKET_SEARCH"  # 👈 Force change session
                                    predicted_intent = "search"
                                    session["category"] = "product"  # 👈 Explicitly set category

                                # Existing Service Redirect
                                elif any(k in user_text_low for k in service_keywords):
                                    logger.info(f"🔄 Redirecting Service Request: {text} -> MARKET_SEARCH")
                                    session["mode"] = "MARKET_SEARCH"
                                    predicted_intent = "search"
                                else:
                                    session["mode"] = "EMPLOYMENT_SEARCH"
                                    logger.info(f"🕵️ Job Seeker confirmed")

                                session["query_item"] = session.get("job_query") or text
                                commit_session(from_number, session)
                                predicted_intent = "search"



                            # CASE D: Instant Greeting
                            elif predicted_intent == "greeting":
                                # 1. Get the greeting text
                                reply = get_local_response("general", None, language=detected_lang, sub_key="greeting")
                                guarded_send(phone_number_id, from_number, reply, message_id)

                                # 2. Check the ID Card (The "Safe-Passage" Check)
                                is_registered = session.get("is_registered") is True
                                has_agreed = session.get("has_agreed_terms") is True

                                # Only send the onboarding button if they are truly new
                                # (i.e., NOT registered AND haven't started the terms process)
                                if not is_registered and not has_agreed:
                                    terms_text = "Welcome to 4ound! 🚀 Please accept our terms to start searching or posting."
                                    send_interactive_button(
                                        phone_number_id,
                                        from_number,
                                        terms_text,
                                        buttons_list=[{"id": "user_onboarding_agree", "title": "I Agree ✅"}]
                                    )
                                    logger.info(f"🆕 New User {from_number}: Sent Onboarding.")
                                else:
                                    # If they are registered OR they are already in the middle of onboarding (has_agreed),
                                    # do nothing. This prevents the "Double-Prompt" loop.
                                    logger.info(
                                        f"✅ Member/In-Progress {from_number}: Greeting sent, skipping terms prompt.")

                                # --- KEEP THIS RETURN ---
                                # This ensures the code exits the function now that the greeting is handled.
                                return


                            # ✅ CASE E: Quick Sell (Items/Quick Sales)
                            elif predicted_intent == "quick_sell":
                                session.update({
                                    "prompt_namespace": "offer_quick",
                                    "listing_type": "quick_sale",
                                    "flow": "awaiting_quick_sell_item_name"  # 🟢 Set this specifically
                                })

                                logger.info(f"🛒 Quick Sell Bridge: Routing {from_number} to Item Onboarding")
                                # These final two lines ensure whatever intent was found is saved for Section 7
                                session["predicted_intent"] = predicted_intent
                                commit_session(from_number, session)
                            else:
                                # 🔒 FLOW LOCK (ONLY when user is actually in a flow)
                                predicted_intent = session.get("predicted_intent", "quick_sell")
                                detected_lang = session.get("detected_lang", "English")
                                confidence = session.get("confidence", 1.0)

                                logger.info(f"🔒 Shield Active: User in {current_flow}. Bypassing Intent Engine.")

                            # 🆕 --- 6.5 THE INTERRUPT GUARD ---
                            # 🛡️ Initialize the Gate
                            should_process_intent = True

                            if is_registering and msg_type in ["text", "image"]:

                                # 1. Define HARD reset commands
                                hard_reset_commands = ["reset", "restart", "start over", "cancel", "stop"]
                                is_manual_reset = text.lower().strip() in hard_reset_commands

                                # 2. 🟢 EXPANDED: Intents that are allowed to "Break" a sticky flow
                                different_category_intents = [
                                    "search", "help", "offer_job",
                                    "recruiter_onboarding", "provider_onboarding", "quick_sell"
                                ]

                                # 🛡️ NEW LOGIC: Check if the new intent is actually DIFFERENT from the current path
                                is_same_intent = (
                                        (predicted_intent in ["offer_job", "recruiter_onboarding"] and session.get(
                                            "listing_type") == "quick_job") or
                                        (predicted_intent == "quick_sell" and session.get(
                                            "listing_type") == "quick_sale") or
                                        (predicted_intent == "provider_onboarding" and session.get(
                                            "listing_type") == "professional")
                                )

                                # 3. Reset logic - Only reset if it's a MANUAL reset or a TRULY DIFFERENT intent

                                # 🆕 THE FIX: Don't let a "Search" intent break a "Search" flow!
                                # We only want to break if the new intent is something totally different (like help or onboarding)
                                is_searching = current_flow in ["awaiting_search_item", "awaiting_search_location"]
                                is_new_search_intent = (predicted_intent == "search")

                                if is_manual_reset or (
                                        predicted_intent in different_category_intents
                                        and confidence > 0.85
                                        and not is_same_intent
                                        and not (is_searching and is_new_search_intent)  # 👈 THIS LINE IS THE MAGIC
                                ):
                                    logger.info(
                                        f"🛡️ Guarded: Intent {predicted_intent} detected. Breaking {current_flow} flow.")
                                    session.update({
                                        "flow": None,
                                        "route_intent": predicted_intent,
                                        "predicted_intent": predicted_intent
                                    })
                                    current_flow = None
                                    commit_session(from_number, session)
                                    should_process_intent = True


                                # 👇 THE SPECIFIC FIX IS HERE
                                else:
                                    # If the user is in an onboarding flow, we muffle (to protect the data entry)
                                    if is_onboarding(current_flow):
                                        logger.info(
                                            f"🔒 Shield Active: User is in {current_flow} (Onboarding). Muffling input.")
                                        should_process_intent = False
                                    else:
                                        # If they are in a search flow (awaiting_search_item), we keep the gate OPEN
                                        # so Section 8 can process their query.
                                        logger.info(
                                            f"🔓 Shield Passive: User is in {current_flow} (Search). Passing to Section 8.")
                                        should_process_intent = True


                            # 🆕 --- 7. THE PROCESSING GATE ---
                            # 🛡️ GLOBAL INITIALIZATION (Add these here!)
                            # This ensures that even if AI logic was skipped, the gate doesn't crash.
                            should_process_intent = locals().get('should_process_intent', True)
                            predicted_intent = locals().get('predicted_intent', None)
                            detected_lang = locals().get('detected_lang', 'English')  # Good to have as a backup too!

                            # 1. FIX: Initialize these globally so the Gatekeeper can always see them
                            current_intent = None
                            onboarding_intents = [
                                "offer", "quick_sell", "recruiter_onboarding",
                                "search_employment", "provider_onboarding",
                                "offer_job"
                            ]
                            guarded_response_sent = False

                            if should_process_intent:

                                # 🆕 STEP A: Run the Rule Engine first!
                                rule_results = rule_quick_sell(text)
                                is_rule_product = any(r[0] == "quick_sell" and r[1] > 0.90 for r in rule_results)

                                # --- 7. ONBOARDING BRANCH (OFFER & QUICK SALE) ---
                                if is_onboarding(current_flow):
                                    current_intent = session.get("predicted_intent")
                                else:
                                    # 🆕 STEP B: If the Rule Engine found a product, FORCE the intent to quick_sell
                                    if is_rule_product:
                                        current_intent = "quick_sell"
                                        logger.info("⚡ Priority Override: Rule Engine forced quick_sell")
                                    else:
                                        current_intent = predicted_intent or session.get("predicted_intent")

                            else:
                                logger.info(f"✅ Logic bypassed. 'should_process_intent' is False. Welcome message avoided.")

                            # THE GATEKEEPER
                            # Now this is safe: onboarding_intents is guaranteed to exist!
                            if current_intent in onboarding_intents or is_onboarding(current_flow):

                                # Ensure the session knows we've upgraded 'offer' to 'quick_sell'
                                session["predicted_intent"] = current_intent
                                logger.info(f"🔥 SECTION 7 ACTIVE | Flow: {current_flow} | Intent: {current_intent}")

                                # --- 🛡️ SIMPLIFIED PROFILE GATE ---

                                # --- 🛡️ PROFILE GATE ---

                                # 1. GUARDIAN CHECK
                                if not session:
                                    logger.warning(f"⚠️ Guard Block: Session empty for {from_number}.")
                                    return

                                # 2. PROFILE CHECK
                                user_gender = session.get("user_gender") or session.get("gender")

                                # 🩹 Self-healing for older sessions
                                if session.get("gender") and not session.get("user_gender"):
                                    session["user_gender"] = session["gender"]
                                    commit_session(from_number, session)
                                    user_gender = session["gender"]

                                # 3. BLOCK IF INCOMPLETE
                                if not user_gender:
                                    reminder = (
                                        "Welcome! 🚀 Before we proceed, "
                                        "please select your gender to set up your profile."
                                    )

                                    send_interactive_button(
                                        phone_number_id,
                                        from_number,
                                        reminder,
                                        buttons_list=[
                                            {"id": "gender_male", "title": "Male 👨"},
                                            {"id": "gender_female", "title": "Female 👩"}
                                        ]
                                    )

                                    guarded_response_sent = True
                                    return

                                # A. START: Initial Intent Hand-off
                                if not current_flow or current_flow == "idle":

                                    # 1. FIX: Correct the Rule Check (c is a tuple: (intent, score, data))
                                    rule_results = rule_quick_sell(text)
                                    is_rule_product = any(r[0] == "quick_sell" and r[1] > 0.90 for r in rule_results)

                                    # 2. Initialize smart_data from cache OR calculate it
                                    real_item = None
                                    smart_data = session.get("smart_data_cache")

                                    if not smart_data:
                                        raw_text = text.strip()

                                        # 1. Expanded Noise Filter: Added "looking", "workers", "company", "need", "hire", etc.
                                        # Added: list, listing, service, services, item, product, items
                                        noise = r"\b(i|do|does|am|my|looking|company|business|register|registration|at|need|hire|hiring|want|to|a|an|the|is|it|de|wan|this|tokunbo|used|fairly|got|has|have|sale|sell|selling|somebody|someone|list|listing|service|services|item|product|items|professional|pro|offer|offers)\b"

                                        # 2. Extract and Clean
                                        extracted_item = re.sub(noise, "", raw_text, flags=re.IGNORECASE).strip()
                                        extracted_item = " ".join(extracted_item.split()).title()

                                        # 👇 ADD IT HERE
                                        bad_phrases = [
                                            "Register Business",
                                            "Business",
                                            "Professional Service",
                                            "Service",
                                            "Offer Service"
                                        ]

                                        if extracted_item in bad_phrases:
                                            extracted_item = ""

                                        # 🛡️ THE GENERIC GUARD:
                                        # If the user only said "Service" or "Product", treat it as vague
                                        generic_words = [
                                            "Service", "Services",
                                            "Product", "Products",
                                            "Item", "Items",
                                            "Business", "Businesses",
                                            "Work", "Job",
                                            "Store", "Company"
                                        ]
                                        if extracted_item in generic_words:
                                            extracted_item = ""

                                            # 3. 🛡️ THE SENTENCE GUARD:
                                        word_count = len(extracted_item.split())
                                        is_vague = not extracted_item or word_count > 3

                                        # Use the extracted item if it's short/clean; otherwise, use None to trigger "the role" or "your item"
                                        real_item = extracted_item if not is_vague else None

                                        # Determine final intent
                                        final_intent = "quick_sell" if (
                                                    predicted_intent == "quick_sell" or is_rule_product) else predicted_intent

                                        # Define the dictionary clearly
                                        smart_data = {
                                            "item": real_item,
                                            # For jobs, "the" works well as a fallback: "the role"
                                            # For products, "your item" works well.
                                            "display_item": real_item if real_item else "",
                                            "language": session.get("detected_lang", "English"),
                                            "is_physical_product": final_intent == "quick_sell",
                                            "is_job": final_intent in ["recruiter_onboarding", "search_employment",
                                                                       "offer_job"],
                                            "is_vague": is_vague
                                        }

                                    # ✅ FIX: smart_data is now guaranteed to exist before this point
                                    is_product = smart_data.get("is_physical_product", False)
                                    is_job = smart_data.get("is_job", False)
                                    display_item = smart_data.get("display_item") or ("the job" if is_job else "your item")  # Safe access

                                    # 🧹 Clean the cache
                                    session["smart_data_cache"] = None

                                    if is_product:
                                        listing_type = "quick_sale"
                                        prompt_namespace = "offer_quick"
                                        start_key = "start"  # 🟢 ADDED THIS
                                        if smart_data.get("is_vague"):
                                            prompt_key = "ask_item_name"
                                            session["flow"] = "awaiting_quick_sell_item_name"
                                        else:
                                            prompt_key = "ask_item_info"
                                            session["flow"] = "awaiting_item_info"

                                    elif is_job:
                                        listing_type = "quick_job"
                                        prompt_namespace = "offer_job"
                                        start_key = "start"  # 🟢 ADDED THIS
                                        prompt_key = "ask_job_details"
                                        session["flow"] = "awaiting_job_details"

                                    else:
                                        listing_type = "professional"
                                        prompt_namespace = "offer_pro"

                                        if not real_item:
                                            start_key = "start_generic"
                                            prompt_key = "ask_details_generic"
                                        else:
                                            start_key = "start"
                                            prompt_key = "ask_details"

                                        session["flow"] = "awaiting_offer_details"

                                    current_flow = session["flow"]

                                    # 🛡️ CLEAN UPDATE: Notice 'offer_description' is removed from here
                                    session.update({
                                        "listing_type": listing_type,
                                        "detected_lang": detected_lang,
                                        "item_name": real_item or session.get("item_name"),
                                        # 🔑 NEVER overwrite with fake value
                                        "prompt_namespace": prompt_namespace,
                                        "image_id": None,
                                        "is_verified": True
                                    })

                                    # ---B Step 1 THE RANDOMIZER LOGIC (With Safety Fixes) ---

                                    # 1. Randomize the "Start" message (Intro)
                                    # ✅ PASS real_item (or display_item) here so get_local_response can use it!
                                    start_options = get_local_response(
                                        prompt_namespace,
                                        item=display_item,
                                        language=detected_lang,
                                        sub_key=start_key
                                    )

                                    # Bug #8 Fix: Ensure start_options exists and handle list/string
                                    if isinstance(start_options, list) and start_options:
                                        intro = random.choice(start_options)
                                    else:
                                        intro = start_options or "Great! Let's get started with {item}."

                                    # Use the actual name we just extracted or saved
                                    item_to_display = real_item or ("the position" if is_job else "your item")

                                    # Only inject formatting if the template actually uses {item}
                                    if "{item}" in intro:
                                        dynamic_reply = intro.replace("{item}", f"*{item_to_display}*")
                                    else:
                                        dynamic_reply = intro


                                    # 2. Pick the right question prompt
                                    # ✅ PASS real_item here too!
                                    raw_prompt = get_local_response(
                                        prompt_namespace,
                                        item=display_item,
                                        language=detected_lang,
                                        sub_key=prompt_key
                                    )

                                    # Safety check for the prompt
                                    name_prompt = random.choice(raw_prompt) if isinstance(raw_prompt, list) else raw_prompt

                                    commit_session(from_number, session)
                                    guarded_send(phone_number_id, from_number, f"{dynamic_reply}\n\n{name_prompt}",
                                                 message_id)
                                    guarded_response_sent = True
                                    return


                                # --- B. STEP 2: Handle Response based on the Flow ---
                                prompt_namespace = session.get("prompt_namespace", "offer_pro")
                                detected_lang = session.get("detected_lang", "English")

                                # 🔍 THE PERFECT SPOT FOR DEBUGGING
                                logger.info(
                                    f"📸 DEBUG IMAGE CHECK | Flow: {current_flow} | msg_type={msg_type} | keys={list(message.keys())}")

                                # --- 2.0: CATCH VAGUE ITEM NAME ---
                                if current_flow == "awaiting_quick_sell_item_name":
                                    session.update({
                                        "item_name": text.title(),
                                        "flow": "awaiting_item_info"
                                    })

                                    raw_prompt = get_local_response(
                                        "offer_quick",
                                        session.get("item_name"),  # 👈 Pass the actual name here
                                        language=detected_lang,
                                        sub_key="ask_item_info"
                                    )

                                    prompt_text = random.choice(raw_prompt) if isinstance(raw_prompt, list) else raw_prompt

                                    # 🟢 FIX: Clean replace logic
                                    final_prompt = prompt_text.replace("{item}", text.title())

                                    commit_session(from_number, session)
                                    guarded_send(phone_number_id, from_number, final_prompt, message_id)
                                    guarded_response_sent = True
                                    return

                                # 1. CATCH ITEM INFO (Quick Sale Only)
                                elif current_flow == "awaiting_item_info":
                                    session.update({
                                        "item_info": text,
                                        "flow": "awaiting_offer_image"
                                    })
                                    # Pull "ask_photo" from JSON
                                    raw_prompt = get_local_response(
                                        "offer_quick",
                                        session.get("item_name"),  # 👈 This ensures it says "photo of the Sandals"
                                        language=detected_lang,
                                        sub_key="ask_photo"
                                    )
                                    prompt = random.choice(raw_prompt) if isinstance(raw_prompt, list) else (
                                                raw_prompt or "📸 Please send a clear photo of the item.")

                                    commit_session(from_number, session)
                                    guarded_send(phone_number_id, from_number, prompt, message_id)
                                    guarded_response_sent = True
                                    return

                                # 2. CATCH JOB DETAILS (Quick Jobs Only)
                                elif current_flow == "awaiting_job_details":
                                    session.update({
                                        "job_details": text,
                                        "flow": "awaiting_biz_name"  # Redirect to the new Company Name step
                                    })

                                    # Fetch the "ask_name" prompt from your responses.json
                                    raw_biz_prompt = get_local_response(
                                        prompt_namespace,
                                        None,
                                        language=detected_lang,
                                        sub_key="ask_name"
                                    )
                                    prompt = random.choice(raw_biz_prompt) if isinstance(raw_biz_prompt,
                                                                                         list) else raw_biz_prompt

                                    commit_session(from_number, session)
                                    guarded_send(phone_number_id, from_number, prompt, message_id)
                                    guarded_response_sent = True
                                    return


                                # 2.5 CATCH COMPANY/BIZ NAME
                                elif current_flow == "awaiting_biz_name":
                                    # 1. Capture the business name and advance the flow
                                    session.update({
                                        "biz_name": text,
                                        "flow": "awaiting_job_salary"
                                    })
                                    commit_session(from_number, session)

                                    # 2. Fetch the NEXT prompt (Salary) from your responses.json
                                    raw_salary_prompt = get_local_response(
                                        prompt_namespace,
                                        None,
                                        language=detected_lang,
                                        sub_key="ask_job_salary"
                                    )
                                    prompt = random.choice(raw_salary_prompt) if isinstance(raw_salary_prompt,
                                                                                            list) else raw_salary_prompt

                                    # 3. Send the salary prompt to the user
                                    guarded_send(phone_number_id, from_number, prompt, message_id)
                                    guarded_response_sent = True
                                    return


                                # 3. CATCH SALARY (Quick Jobs Only)
                                elif current_flow == "awaiting_job_salary":
                                    # 🌍 Global Logic: Parse numeric value and detect the correct currency symbol
                                    numeric_val, symbol, formatted_val = parse_nigerian_price(text, from_number)

                                    if numeric_val > 0:
                                        session.update({
                                            "temp_price": numeric_val,
                                            "currency": symbol,
                                            "formatted_price": formatted_val,
                                            "flow": "awaiting_job_frequency"
                                        })
                                        commit_session(from_number, session)

                                        # Dynamic currency symbol in the message (₦, $, GH₵, etc.)
                                        msg = f"Got it! Is *{formatted_val}* the rate per hour, day, or month?"
                                        send_interactive_button(
                                            phone_number_id,
                                            from_number,
                                            msg,
                                            buttons_list=[
                                                {"id": "freq_hour", "title": "Per Hour ⏱️"},
                                                {"id": "freq_day", "title": "Per Day ☀️"},
                                                {"id": "freq_month", "title": "Per Month 🗓️"}
                                            ]
                                        )
                                        guarded_response_sent = True
                                        return
                                    else:
                                        guarded_send(phone_number_id, from_number,
                                                     "I didn't catch a valid number. Please type the amount (e.g., 50k or 2000).",
                                                     message_id)
                                        guarded_response_sent = True
                                        return

                                # 3.5 CATCH FREQUENCY BUTTON
                                elif current_flow == "awaiting_job_frequency":
                                    # 🕵️ Check if the message is actually a button click
                                    msg_type = message.get("type")
                                    selection_id = None

                                    if msg_type == "interactive":
                                        selection_id = message.get("interactive", {}).get("button_reply", {}).get("id")
                                    else:
                                        # Fallback: if they manually typed "Per Month" or similar
                                        raw_input = text.strip().lower()
                                        if "hour" in raw_input:
                                            selection_id = "freq_hour"
                                        elif "day" in raw_input:
                                            selection_id = "freq_day"
                                        elif "month" in raw_input:
                                            selection_id = "freq_month"

                                    if not selection_id:
                                        # If we still don't have a selection, tell them to use the buttons
                                        guarded_send(phone_number_id, from_number,
                                                     "Please select one of the options below to continue. 👇")
                                        return

                                    freq_map = {"freq_hour": "hour", "freq_day": "day", "freq_month": "month"}
                                    selection = freq_map.get(selection_id, "total")

                                    price = session.get("temp_price", 0)
                                    symbol = session.get("currency", "₦")

                                    # Format: e.g., ₦450,000/month
                                    formatted_val = f"{symbol}{price:,.0f}/{selection}" if selection != "total" else f"{symbol}{price:,.0f}"

                                    session.update({
                                        "price": price,
                                        "formatted_price": formatted_val,
                                        "pay_period": selection,
                                        "flow": "awaiting_offer_phone"  # 🚨 Ensure this matches your phone flow name!
                                    })
                                    commit_session(from_number, session)

                                    raw_phone_prompt = get_local_response(
                                        prompt_namespace,
                                        item=session.get("item_name"),
                                        price=formatted_val,
                                        language=detected_lang,
                                        sub_key="ask_phone"
                                    )
                                    prompt = random.choice(raw_phone_prompt) if isinstance(raw_phone_prompt,
                                                                                           list) else raw_phone_prompt

                                    guarded_send(phone_number_id, from_number, prompt, message_id)
                                    guarded_response_sent = True
                                    return


                                # 4a. CATCH PROFESSIONAL DESCRIPTION (For Tailors, Devs, etc.)
                                elif current_flow == "awaiting_offer_details":
                                    session.update({
                                        "offer_description": text,  # 🧠 This powers your Semantic Search!
                                        "flow": "awaiting_offer_name"
                                    })

                                    # Now ask for the Business or Professional Name
                                    raw_prompt = get_local_response(prompt_namespace, None, language=detected_lang,
                                                                    sub_key="ask_name")
                                    prompt = random.choice(raw_prompt) if isinstance(raw_prompt, list) else raw_prompt

                                    commit_session(from_number, session)
                                    guarded_send(phone_number_id, from_number, prompt, message_id)
                                    guarded_response_sent = True
                                    return

                                # 4b. CATCH BUSINESS NAME (Updated to jump to Pricing)
                                elif current_flow == "awaiting_offer_name":
                                    session.update({
                                        "biz_name": text,
                                        "flow": "awaiting_offer_price"  # Changed from awaiting_offer_phone
                                    })
                                    commit_session(from_number, session)

                                    # Prompt for the service rate
                                    guarded_send(phone_number_id, from_number,
                                                 "What is your base rate or starting price for this service? Example: 5k or 50,000", message_id)
                                    guarded_response_sent = True
                                    return

                                # 4c. CATCH OFFER PRICE
                                elif current_flow == "awaiting_offer_price":
                                    numeric_val, symbol, formatted_val = parse_nigerian_price(text, from_number)

                                    if numeric_val > 0:
                                        session.update({
                                            "temp_price": numeric_val,
                                            "currency": symbol,
                                            "formatted_price": formatted_val,
                                            "flow": "awaiting_offer_frequency"
                                        })
                                        commit_session(from_number, session)

                                        msg = f"Got it! Is *{formatted_val}* your rate per hour, day, or month?"
                                        send_interactive_button(
                                            phone_number_id,
                                            from_number,
                                            msg,
                                            buttons_list=[
                                                {"id": "freq_hour", "title": "Per Hour ⏱️"},
                                                {"id": "freq_day", "title": "Per Day ☀️"},
                                                {"id": "freq_month", "title": "Per Month 🗓️"}
                                            ]
                                        )
                                        guarded_response_sent = True
                                        return
                                    else:
                                        guarded_send(phone_number_id, from_number,
                                                     "Please type a valid amount (e.g., 50k or 2000).", message_id)
                                        guarded_response_sent = True
                                        return

                                # 4d. CATCH OFFER FREQUENCY
                                elif current_flow == "awaiting_offer_frequency":
                                    # 🕵️ Handle button click or manual text
                                    msg_type = message.get("type")
                                    selection_id = None

                                    if msg_type == "interactive":
                                        selection_id = message.get("interactive", {}).get("button_reply", {}).get("id")
                                    else:
                                        raw_input = text.strip().lower()
                                        if "hour" in raw_input:
                                            selection_id = "freq_hour"
                                        elif "day" in raw_input:
                                            selection_id = "freq_day"
                                        elif "month" in raw_input:
                                            selection_id = "freq_month"

                                    if not selection_id:
                                        guarded_send(phone_number_id, from_number,
                                                     "Please select one of the options below. 👇")
                                        return


                                    freq_map = {"freq_hour": "hour", "freq_day": "day", "freq_month": "month"}
                                    selection = freq_map.get(selection_id, "total")

                                    price = session.get("temp_price", 0)
                                    symbol = session.get("currency", "₦")

                                    # 🟢 IMPROVEMENT: If it's 'total', don't add the slash
                                    if selection == "total":
                                        formatted_val = f"{symbol}{price:,.0f}"
                                    else:
                                        formatted_val = f"{symbol}{price:,.0f}/{selection}"

                                    session.update({
                                        "price": price,
                                        "formatted_price": formatted_val,  # 👈 This is what we need for 'compensation'
                                        "pay_period": selection,
                                        "flow": "awaiting_offer_phone"
                                    })
                                    commit_session(from_number, session)

                                    # Finally, ask for phone
                                    raw_phone_prompt = get_local_response(prompt_namespace, None, language=detected_lang,
                                                                          sub_key="ask_phone")
                                    prompt = random.choice(raw_phone_prompt) if isinstance(raw_phone_prompt,
                                                                                           list) else raw_phone_prompt

                                    guarded_send(phone_number_id, from_number, prompt, message_id)
                                    guarded_response_sent = True
                                    return


                                # 📸 5. IMAGE HANDLING -> MOVE TO PRICE (Quick Sale Only)
                                elif current_flow == "awaiting_offer_image":
                                    logger.info(f"🎯 FLOW REACHED: awaiting_offer_image | MsgType: {msg_type}")

                                    # 🔍 Attempt to extract image
                                    image_data = message.get("image")
                                    logger.info(f"🔎 EXTRACTION CHECK: image_data is {type(image_data)}")

                                    if image_data and isinstance(image_data, dict):
                                        image_id = image_data.get("id")
                                        logger.info(f"✅ SUCCESS: Found Image ID: {image_id}")

                                        if image_id:
                                            # 🛡️ Update session and move flow forward
                                            session.update({
                                                "user_id": from_number,
                                                "image_id": image_id,
                                                "is_verified": False,
                                                "verification_status": "pending",
                                                "created_at": datetime.now(timezone.utc).isoformat(), # 👈 Store as ISO string
                                                "flow": "awaiting_item_price"
                                            })
                                            commit_session(from_number, session)

                                            # ✅ FIX: Pass item_name instead of None to kill the "something" bug
                                            item_name = session.get("item_name", "item")
                                            raw_price_prompt = get_local_response(
                                                "offer_quick",
                                                item_name,
                                                language=detected_lang,
                                                sub_key="ask_price"
                                            )
                                            prompt = random.choice(raw_price_prompt) if isinstance(raw_price_prompt,
                                                                                                   list) else raw_price_prompt

                                            guarded_send(phone_number_id, from_number, f"✅ Photo received!\n\n{prompt}",
                                                         message_id)
                                            guarded_response_sent = True
                                            return
                                        else:
                                            logger.error(f"❌ Image received but ID missing for {from_number}")

                                    # ⚠️ FALLBACK: If they send text instead of an image
                                    if msg_type == "text":
                                        current_item = session.get("item_name", "item")
                                        error_msg = f"📸 I still need a **photo** of the {current_item}! Please use the 📎 attachment icon to send a picture."
                                    else:
                                        error_msg = "⚠️ I couldn't process that. Please send a standard photo to continue."

                                    guarded_send(phone_number_id, from_number, error_msg, message_id)
                                    guarded_response_sent = True
                                    return


                                # 💰 6. CATCH PRICE -> MOVE TO PHONE (Quick Sale Only)
                                elif current_flow == "awaiting_item_price":
                                    # Capture all three: value, symbol, and the pretty formatted version
                                    numeric_val, symbol, formatted_price = parse_nigerian_price(text, from_number)

                                    if numeric_val > 0:
                                        session.update({
                                            "price": numeric_val,
                                            "currency": symbol,
                                            "formatted_price": formatted_price,
                                            "raw_price_text": text,
                                            "flow": "awaiting_offer_phone"
                                        })
                                        commit_session(from_number, session)

                                        item_name = session.get("query_item") or session.get("item_name") or "item"

                                        raw_phone_prompt = get_local_response(
                                            "offer_quick",
                                            item=item_name,
                                            price=formatted_price,
                                            language=detected_lang,
                                            sub_key="ask_phone"
                                        )

                                        prompt = random.choice(raw_phone_prompt) if isinstance(raw_phone_prompt,
                                                                                               list) else raw_phone_prompt

                                        guarded_send(phone_number_id, from_number, prompt, message_id)
                                        # ✅ SUCCESS PATH: Set flag to True
                                        guarded_response_sent = True
                                        return
                                    else:
                                        # Fallback if they didn't type a valid number
                                        error_reply = "I didn't catch the price. Please type a number (e.g., 5000 or 15k)."
                                        guarded_send(phone_number_id, from_number, error_reply, message_id)
                                        # ✅ ERROR PATH: Also set flag to True to prevent further processing this turn
                                        guarded_response_sent = True
                                        return

                                # D. STEP 3: Phone Received -> Visibility
                                elif current_flow == "awaiting_offer_phone":
                                    prompt_namespace = session.get("prompt_namespace", "offer_pro")
                                    detected_lang = session.get("detected_lang", "English")

                                    # 🧼 PRE-CLEAN: Remove spaces, hyphens, and parentheses
                                    # This allows: "080-333-4444" or "0803 333 4444" to pass easily
                                    clean_phone = text.replace(" ", "").replace("-", "").replace("(", "").replace(")",
                                                                                                                  "").replace(
                                        ".", "")

                                    # 🕵️ CHECK: Use regex on the CLEANED number
                                    if re.search(r'^\+?\d{10,15}$', clean_phone):
                                        # ✅ SUCCESS: Save the cleaned version so "Click-to-Call" works later
                                        session.update({
                                            "biz_phone": clean_phone,
                                            "flow": "awaiting_offer_visibility"
                                        })
                                        commit_session(from_number, session)

                                        # Get visibility prompt
                                        raw_vis_prompt = get_local_response(prompt_namespace, None, language=detected_lang,
                                                                            sub_key="ask_visibility")
                                        vis_prompt = random.choice(raw_vis_prompt) if isinstance(raw_vis_prompt,
                                                                                                 list) else raw_vis_prompt

                                        guarded_send(phone_number_id, from_number, vis_prompt, message_id)
                                    else:
                                        # ❌ ERROR: Not a valid number
                                        raw_error = get_local_response(prompt_namespace, None, language=detected_lang,
                                                                       sub_key="phone_error")
                                        error_msg = random.choice(raw_error) if isinstance(raw_error, list) else (
                                                    raw_error or "⚠️ Please send a valid phone number (e.g., 08012345678).")

                                        guarded_send(phone_number_id, from_number, error_msg, message_id)

                                    guarded_response_sent = True
                                    return


                                # E. STEP 4: Visibility -> Location Request
                                elif current_flow == "awaiting_offer_visibility":
                                    prompt_namespace = session.get("prompt_namespace", "offer_pro")
                                    choice = text.strip().lower()

                                    # ✅ IMPROVED LOGIC: Catch numbers AND keywords
                                    if any(word in choice for word in ["2", "delivery", "deliver", "bring", "send"]):
                                        visibility = "delivery"
                                    elif any(
                                            word in choice for word in ["1", "shop", "store", "come", "visit", "physical"]):
                                        visibility = "physical"
                                    else:
                                        # If they type something weird, default to physical but log it
                                        visibility = "physical"

                                    session.update({"visibility": visibility, "flow": "awaiting_offer_location"})
                                    commit_session(from_number, session)

                                    # Pull the location prompt
                                    loc_prompt_raw = get_local_response(prompt_namespace, None,
                                                                        language=session.get("detected_lang"),
                                                                        sub_key="ask_location")
                                    loc_prompt = random.choice(loc_prompt_raw) if isinstance(loc_prompt_raw,
                                                                                             list) else loc_prompt_raw

                                    # 💡 ADD THE FALLBACK INSTRUCTION HERE
                                    instruction_suffix = "\n\n*Pro Tip:* If you can't use the pin, just type your area name (e.g., 'Wuse 2, Abuja') and send it! ✍️"
                                    full_loc_message = f"{loc_prompt}{instruction_suffix}"

                                    # Send the native button with the new combined text
                                    send_location_request(from_number, body_text=full_loc_message)
                                    guarded_response_sent = True
                                    return

                                # F. STEP 5: Location Received -> FINALIZE (Global Pure Integration)
                                elif current_flow == "awaiting_offer_location":

                                    logger.info(
                                        f"LOCATION STEP START | "
                                        f"text={text} | "
                                        f"msg_type={msg_type} | "
                                        f"session_lat={session.get('lat')} | "
                                        f"session_lng={session.get('lng')} | "
                                        f"user_lat={session.get('user_lat')} | "
                                        f"user_lng={session.get('user_lng')}"
                                    )
                                    # 1. Initialize scope variables
                                    visibility = session.get("visibility", "physical")
                                    lat, lng = None, None
                                    address_text = None
                                    resolved_city = session.get(
                                        "location")  # Default fallback to whatever is in the session
                                    prompt_namespace = session.get("prompt_namespace", "offer_pro")
                                    detected_lang = session.get("detected_lang", "English")

                                    # 2. Handle Location Pin vs. Manual Text vs. Accidental Media
                                    # Priority 1: Direct GPS Pin in current message
                                    if msg_type == "location":
                                        loc = message.get("location", {})
                                        lat, lng = loc.get("latitude"), loc.get("longitude")
                                        address_text = loc.get("address")
                                        logger.info(f"📍 GPS Pin Received: {lat}, {lng}")

                                    # Priority 2: GPS data already captured by Controller or Session
                                    elif (session.get("lat") and session.get("lng")) or (
                                            session.get("user_lat") and session.get("user_lng")):
                                        lat = session.get("lat") or session.get("user_lat")
                                        lng = session.get("lng") or session.get("user_lng")
                                        address_text = text if msg_type == "text" else None
                                        logger.info(f"🔄 Using GPS from Controller/Session: {lat}, {lng}")

                                    # Priority 3: Manual text address
                                    elif msg_type == "text" and len(text) > 2:
                                        address_text = text
                                        logger.info(f"✍️ Manual Address Received: {address_text}")

                                    else:
                                        # 🛡️ THE SAFETY GATE:
                                        reason = "photo" if msg_type == "image" else "invalid input"
                                        logger.warning(f"⚠️ Rejecting {reason} during location step.")

                                        guarded_send(
                                            phone_number_id,
                                            from_number,
                                            "📍 I still need your location to finish the listing! Please share a location pin or type your area name (e.g., 'Wuse 2').",
                                            message_id
                                        )
                                        guarded_response_sent = True
                                        return

                                    # 🔍 DEBUG INPUT
                                    logger.info(
                                        f"GEO INPUT | text={address_text} | "
                                        f"msg_type={msg_type} | "
                                        f"lat={lat} | lng={lng}"
                                    )

                                    # 🚀 CALL PURE GLOBAL ENGINE FOR ONBOARDING PROVIDERS
                                    geo_check = extract_global_location(
                                        text=address_text if msg_type == "text" else None,
                                        from_number=from_number,
                                        lat=lat,
                                        lng=lng
                                    )

                                    # 🔍 DEBUG OUTPUT
                                    logger.info(f"GEO OUTPUT | {geo_check}")

                                    if geo_check.get("found"):
                                        lat = geo_check.get("lat") or lat
                                        lng = geo_check.get("lng") or lng
                                        resolved_city = geo_check.get("name") or geo_check.get(
                                            "display_name") or resolved_city

                                        # If they sent a pin with no native textual metadata address, extract the address string from engine
                                        if not address_text:
                                            address_text = geo_check.get("display_name")

                                    # 🟢 BRIDGE MESSAGE: Send feedback & capture ID dynamically
                                    wait_msg = f"Got it! Populating your listing in *{resolved_city}* and securing your spot... ⏳"
                                    resp = guarded_send(phone_number_id, from_number, wait_msg, None)

                                    loading_id = None
                                    if isinstance(resp, dict) and "messages" in resp and len(resp["messages"]) > 0:
                                        loading_id = resp["messages"][0]["id"]
                                    else:
                                        logger.warning(f"⚠️ Could not capture loading_id. resp was: {resp}")

                                    # 3. SEARCH OPTIMIZATION VECTOR BUILDING
                                    search_vector_text = (
                                        f"{session.get('item_name', '')} "
                                        f"{session.get('offer_description', '')} "
                                        f"{session.get('biz_name', '')} "
                                        f"{session.get('item_info', '')} "
                                        f"{resolved_city}"
                                    ).strip().lower()

                                    # 4. Save to Session Natively (Cleanly owning state writes within Section 7)
                                    session.update({
                                        "manual_address": address_text,
                                        "search_index_text": search_vector_text,
                                        "lat": lat,
                                        "lng": lng,
                                        "user_lat": lat,
                                        "user_lng": lng,
                                        "location": geo_check.get("town") or resolved_city,
                                        "city": geo_check.get("city"),
                                        "state": geo_check.get("state"),
                                        "country": geo_check.get("country"),
                                        "loading_msg_id": loading_id
                                    })

                                    try:
                                        logger.info(f"💾 Attempting to finalize listing for {from_number}...")

                                        # Compile structural data for writing to Firestore collections
                                        listing_doc = prepare_listing_data(session, lat, lng)
                                        res_text = finalize_listing_to_db(from_number, listing_doc, session)

                                        # 🎯 AD INJECTION: Show a relevant ad after successful listing
                                        user_city = session.get("location") or session.get("city",
                                                                                           "EVERYWHERE")  # Suburb first, city fallback
                                        user_gender = session.get("gender", "All")
                                        user_cat = listing_doc.get("category", "ALL")

                                        ad_to_show = get_targeted_ad(
                                            from_number,
                                            user_city,
                                            user_cat,
                                            user_gender,
                                            user_state=session.get("state"),
                                            user_country=session.get("country")
                                        )

                                        # 5. --- SUCCESS UI/UX DISPLAY ---
                                        expiry_days = listing_doc.get("expiry_days", 7)
                                        success_raw = get_local_response(prompt_namespace, None, language=detected_lang,
                                                                         sub_key="success_msg")
                                        success_prefix = random.choice(success_raw) if isinstance(success_raw,
                                                                                                  list) else success_raw

                                        market_tip = get_market_insight(user_city=session.get("city") or session.get("location", "GLOBAL"))

                                        privacy_note = ""
                                        if visibility == "delivery":
                                            privacy_note = "\n\n🛡️ *Privacy Mode:* We'll only show your general area to customers."

                                        final_msg = (
                                            f"✅ *{success_prefix}*\n"
                                            f"{res_text}{privacy_note}\n\n"
                                            f"⏱️ *Note:* This listing will expire in {expiry_days} days.\n"
                                            f"{'-' * 15}\n"
                                            f"💡 {market_tip}"
                                        )

                                        # 🧹 COMPLETE ACCOUNT WORKSPACE WIPEOUT
                                        session.update({
                                            "flow": None,
                                            "loading_msg_id": None,
                                            "pending_search": None,
                                            "location_requested": False
                                        })
                                        commit_session(from_number, session)
                                        clear_session(from_number)  # Final hard wipe

                                        # 🟢 PROFESSIONAL MESSAGE TRANSFORMATION: Edit or send raw
                                        if loading_id:
                                            edit_message(phone_number_id, from_number, loading_id, final_msg)
                                        else:
                                            guarded_send(phone_number_id, from_number, final_msg, message_id)

                                        # 🎯 Deliver ad after success message
                                        if ad_to_show:
                                            time.sleep(1)
                                            lead_in = "💡 *While you're here, check this out:* "
                                            guarded_send(phone_number_id, from_number, lead_in, message_id)
                                            deliver_ad(phone_number_id, from_number, ad_to_show, message_id)
                                            logger.info(f"📢 Ad injected after listing for {from_number}")

                                        guarded_response_sent = True
                                        return

                                    except Exception as e:
                                        logger.error(f"❌ CRITICAL ERROR in finalize_listing: {str(e)}", exc_info=True)
                                        guarded_send(phone_number_id, from_number,
                                                     "⚠️ Oops! Something went wrong saving your listing. Please try again in a moment.",
                                                     message_id)
                                        guarded_response_sent = True
                                        return

                                # --- FINAL CATCH-ALLS ---

                                # Final catch-all save for the branch if no return was triggered yet
                                commit_session(from_number, session)

                                # 🧯 FINAL FALLBACK PROTECTION (prevents silent fall-through)
                                if not guarded_response_sent and is_onboarding(current_flow):
                                    logger.warning(f"⚠️ Section 7 fell through. Resetting flow.")

                                    # 🟢 FIX: Set to None so the next message isn't "muffled"
                                    session["flow"] = None
                                    commit_session(from_number, session)

                                    guarded_send(phone_number_id, from_number,
                                                 "I'm ready to help! 🔍 What are you looking for or wanting to list today?",
                                                 message_id)
                                    return



                            # --- 8. SEARCHER BRANCH (Pro V2.2 - Dispatcher Pattern) ---
                            elif session.get("route_intent") in ["search", "search_employment"] or \
                                    session.get("predicted_intent") in ["search", "search_employment"] or \
                                    session.get("flow") in ["awaiting_search_location", "searching"]:

                                # 🛡️ Safety defaults to prevent UnboundLocalError
                                results = []
                                targeting_meta = {"category": "all", "city": "EVERYWHERE"}

                                # 🟢 1. PAGINATION CONTEXT RECOVERY (The Fix)
                                # Check if the incoming 'text' is a pagination button ID
                                is_pagination = "more_results_" in (text or "")

                                if is_pagination:
                                    # Extract the offset number (e.g., '3' from 'more_results_3')
                                    try:
                                        current_offset = int(text.split("_")[-1])
                                    except (ValueError, IndexError):
                                        current_offset = session.get("search_offset", 0)

                                    # Restore the original query so Section 8 knows what we are actually searching for
                                    original_query = session.get("last_search_query") or session.get("query")
                                    if original_query:
                                        logger.info(
                                            f"🔄 Pagination Context: Restoring query '{original_query}' at offset {current_offset}")
                                        text = original_query  # Override "more_results_x" with the real search term

                                    session["search_offset"] = current_offset

                                else:
                                    # 🚨 Reset ONLY for real new typed searches
                                    is_new_typed_search = (
                                            msg_type == "text"
                                            and predicted_intent in ["search", "search_employment"]
                                            and current_flow not in ["awaiting_search_location"]
                                    )

                                    if is_new_typed_search:
                                        current_offset = 0
                                        session["search_offset"] = 0
                                    else:
                                        current_offset = session.get("search_offset", 0)

                                # 🟢 2. Initialize Query Persistence
                                # Now 'text' is either the new search or the restored original query
                                initial_query = text if text else session.get("last_search_query")

                                if initial_query:
                                    session["last_search_query"] = initial_query

                                mode = session.get("mode", "MARKET_SEARCH")

                                # 🔍 DISPATCHER: Use the Session Category directly!
                                # This ignores the 'mode' guessing and uses the exact category you assigned in Section 6.
                                # 🔍 DISPATCHER: Don't force 'service' as the default fallback
                                db_category = session.get("category")

                                logger.info(f"🔍 Dispatcher: Mode={mode} | Using DB_Category={db_category}")
                                current_time = time.time()
                                # 🛡️ THE ULTIMATE GUARD:
                                # Check current intent OR the session-cached intent from the first thread
                                saved_intent = session.get("predicted_intent")

                                if predicted_intent == "offer" or saved_intent == "offer":
                                    logger.info(
                                        f"🚫 SEARCHER REJECTED: Intent is OFFER. (Recovered: {saved_intent == 'offer'})")
                                    # Do absolutely nothing. Let Section 7 handle it.
                                    pass

                                else:
                                    # --- Continue with Search Logic ---
                                    current_flow = session.get("flow")

                                # ⏱️ Fix 5: Zombie session prevention (Timeout stuck flows after 10 mins / 600 secs)
                                # Provide a fallback using 'or' to ensure we always have a float
                                flow_start = session.get("flow_started_at") or current_time

                                if current_flow and (current_time - flow_start > 600):
                                    logger.info(f"⏱️ Flow timeout for {from_number}. Resetting zombie session.")
                                    current_flow = None
                                    session.update({"flow": None, "flow_started_at": None})
                                    commit_session(from_number, session)

                                # --- 🛡️ FIXED: Mandatory Profile Gate (Section 8) ---

                                # 1. GUARDIAN CHECK
                                if not session:
                                    logger.warning(
                                        f"⚠️ Guard Block Section 8: Session empty for {from_number}. Skipping gate.")
                                    return

                                # 2. PROFILE GATE
                                user_gender = session.get("user_gender") or session.get("gender")

                                # 💡 HEALER LOGIC
                                if session.get("gender") and not session.get("user_gender"):
                                    session["user_gender"] = session["gender"]
                                    commit_session(from_number, session)
                                    user_gender = session["gender"]

                                # 3. BLOCK IF PROFILE INCOMPLETE
                                if not user_gender:
                                    reminder = (
                                        "Wait! 🛑 Please select your gender "
                                        "so I can show you the best results."
                                    )

                                    send_interactive_button(
                                        phone_number_id,
                                        from_number,
                                        reminder,
                                        buttons_list=[
                                            {"id": "gender_male", "title": "Male 👨"},
                                            {"id": "gender_female", "title": "Female 👩"}
                                        ]
                                    )

                                    return

                                # 🛡️ Fix 1: Safe text extraction + Low Signal Filtering
                                safe_text = (text or "").strip()
                                cleaned_text = clean_user_text(safe_text)
                                if cleaned_text:
                                    cleaned_text = cleaned_text.replace("/",
                                                                        " ")  # Converts tailor/fashion to tailor fashion

                                # 👈 Defensive typing check and empty string protection
                                # 👇 FULL SANITATION FIRST
                                if not isinstance(cleaned_text, str) or not cleaned_text.strip():
                                    cleaned_text = None
                                else:
                                    cleaned_text = cleaned_text.strip()
                                    low_signal = ["ok", "yes", "yeah", "yep", "hi", "no", "sure", "pls", "help"]
                                    if len(cleaned_text) < 3 or cleaned_text.lower() in low_signal:
                                        cleaned_text = None

                                # 🧠 Detect TRUE new search (not flow-based)
                                prev_query = (session.get("pending_search") or "").strip().lower()
                                curr_query = (
                                    cleaned_text.strip().lower()
                                    if cleaned_text and current_flow not in ["awaiting_search_location"]
                                    else prev_query
                                )

                                is_new_search = (
                                        predicted_intent == "search" and
                                        curr_query and
                                        curr_query != prev_query
                                )

                                # 🧹 Reset stale state for every NEW search
                                if is_new_search:
                                    logger.info(f"🆕 New search detected: {cleaned_text}")
                                    session.update({
                                        "flow": None,
                                        "user_lat": None,
                                        "user_lng": None,
                                        "pending_search": None,
                                        "query": None,
                                        "query_item": None,
                                        "item_name": None,
                                        "location_requested": False,
                                        "flow_started_at": current_time,
                                        "mode": None  # 🧹 Clear stale mode
                                    })
                                    commit_session(from_number, session)
                                    current_flow = None


                                # 🔄 Fix 2: Define 'raw_query' OUTSIDE the 'is_new_search' block
                                # Now it runs for EVERY interaction, which is exactly what you want.
                                fallback_query = session.get("query") if current_flow in ["searching",
                                                                                          "awaiting_search_location"] else None

                                raw_query = (
                                                cleaned_text if current_flow not in ["awaiting_search_location"]
                                                else session.get("pending_search")
                                            ) or session.get("pending_search") or fallback_query


                                # 🧠 SMART QUERY CLEANER
                                if raw_query:

                                    # Remove conversational phrases first
                                    fluff_phrases = [
                                        r"i am looking for",
                                        r"i'm looking for",
                                        r"looking for",
                                        r"where can i get",
                                        r"where can i find",
                                        r"where can i buy",
                                        r"i need",
                                        r"i am in need of",
                                        r"in need of",
                                        r"can i get",
                                        r"help me find",
                                        r"find me",
                                        r"search for",
                                        r"show me",
                                        r"show me where i can get",
                                        r"show me where i can find",

                                        # 🆕 NEW NATURAL SEARCH PHRASES
                                        r"are there any",
                                        r"is there any",
                                        r"do you know any",
                                        r"can you find",
                                        r"can you help me find",
                                        r"anyone offering",
                                        r"any available"
                                    ]

                                    for phrase in fluff_phrases:
                                        raw_query = re.sub(
                                            phrase,
                                            "",
                                            raw_query,
                                            flags=re.IGNORECASE
                                        )


                                    # Remove leftover filler words
                                    raw_query = re.sub(
                                        r"\b(a|an|the|please|some|any|nearby|near me|closeby|close by|around me|around here|around|near|close|by|me|vacancy|vacancies|opening|openings)\b",
                                        "",
                                        raw_query,
                                        flags=re.IGNORECASE
                                    )

                                    # Final cleanup — remove punctuation and extra spaces
                                    raw_query = re.sub(r"[^\w\s]", "", raw_query)
                                    raw_query = re.sub(r"\s+", " ", raw_query).strip()

                                    # 🗺️ Remove location phrases from search query
                                    # "plumber in gwarinpa abuja" → "plumber"
                                    raw_query = re.sub(
                                        r"\b(in|at|near|around|close to|within|based in)\b.*$",
                                        "",
                                        raw_query,
                                        flags=re.IGNORECASE
                                    ).strip()
                                    logger.info(
                                        f"🔍 DEBUG: raw_query={raw_query} | lat={lat} | lng={lng} | mode={mode} | current_flow={current_flow}")
                                # 🚀 CLEAN FOR HUMAN DISPLAY
                                display_query = clean_for_display(raw_query)

                                display_query = re.sub(
                                    r"\b(to buy|for sale|near me|nearby|closeby|close by|around here|around me|close to me)\b",
                                    "",
                                    display_query,
                                    flags=re.IGNORECASE
                                )

                                display_query = re.sub(r"\s+", " ", display_query).strip()

                                # Final keyword extraction
                                display_query = extract_keyword(display_query)

                                # Now continue to the rest of the code...
                                clean_query = raw_query.strip() if raw_query else None
                                normalized_query = clean_query.lower() if clean_query else None

                                # 🚀 1.1 VAGUE QUERY INTERCEPTOR (Updated for Multi-lang)
                                vague_list = ["something", "someone", "person", "anything", "service"]
                                if not normalized_query or normalized_query in vague_list:
                                    lang = session.get("detected_lang", "English")
                                    # Pull from JSON instead of hardcoded string
                                    vague_reply = get_local_response("search", clean_query or "item",
                                                                     sub_key="vague_request", language=lang)

                                    # If the response is a list, pick one
                                    if isinstance(vague_reply, list):
                                        vague_reply = random.choice(vague_reply)

                                    guarded_send(phone_number_id, from_number, vague_reply, message_id)

                                    session.update({
                                        "flow": "awaiting_search_item",
                                        "pending_search": None,
                                        "query": None,
                                        "flow_started_at": current_time  # Reset the timeout clock
                                    })
                                    commit_session(from_number, session)
                                    return

                                # 📍 2. LOCATION RESOLUTION: THE SMART BRIDGE
                                lat = lat or session.get("user_lat")
                                lng = lng or session.get("user_lng")
                                text_location_input = cleaned_text if current_flow == "awaiting_search_location" else None

                                # 🚀 CALL PURE GLOBAL ENGINE
                                geo_check = extract_global_location(
                                    text=text_location_input,
                                    from_number=from_number,
                                    lat=lat,
                                    lng=lng
                                )

                                # 🛡️ FIX: Ensure geo_check is a dict before calling .get()
                                if isinstance(geo_check, list) and len(geo_check) > 0:
                                    geo_check = geo_check  # Assume the first item is the result

                                # Now proceed only if it's a valid dict
                                if isinstance(geo_check, dict) and geo_check.get("found"):
                                    lat = geo_check.get("lat") or lat
                                    lng = geo_check.get("lng") or lng
                                    loc_display_name = geo_check.get("name") or geo_check.get(
                                        "display_name") or "nearby"

                                    # 🟢 PROFESSIONAL BRIDGE: Send "Searching..." message immediately
                                    search_wait_msg = f"📍 _location received! searching for *{display_query}* in {loc_display_name}..._ 🔍"
                                    resp = guarded_send(phone_number_id, from_number, search_wait_msg, None)

                                    loading_id = None
                                    if isinstance(resp, dict) and "messages" in resp and len(resp["messages"]) > 0:
                                        loading_id = resp["messages"][0]["id"]  # 💡 Added here

                                    # ✅ Section 8 safely owns the state update directly (No hidden function side-effects)
                                    session.update({
                                        "user_lat": geo_check.get("lat") or lat,
                                        "user_lng": geo_check.get("lng") or lng,
                                        "location": geo_check.get("town"),
                                        "city": geo_check.get("city"),
                                        "state": geo_check.get("state"),
                                        "country": geo_check.get("country"),
                                        "flow_started_at": current_time,
                                        "loading_msg_id": loading_id
                                    })
                                    commit_session(from_number, session)
                                    logger.info(f"📍 Location synced cleanly in Section 8: {loc_display_name}")

                                logger.info(
                                    f"🔍 DEBUG: Checking location. lat={lat} | lng={lng} | session_lat={session.get('user_lat')} | session_lng={session.get('user_lng')}")
                                # 🛑 FINAL CHECK: If we STILL don't have coordinates, request them
                                if not (lat and lng):
                                    pretty_query = display_query or clean_query or "this"
                                    logger.info(
                                        f"📍 DEBUG: Entering location request block. pretty_query={pretty_query}")

                                    session.update({
                                        "flow": "awaiting_search_location",
                                        "pending_search": raw_query,  # Keep RAW for AI/FAISS
                                        "query": pretty_query,  # Keep CLEAN for UI
                                        "location_requested": True,
                                        "flow_started_at": current_time
                                    })
                                    commit_session(from_number, session)

                                    raw_loc_responses = get_local_response(
                                        "search",
                                        pretty_query,
                                        sub_key="location_request"
                                    )

                                    loc_template = (
                                        random.choice(raw_loc_responses)
                                        if isinstance(raw_loc_responses, list)
                                        else raw_loc_responses
                                    )

                                    # ✅ Use pretty query for human output
                                    loc_msg = loc_template.format(item=pretty_query)

                                    logger.info(f"📍 DEBUG: Sending location request. loc_msg={loc_msg[:100]}")
                                    result = send_location_request(from_number, loc_msg)
                                    logger.info(f"📍 DEBUG: Location request response: {result}")
                                    return

                                # 🚀 3. EXECUTION PHASE (Modified for Professional Edit Path)
                                user_lang = session.get("detected_lang", "English")

                                # Check if we have an active "Fetching" message from a button click
                                loading_id = session.get("loading_msg_id")

                                if not session.get("location_requested", False) and not loading_id:
                                    # ONLY send this if we AREN'T already showing a "Fetching..." bubble
                                    raw_start = get_local_response("search", display_query, language=user_lang,
                                                                   sub_key="searching_start")
                                    search_start_msg = random.choice(raw_start) if isinstance(raw_start,
                                                                                              list) else raw_start
                                    final_wait_msg = search_start_msg.format(item=display_query)

                                    guarded_send(phone_number_id, from_number, final_wait_msg, message_id)

                                # 🆕 STEP: Final Category Resolution
                                # Use the session category first, then try to resolve again, then fallback to 'product'
                                final_category = db_category or resolve_category(predicted_intent,
                                                                                 raw_query) or "product"

                                # 🔍 EXECUTE SEARCH (Forked Logic)
                                predicted_intent = session.get("predicted_intent", "search")
                                raw_query = raw_query or text or ""

                                # 1. Determine true category
                                intent_source_text = cleaned_text or raw_query or text or ""

                                current_category = resolve_category(
                                    predicted_intent,
                                    intent_source_text
                                )

                                # 🆕 STEP 2: MODE SYNC

                                # Save latest query
                                session["last_search_query"] = raw_query
                                commit_session(from_number, session)

                                # 🛡️ Mode sync — use the FIRST is_new_search definition from above
                                # Only preserve EMPLOYMENT_SEARCH for continuation flows (pagination, location followup)
                                is_continuation = current_flow in ["searching",
                                                                   "awaiting_search_location"] and not is_new_search

                                if mode == "EMPLOYMENT_SEARCH" and is_continuation:
                                    logger.info(
                                        "🛡️ Guard Active: Maintaining EMPLOYMENT_SEARCH mode for continuation flow.")
                                else:
                                    if current_category in ["product", "service", "vendor"]:
                                        logger.info(f"🔄 Auto-switching Mode to MARKET_SEARCH for {raw_query}")
                                        mode = "MARKET_SEARCH"
                                        session["mode"] = "MARKET_SEARCH"
                                        commit_session(from_number, session)
                                    elif current_category == "job":
                                        logger.info(f"🔄 Setting Mode to EMPLOYMENT_SEARCH for {raw_query}")
                                        mode = "EMPLOYMENT_SEARCH"
                                        session["mode"] = "EMPLOYMENT_SEARCH"
                                        commit_session(from_number, session)

                                # 🚀 Payload construction in Section 8:
                                smart_payload = {
                                    "item": display_query,
                                    "search_query": raw_query,
                                    "category_filter": current_category,
                                    "mode": mode,
                                    "offset": current_offset,
                                    "from_number": from_number,
                                    "city": session.get("city") or session.get("location"),
                                    # City first, suburb fallback
                                    "town": session.get("location"),  # Suburb/area for precise targeting
                                    "state": session.get("state"),
                                    "country": session.get("country"),
                                    "predicted_intent": predicted_intent
                                }
                                logger.info(
                                    f"🔍 DEBUG: Mode sync. mode={mode} | is_new_search={is_new_search} | is_continuation={is_continuation} | current_category={current_category}")




                                # 🆕 STEP 3: ROUTING
                                if mode == "EMPLOYMENT_SEARCH":
                                    logger.info(f"🕵️ Internal Job Search: {raw_query} (Offset: {current_offset})")

                                    # Add 'offset' to your function call
                                    job_response = search_offers_firestore(
                                        query=raw_query,
                                        user_lat=lat,
                                        user_lng=lng,
                                        top_k=3,
                                        offset=current_offset,
                                        entry_type="quick_job"
                                    )

                                    if isinstance(job_response, tuple):
                                        results, total_job_count = job_response
                                    else:
                                        results = job_response or []
                                        total_job_count = len(results)

                                    targeting_meta = {"category": "job", "source": "internal",
                                                      "total_count": total_job_count}
                                else:
                                    # 🛒 MARKET SEARCH (Hybrid Path)
                                    # Ensure smart_payload includes the offset as we discussed
                                    results, targeting_meta = perform_smart_search(smart_payload, lat, lng)

                                # 📊 PRESENT RESULTS (Modified for Professional Edit Path)
                                if results:
                                    # 🟢 PROFESSIONAL TOUCH: If there's a loading message, update it to "Done!"
                                    # or just proceed to send results and then delete/edit it.
                                    if loading_id:
                                        # Option A: Edit the "Fetching..." text to "Results ready! ✅"
                                        # before sending the actual interactive list.
                                        edit_message(phone_number_id, from_number, loading_id,
                                                     f"✅ *4ound* {len(results)} results for *{display_query}*:")

                                        # Clear it so it's not reused
                                        session["loading_msg_id"] = None
                                        commit_session(from_number, session)

                                    # Now present the list as usual
                                    present_foursquare_results(
                                        phone_number_id,
                                        from_number,
                                        results,
                                        f"{message_id}_{current_offset}",
                                        targeting_meta=targeting_meta,
                                        item_name=display_query
                                    )
                                    guarded_response_sent = True



                                else:

                                    # 🛑 NO RESULTS FOUND

                                    # 🧠 Human-friendly fallback resolution

                                    human_query = (

                                            display_query

                                            or clean_query

                                            or session.get("query")

                                            or session.get("pending_search")

                                            or session.get("last_search_query")

                                            or "that"

                                    )

                                    # 🧼 Prevent ugly internal placeholders

                                    bad_values = ["results", "result", "item", "search"]

                                    if isinstance(human_query, str) and human_query.lower() in bad_values:
                                        human_query = "that"

                                    no_results_msg = get_local_response(

                                        "search",

                                        human_query,

                                        language=user_lang,

                                        sub_key="no_results"

                                    )

                                    # 🟢 PROFESSIONAL EDIT PATH

                                    loading_id = session.get("loading_msg_id")

                                    if loading_id:
                                        edit_message(phone_number_id, from_number, loading_id, no_results_msg)
                                        session["loading_msg_id"] = None
                                        commit_session(from_number, session)
                                    else:
                                        guarded_send(phone_number_id, from_number, no_results_msg, message_id)

                                        # 💡 Help hint after no results
                                    help_hint = (
                                        "💡 *Try searching differently:*\n"
                                        "• 'I need a plumber'\n"
                                        "• 'Looking for shoes'\n"
                                        "• 'Chef jobs nearby'\n\n"
                                        "Type *help* for full command list."
                                    )
                                    guarded_send(phone_number_id, from_number, help_hint, message_id)

                                    guarded_response_sent = True

                                # 🚀  MARKET TIP (Always Show)
                                # We show this first to ensure the user gets value even if no ads exist
                                market_tip = get_market_insight(user_city=session.get("city") or session.get("location", "GLOBAL"))  # Ensure this function is imported/defined
                                if market_tip:
                                    tip_text = f"💡 *Market Tip:* {market_tip}"
                                    guarded_send(phone_number_id, from_number, tip_text, message_id)
                                    logger.info(f"💡 Market Tip sent to {from_number}")

                                # 🚀 3. PLAN B AD INJECTOR (Sync V1.5)
                                # Display an ad even when results are empty
                                user_city = session.get("location") or session.get("city",
                                                                                   "EVERYWHERE")  # Suburb first, city fallback
                                user_gender = session.get("gender", "All")
                                target_cat = targeting_meta.get("category", "all")

                                ad_to_show = get_targeted_ad(
                                    from_number,
                                    user_city,
                                    target_cat,
                                    user_gender,
                                    search_query=raw_query,
                                    user_state=session.get("state"),
                                    user_country=session.get("country")
                                )

                                if ad_to_show:
                                    time.sleep(1)
                                    lead_in = "💡 *While you're here, check this out:* "
                                    guarded_send(phone_number_id, from_number, lead_in, message_id)
                                    deliver_ad(phone_number_id, from_number, ad_to_show, message_id)
                                    logger.info(f"📢 Ad injected for {from_number} | query={raw_query} | city={user_city}")

                                # 🧹 4. SELECTIVE CLEANUP (Keep the state, only clear the transient flow)
                                session.update({
                                    "flow": None,
                                    "pending_search": None,
                                    "pending_cat": None,
                                    "location_requested": False,
                                    "loading_msg_id": None,
                                    "flow_started_at": None
                                })
                                commit_session(from_number, session)
                                logger.info(f"✅ Search execution complete for {clean_query}")
                                return

                            # --- 9. FINAL FALLBACK ---
                            if guarded_response_sent:
                                logger.info("✅ Section 9 bypassed: Response already sent.")
                            else:
                                # A. New User / Needs Consent
                                if not session.get("has_agreed_terms"):
                                    welcome_text = get_local_response("general", "greeting", language="English")
                                    send_interactive_button(
                                        phone_number_id,
                                        from_number,
                                        welcome_text,
                                        buttons_list=[{"id": "user_onboarding_agree", "title": "✅ I Agree & Start"}]
                                    )
                                    session.update({"flow": "awaiting_onboarding"})
                                    commit_session(from_number, session)
                                    # Note: We don't return here so we hit the 'finally' block for the typing indicator

                                # B. Confused Returning User
                                else:
                                    lang = session.get("detected_lang", "English")
                                    fallback_reply = get_local_response("general", "something", language=lang)
                                    guarded_send(phone_number_id, from_number, fallback_reply, message_id)

                                    session.update({"flow": None})
                                    commit_session(from_number, session)

                                # Final exit of the message processing
                                return


                        # 🟢 Aligns with 'try: # 👈 Start a Protected block'
                        except Exception:
                            logger.exception(f"Error in inner processing for {message_id}")
                            commit_session(from_number, session)

                    finally:
                         #⚪ Clears indicator regardless of 'return' or 'exception'
                        threading.Thread(target=send_typing_indicator, args=(from_number, message_id, "")).start()

                # 🔴 Aligns with your very first 'try' (deduplication check)
                except Exception:
                    logger.exception(f"Critical error in message loop for {message_id}")


# -------------------------
# Webhook endpoint (main logic)
# -------------------------
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    # 1. Verification for Meta/WhatsApp (KEEP AS IS)
    if request.method == "GET":
        if request.args.get("hub.verify_token") == VERIFY_TOKEN:
            return request.args.get("hub.challenge"), 200
        return "Verification failed", 403

    # 2. Receive and Handoff IMMEDIATELY
    data = request.get_json(silent=True)
    if not data:
        return "No JSON", 200 # Always 200 to stop retries

    # --- ✅ THE CRITICAL CHANGE ---
    # We don't loop here. We just pass the whole 'data' to the thread.
    try:
        import threading
        # We pass the entire 'data' dictionary so the thread handles the loops
        thread = threading.Thread(
            target=handle_whatsapp_logic,
            args=(data,)
        )
        thread.daemon = True
        thread.start()
    except Exception as e:
        logger.error(f"Failed to start worker thread: {e}")

    # 3. INSTANT RETURN
    # This happens in milliseconds. WhatsApp sees this and stops the "spinning" circle.
    return "OK", 200

# Add this route to main.py
@app.route('/cron/maintenance', methods=['GET'])
def trigger_maintenance():
    # Only run the remaining housekeeping tasks
    logger.info("⏰ UptimeRobot ping received: Starting maintenance tasks...")

    # process_pending_verifications handles stuck items
    process_pending_verifications()

    # nightly_cleanup handles expired documents
    nightly_cleanup()

    return "Maintenance complete", 200


@app.route('/')
def index():
    return "Service is live and operational.", 200




# -------------------------
# -------------------------
# Scheduler setup (APScheduler)
# -------------------------
scheduler = BackgroundScheduler()

# -------------------------
# App entrypoint
# -------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)


