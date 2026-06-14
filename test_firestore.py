from google.cloud import firestore

# Firestore client automatically uses the JSON key
db = firestore.Client()

# Quick test: list collections
print("Firestore connected!")
print("Collections:", list(db.collections()))