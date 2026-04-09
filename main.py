import os
import requests
import uvicorn
import resend
import google.auth
import google.auth.transport.requests
from datetime import datetime, timedelta
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

# Firestore setup
try:
    from google.cloud import firestore
    db = firestore.Client(project="pghive-agent-491911")
    db.collection("tenants").limit(1).get()
    FIRESTORE_OK = True
    print("Firestore connected")
except Exception as e:
    FIRESTORE_OK = False
    db = None
    print(f"Firestore not available: {e}")

# Google Calendar setup using service account
CALENDAR_ID = "bharathyadav620@gmail.com"
SERVICE_ACCOUNT_FILE = os.path.join(os.path.dirname(__file__), "service_account.json")
calendar_service = None

try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    sa_creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE,
        scopes=["https://www.googleapis.com/auth/calendar"]
    )
    calendar_service = build("calendar", "v3", credentials=sa_creds)
    print("Google Calendar connected")
except Exception as e:
    print(f"Calendar not available: {e}")

# Resend for emails via pghive.in domain
resend.api_key = os.environ.get("RESEND_API_KEY", "")


def call_gemini(payload: dict) -> requests.Response:
    """Call Gemini via Vertex AI using GCP project credentials."""
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds.refresh(google.auth.transport.requests.Request())

    url = (
        "https://asia-south1-aiplatform.googleapis.com/v1/"
        "projects/pghive-agent-491911/locations/asia-south1/"
        "publishers/google/models/gemini-2.5-flash:generateContent"
    )
    headers = {
        "Authorization": f"Bearer {creds.token}",
        "Content-Type": "application/json"
    }
    return requests.post(url, json=payload, headers=headers, timeout=30)


def build_system_prompt() -> str:
    """Build the system prompt with live data from Firestore."""
    tenants_info = ""
    rooms_info = ""

    if FIRESTORE_OK and db:
        try:
            for doc in db.collection("tenants").stream():
                d = doc.to_dict()
                tenants_info += (
                    f"{doc.id}={d.get('name')},Room {d.get('room')},"
                    f"Rs.{d.get('rent')},{d.get('payment_status')},"
                    f"email:{d.get('email', '')}. "
                )
        except Exception:
            tenants_info = "T001=Ravi Kumar,Room 101,Rs.8500,PENDING. T002=Priya Sharma,Room 102,Rs.9000,PAID. T003=Arun Mehta,Room 201,Rs.7500,PENDING."

        try:
            for doc in db.collection("rooms").stream():
                d = doc.to_dict()
                status = "AVAILABLE" if d.get("available") else "OCCUPIED"
                rooms_info += f"Room {doc.id}:{d.get('type')},Rs.{d.get('rent')},{status},{d.get('amenities', [])}. "
        except Exception:
            rooms_info = "Room 202:double,Rs.6000,AVAILABLE. Room 301:single,Rs.9000,AVAILABLE. Room 302:triple,Rs.4500,AVAILABLE."
    else:
        tenants_info = "T001=Ravi Kumar,Room 101,Rs.8500,PENDING. T002=Priya Sharma,Room 102,Rs.9000,PAID. T003=Arun Mehta,Room 201,Rs.7500,PENDING."
        rooms_info = "Room 202:double,Rs.6000,AVAILABLE. Room 301:single,Rs.9000,AVAILABLE. Room 302:triple,Rs.4500,AVAILABLE."

    return f"""You are PGHive AI Assistant — a smart manager for PG (Paying Guest) accommodations in India.

TENANT DATA: {tenants_info}
ROOM DATA: {rooms_info}
RULES: Gate closes 10:30 PM. Guests allowed 9 AM–9 PM. 30-day notice to vacate. Deposit = 2 months rent. Meals: Breakfast 7:30-9 AM, Lunch 12:30-2 PM, Dinner 8-9:30 PM.

YOU CAN HANDLE:
1. RENT STATUS — When tenant asks about rent, give their exact status, amount, and due date. Mention pghive.in/pay for payment.
2. MAINTENANCE — When tenant reports an issue, create a ticket ID like MNT-{datetime.now().strftime('%Y%m%d')}-XXX, set priority (emergency/high/medium/low), give ETA.
   - No water/electricity/fire → emergency (2-4 hours)
   - Broken lock → high (24 hours)
   - Leaking tap/fan → medium (2-3 days)
   - Cosmetic → low (5-7 days)
   After creating ticket, mention that a visit has been scheduled and email confirmation sent.
3. ROOM SEARCH — Show available rooms with amenities, type, price.
4. PENDING DUES — For owner: list all PENDING tenants with total outstanding.
5. REVENUE — For owner: show collected vs expected, collection rate.
6. HOUSE RULES — Answer questions about curfew, guests, food, WiFi, parking, deposit, notice.
7. RENT SPLIT — Calculate per person cost when sharing a room.

ALWAYS: Be short, friendly, and specific. Never say "contact your PG owner" — YOU are the assistant.
If someone reports maintenance, always give a ticket ID and mention that calendar visit is scheduled and email sent."""


