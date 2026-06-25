import os
import json
import base64
from google.oauth2 import service_account
from google.cloud import firestore

# 1. Get the environment variable
raw_creds = os.getenv('GOOGLE_APPLICATION_CREDENTIALS_JSON')

if not raw_creds:
    print("❌ ERROR: GOOGLE_APPLICATION_CREDENTIALS_JSON is not set!")
    exit(1)

# 2. Decode and Load
try:
    decoded_json = base64.b64decode(raw_creds)
    key_dict = json.loads(decoded_json)

    # 3. Create Credentials
    creds = service_account.Credentials.from_service_account_info(key_dict)

    # 4. Initialize Client
    # We use the explicit project ID to ensure no ambiguity
    db = firestore.Client(credentials=creds, project="the-markit-446be", database="(default)")

    print(f"✅ Success! Firestore client initialized for project: {creds.project_id}")

    # 5. Perform a real read operation
    # This is where the 403 usually triggers
    docs = db.collection("sessions").limit(1).get()

    print(f"✅ Connection verified. Documents found: {len(docs)}")
    for doc in docs:
        print(f"Document ID: {doc.id}")

except Exception as e:
    print(f"❌ FAILED: {e}")