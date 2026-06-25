from google.cloud import firestore
from google.oauth2 import service_account

# 1. Update this path to where your JSON key file is stored on your PC
KEY_PATH = "C:/Users/HP/PycharmProjects/4ound/4oundKey.json"

try:
    print("Loading service account credentials...")
    credentials = service_account.Credentials.from_service_account_file(KEY_PATH)

    print("Initializing Firestore Client...")
    db = firestore.Client(credentials=credentials, project="the-markit-446be")

    print("Attempting to read from 'sessions' collection...")
    # Replicates the query that failed in your Render logs
    docs = list(db.collection("sessions").limit(1).get())

    print("✅ Success! Connection verified. Documents found:", len(docs))
    for doc in docs:
        print(f"Document ID: {doc.id} => {doc.to_dict()}")

except Exception as e:
    print(f"❌ Connection FAILED: {e}")