def handle_maintenance_side_effects(tenant_id: str, issue: str, urgency: str, ticket_id: str):
    """After a maintenance ticket is created — save to Firestore, schedule Calendar event, send email."""
    days_map = {"emergency": 0, "high": 1, "medium": 2, "low": 3}
    days_ahead = days_map.get(urgency.lower(), 2)

    visit_start = (datetime.now() + timedelta(days=days_ahead)).replace(
        hour=10, minute=0, second=0, microsecond=0
    )
    visit_end = visit_start + timedelta(hours=1)

    # Save ticket to Firestore
    if FIRESTORE_OK and db:
        try:
            db.collection("maintenance_tickets").document(ticket_id).set({
                "ticket_id": ticket_id,
                "tenant_id": tenant_id,
                "issue": issue,
                "urgency": urgency,
                "status": "OPEN",
                "created_at": datetime.now().isoformat()
            })
        except Exception as e:
            print(f"Firestore write failed: {e}")

    # Create Calendar event
    if calendar_service:
        try:
            calendar_service.events().insert(
                calendarId=CALENDAR_ID,
                body={
                    "summary": f"🔧 Maintenance: {issue}",
                    "description": f"Ticket: {ticket_id}\nTenant: {tenant_id}",
                    "start": {"dateTime": visit_start.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "Asia/Kolkata"},
                    "end":   {"dateTime": visit_end.strftime("%Y-%m-%dT%H:%M:%S"),   "timeZone": "Asia/Kolkata"},
                }
            ).execute()
        except Exception as e:
            print(f"Calendar event failed: {e}")

    # Get tenant details and send confirmation email
    tenant_name = tenant_id
    tenant_email = ""

    if FIRESTORE_OK and db:
        try:
            doc = db.collection("tenants").document(tenant_id).get()
            if doc.exists:
                data = doc.to_dict()
                tenant_name = data.get("name", tenant_id)
                tenant_email = data.get("email", "")
        except Exception:
            pass

    if tenant_email:
        try:
            resend.Emails.send({
                "from": "PGHive <noreply@pghive.in>",
                "to": tenant_email,
                "subject": f"[PGHive] Maintenance Visit Scheduled — {ticket_id}",
                "text": f"""Dear {tenant_name},

Your maintenance request has been received.

Ticket ID : {ticket_id}
Issue     : {issue}
Scheduled : {visit_start.strftime('%A, %B %d at 10:00 AM IST')}

Our team will arrive at your room during this time.
Please make sure someone is available to provide access.

Thank you,
PGHive Management
support@pghive.in"""
            })
        except Exception as e:
            print(f"Email failed: {e}")


# FastAPI app setup
app = FastAPI(title="PGHive AI Assistant")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

#chat UI
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def root():
    index = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index):
        return FileResponse(index)
    return {
        "agent": "PGHive AI Assistant",
        "model": "gemini-2.5-flash",
        "version": "2.0.0",
        "status": "running"
    }


@app.get("/health")
def health():
    return {"status": "healthy"}


class ChatRequest(BaseModel):
    message: str
    session_id: str = "default_session"


@app.post("/chat")
def chat(req: ChatRequest):
    try:
        system_prompt = build_system_prompt()

        payload = {
            "system_instruction": {
                "parts": [{"text": system_prompt}]
            },
            "contents": [{
                "role": "user",
                "parts": [{"text": req.message}]
            }]
        }

        resp = call_gemini(payload)
        data = resp.json()

        if "candidates" not in data:
            error_msg = data.get("error", {}).get("message", str(data))
            print(f"Gemini error: {error_msg}")
            return {"response": f"API Error: {error_msg}", "session_id": req.session_id}

        reply = data["candidates"][0]["content"]["parts"][0]["text"]

        # Check if this is a maintenance request and trigger side effects
        msg_lower = req.message.lower()
        maintenance_keywords = [
            "leaking", "broken", "no water", "no electricity", "not working",
            "repair", "maintenance", "fan", "tap", "toilet", "light", "door"
        ]

        if any(kw in msg_lower for kw in maintenance_keywords):
            tenant_id = "UNKNOWN"
            for tid in ["T001", "T002", "T003"]:
                if tid.lower() in req.message.lower():
                    tenant_id = tid
                    break

            urgency = "medium"
            if any(w in msg_lower for w in ["no water", "no electricity", "fire", "flood", "gas"]):
                urgency = "emergency"
            elif any(w in msg_lower for w in ["lock", "security", "crack"]):
                urgency = "high"
            elif any(w in msg_lower for w in ["paint", "cosmetic", "minor"]):
                urgency = "low"

            ticket_id = f"MNT-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
            handle_maintenance_side_effects(tenant_id, req.message, urgency, ticket_id)

        return {
            "response": reply,
            "session_id": req.session_id,
            "agent": "pghive_coordinator"
        }

    except Exception as e:
        return {"response": f"Error: {str(e)}", "session_id": req.session_id}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)