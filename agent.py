import os
import resend
from datetime import datetime, timedelta
from google.adk.agents import Agent
from google.adk.tools import FunctionTool

# Firestore 
try:
    from google.cloud import firestore
    db = firestore.Client(project="pghive-agent-491911")
    db.collection("tenants").limit(1).get()
    FIRESTORE_OK = True
    print("Firestore DB connected")
except Exception as e:
    FIRESTORE_OK = False
    db = None
    print(f"Firestore unavailable, using mock data: {e}")

# Google Calendar 
CALENDAR_ENABLED = False
CALENDAR_ID = "bharathyadav620@gmail.com"
SERVICE_ACCOUNT_FILE = os.path.join(os.path.dirname(__file__), "service_account.json")

try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE,
        scopes=[
            "https://www.googleapis.com/auth/calendar",
            "https://www.googleapis.com/auth/gmail.send"
        ]
    )
    calendar_service = build("calendar", "v3", credentials=creds)
    gmail_service = build("gmail", "v1", credentials=creds)
    CALENDAR_ENABLED = True
    print("Google APIs connected")
except Exception as e:
    calendar_service = None
    gmail_service = None
    print(f"Google APIs unavailable: {e}")

# Resend Email
resend.api_key = os.environ.get("RESEND_API_KEY")

def send_email(to_email: str, subject: str, body: str) -> dict:
    try:
        result = resend.Emails.send({
            "from": "PGHive <noreply@pghive.in>",
            "to": to_email,
            "subject": subject,
            "text": body
        })
        return {"success": True, "sent_to": to_email, "message_id": result.get("id")}
    except Exception as e:
        return {"success": False, "error": str(e)}


# TENANT AGENT TOOLS
def get_rent_status(tenant_id: str) -> dict:
    """
    Get rent payment status for a tenant.
    Use when tenant asks about rent, payment dues, or amount owed.
    Args:
        tenant_id: Tenant ID like T001, T002, T003
    """
    if FIRESTORE_OK and db:
        try:
            doc = db.collection("tenants").document(tenant_id).get()
            if doc.exists:
                data = doc.to_dict()
                return {
                    "found": True,
                    "name": data["name"],
                    "room": data["room"],
                    "rent": f"Rs.{data['rent']}",
                    "status": data["payment_status"],
                    "due_date": data.get("due_date", "5th of every month"),
                    "pay_link": "https://pghive.in/pay"
                }
        except Exception as e:
            print(f"Firestore error: {e}")

    # Fallback mock data
    mock = {
        "T001": {"name": "Ravi Kumar",   "room": "101", "rent": 8500, "status": "PENDING"},
        "T002": {"name": "Priya Sharma", "room": "102", "rent": 9000, "status": "PAID"},
        "T003": {"name": "Arun Mehta",   "room": "201", "rent": 7500, "status": "PENDING"},
    }
    if tenant_id in mock:
        t = mock[tenant_id]
        return {
            "found": True,
            "name": t["name"], "room": t["room"],
            "rent": f"Rs.{t['rent']}", "status": t["status"],
            "due_date": "5th of every month",
            "pay_link": "https://pghive.in/pay"
        }
    return {"found": False, "message": f"Tenant '{tenant_id}' not found."}


def create_maintenance_ticket(tenant_id: str, issue: str, urgency: str = "medium") -> dict:
    """
    Create a maintenance ticket when a tenant reports a problem.
    Use when tenant reports any issue — leaking tap, broken fan, no electricity, etc.
    Set urgency to 'emergency' for water, electricity, or safety issues.
    Args:
        tenant_id: The tenant's ID
        issue: Description of the problem
        urgency: emergency / high / medium / low
    """
    ticket_id = f"MNT-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    eta = {
        "emergency": "2-4 hours",
        "high": "24 hours",
        "medium": "2-3 days",
        "low": "5-7 days"
    }.get(urgency.lower(), "3-5 days")

    room_number = "Unknown"
    if FIRESTORE_OK and db:
        try:
            doc = db.collection("tenants").document(tenant_id).get()
            if doc.exists:
                room_number = doc.to_dict().get("room", "Unknown")
        except:
            pass

    ticket_data = {
        "ticket_id": ticket_id,
        "tenant_id": tenant_id,
        "issue": issue,
        "urgency": urgency,
        "eta": eta,
        "status": "OPEN",
        "room": room_number,
        "created_at": datetime.now().isoformat()
    }

    if FIRESTORE_OK and db:
        try:
            db.collection("maintenance_tickets").document(ticket_id).set(ticket_data)
        except Exception as e:
            print(f"Firestore write failed: {e}")

    return {
        "success": True,
        "ticket_id": ticket_id,
        "room_number": room_number,
        "issue": issue,
        "urgency": urgency,
        "eta": eta,
        "message": f"Ticket {ticket_id} created. Scheduling visit and sending confirmation..."
    }


def schedule_maintenance_visit(
    ticket_id: str,
    issue: str,
    room_number: str,
    urgency: str = "medium"
) -> dict:
    """
    Schedule a maintenance visit in Google Calendar after a ticket is created.
    Always call this after create_maintenance_ticket.
    Args:
        ticket_id: The maintenance ticket ID
        issue: Description of the issue
        room_number: Room number where the issue is
        urgency: emergency / high / medium / low
    """
    days_ahead = {"emergency": 0, "high": 1, "medium": 2, "low": 3}.get(urgency.lower(), 2)
    visit_start = (datetime.now() + timedelta(days=days_ahead)).replace(
        hour=10, minute=0, second=0, microsecond=0
    )
    visit_end = visit_start + timedelta(hours=1)

    if CALENDAR_ENABLED and calendar_service:
        try:
            event = {
                "summary": f"🔧 Maintenance: Room {room_number} - {issue}",
                "description": f"Ticket: {ticket_id}\nIssue: {issue}\nUrgency: {urgency}",
                "start": {"dateTime": visit_start.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "Asia/Kolkata"},
                "end":   {"dateTime": visit_end.strftime("%Y-%m-%dT%H:%M:%S"),   "timeZone": "Asia/Kolkata"},
                "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 30}]}
            }
            created = calendar_service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
            return {
                "success": True,
                "mode": "google_calendar",
                "visit_scheduled": visit_start.strftime("%A, %B %d at %I:%M %p"),
                "calendar_link": created.get("htmlLink"),
                "message": f"Visit scheduled for {visit_start.strftime('%A, %B %d at 10:00 AM IST')}"
            }
        except Exception as e:
            print(f"Calendar API error: {e}")

    return {
        "success": True,
        "mode": "simulated",
        "visit_scheduled": visit_start.strftime("%A, %B %d at %I:%M %p"),
        "message": f"Visit scheduled for {visit_start.strftime('%A, %B %d at 10:00 AM IST')}"
    }


def send_maintenance_confirmation(tenant_id: str, ticket_id: str, visit_time: str) -> dict:
    """
    Send email confirmation to tenant after maintenance visit is scheduled.
    Always call this after schedule_maintenance_visit.
    Args:
        tenant_id: The tenant's ID
        ticket_id: The maintenance ticket ID
        visit_time: Scheduled visit time from schedule_maintenance_visit
    """
    tenant_name, tenant_email = "Tenant", "tenant@example.com"

    if FIRESTORE_OK and db:
        try:
            doc = db.collection("tenants").document(tenant_id).get()
            if doc.exists:
                data = doc.to_dict()
                tenant_name = data.get("name", "Tenant")
                tenant_email = data.get("email", "tenant@example.com")
        except:
            pass
    else:
        mock = {
            "T001": ("Ravi Kumar",   "bellambharathkumar211@gmail.com"),
            "T002": ("Priya Sharma", "bellambharathkumar211@gmail.com"),
            "T003": ("Arun Mehta",   "bellambharathkumar211@gmail.com"),
        }
        if tenant_id in mock:
            tenant_name, tenant_email = mock[tenant_id]

    body = f"""Dear {tenant_name},

        Your maintenance request has been received and a visit has been scheduled.

        Ticket ID   : {ticket_id}
        Scheduled   : {visit_time}

        Our team will arrive at your room during this time.
        Please make sure someone is available to provide access.

        Need to reschedule? Contact us at support@pghive.in

        Thank you,
        PGHive Management
        """
    result = send_email(tenant_email, f"[PGHive] Maintenance Visit Scheduled — {ticket_id}", body)
    return {**result, "tenant": tenant_name, "visit_time": visit_time}


def get_pg_rules(topic: str) -> dict:
    """
    Get PG house rules on a specific topic.
    Use when tenant asks about curfew, guests, food, wifi, parking, deposit, or notice period.
    Args:
        topic: Topic like curfew / guests / food / wifi / parking
    """
    rules = {
        "curfew":  "Gate closes at 10:30 PM. Let the caretaker know if you're coming late.",
        "guest":   "Guests allowed 9 AM–9 PM only. Overnight stays need prior approval.",
        "food":    "Breakfast 7:30–9 AM | Lunch 12:30–2 PM | Dinner 8–9:30 PM.",
        "wifi":    "WiFi available 24/7, 100 Mbps shared connection.",
        "parking": "Two-wheeler parking is free. Four-wheeler on availability basis.",
        "deposit": "Security deposit is 2 months rent. Refunded after room inspection.",
        "notice":  "30-day written notice required before vacating.",
        "smoking": "No smoking or alcohol on premises.",
        "noise":   "Quiet hours: 10:30 PM to 7 AM.",
        "laundry": "Washing machine on terrace, available 6 AM–9 PM.",
    }
    matched = {k: v for k, v in rules.items() if k in topic.lower()}
    return {"rules": matched if matched else rules, "contact": "support@pghive.in"}


# OWNER AGENT TOOLS

def get_pending_dues() -> dict:
    """
    Get all tenants with pending rent payments.
    Use when owner asks who hasn't paid, total outstanding, or pending dues.
    No arguments needed.
    """
    if FIRESTORE_OK and db:
        try:
            pending = []
            for doc in db.collection("tenants").where("payment_status", "==", "PENDING").stream():
                data = doc.to_dict()
                pending.append({
                    "tenant_id": doc.id,
                    "name": data["name"],
                    "room": data["room"],
                    "amount": data["rent"],
                    "email": data.get("email", "")
                })
            total = sum(t["amount"] for t in pending)
            return {"pending_count": len(pending), "total_outstanding": f"Rs.{total}", "tenants": pending}
        except Exception as e:
            print(f"Firestore error: {e}")

    # Fallback
    pending = [
        {"tenant_id": "T001", "name": "Ravi Kumar",  "room": "101", "amount": 8500, "email": "ravi.kumar@email.com"},
        {"tenant_id": "T003", "name": "Arun Mehta",  "room": "201", "amount": 7500, "email": "arun.mehta@email.com"},
    ]
    return {"pending_count": 2, "total_outstanding": "Rs.16000", "tenants": pending}


def get_revenue_report() -> dict:
    """
    Get monthly rent collection and revenue report.
    Use when owner asks about revenue, collection rate, or monthly summary.
    No arguments needed.
    """
    if FIRESTORE_OK and db:
        try:
            total_expected = total_collected = 0
            for doc in db.collection("tenants").stream():
                data = doc.to_dict()
                rent = data.get("rent", 0)
                total_expected += rent
                if data.get("payment_status") == "PAID":
                    total_collected += rent
            if total_expected > 0:
                rate = round((total_collected / total_expected) * 100, 1)
                return {
                    "month": datetime.now().strftime("%B %Y"),
                    "total_expected": f"Rs.{total_expected}",
                    "collected": f"Rs.{total_collected}",
                    "pending": f"Rs.{total_expected - total_collected}",
                    "collection_rate": f"{rate}%"
                }
        except Exception as e:
            print(f"Firestore error: {e}")

    return {
        "month": datetime.now().strftime("%B %Y"),
        "total_expected": "Rs.25000",
        "collected": "Rs.9000",
        "pending": "Rs.16000",
        "collection_rate": "36.0%"
    }


def send_rent_reminder(tenant_name: str, tenant_email: str, rent_amount: int, due_date: str = "5th of this month") -> dict:
    """
    Send a rent reminder email to a tenant.
    Use when owner wants to remind tenants about pending rent payments.
    Args:
        tenant_name: Full name of the tenant
        tenant_email: Tenant's email address
        rent_amount: Amount due in INR
        due_date: Payment due date
    """
    body = f"""Dear {tenant_name},

This is a friendly reminder that your rent of Rs.{rent_amount} is due by {due_date}.

Payment options:
  UPI  : pghive@upi
  Link : https://pghive.in/pay

If you've already paid, please ignore this message.

Thank you,
PGHive Management
support@pghive.in
"""
    return send_email(tenant_email, f"[PGHive] Rent Reminder — Rs.{rent_amount} due by {due_date}", body)


# ROOM SEARCH AGENT TOOLS

def search_available_rooms(room_type: str = "any", max_budget: int = 15000) -> dict:
    """
    Search for available PG rooms by type and budget.
    Use when someone asks about available rooms, vacancies, or rooms to rent.
    Args:
        room_type: single / double / triple / any
        max_budget: Maximum monthly rent in INR
    """
    if FIRESTORE_OK and db:
        try:
            matching = []
            for doc in db.collection("rooms").where("available", "==", True).stream():
                data = doc.to_dict()
                if data.get("rent", 0) <= max_budget:
                    if room_type == "any" or data.get("type", "").lower() == room_type.lower():
                        matching.append({
                            "room_number": doc.id,
                            "type": data.get("type"),
                            "rent": data.get("rent"),
                            "amenities": data.get("amenities", [])
                        })
            return {"found": bool(matching), "count": len(matching), "rooms": matching}
        except Exception as e:
            print(f"Firestore error: {e}")

    return {"found": True, "count": 2, "rooms": [
        {"room_number": "301", "type": "single", "rent": 9000, "amenities": ["WiFi", "AC", "Attached Bath", "Balcony"]},
        {"room_number": "202", "type": "double", "rent": 6000, "amenities": ["WiFi", "Common Bath", "Meals"]},
    ]}


def calculate_rent_split(total_rent: int, num_people: int, include_utilities: bool = True) -> dict:
    """
    Calculate how much each person pays when splitting rent.
    Use when someone asks about splitting rent among roommates.
    Args:
        total_rent: Total monthly rent in INR
        num_people: Number of people sharing
        include_utilities: Include estimated utility costs (default True)
    """
    per_person = round(total_rent / num_people, 2)
    utilities = 500 if include_utilities else 0
    return {
        "rent_per_person": f"Rs.{per_person}",
        "utilities_per_person": f"Rs.{utilities}",
        "total_per_person": f"Rs.{per_person + utilities}",
        "note": "Utility estimate covers electricity and water. Actual may vary."
    }


# SUB-AGENTS

tenant_agent = Agent(
    name="tenant_agent",
    model="gemini-2.0-flash",
    description="""
    Handles all TENANT requests:
    - Rent payment status and dues
    - Maintenance complaints — creates ticket, schedules visit, sends confirmation email
    - House rules and policies
    """,
    instruction="""
    You are the PGHive Tenant Assistant. Help tenants with rent, maintenance, and house rules.

    For maintenance requests, always follow this 3-step workflow:
    1. create_maintenance_ticket() — get ticket_id and room_number
    2. schedule_maintenance_visit() — with ticket_id, issue, room_number, urgency
    3. send_maintenance_confirmation() — with tenant_id, ticket_id, visit_time

    Urgency guide: no water/electricity/safety = emergency | broken lock = high | leaking tap/fan = medium | cosmetic = low

    Always ask for tenant ID if not provided. Be friendly and reassuring.
    """,
    tools=[
        FunctionTool(get_rent_status),
        FunctionTool(create_maintenance_ticket),
        FunctionTool(schedule_maintenance_visit),
        FunctionTool(send_maintenance_confirmation),
        FunctionTool(get_pg_rules),
    ]
)

owner_agent = Agent(
    name="owner_agent",
    model="gemini-2.0-flash",
    description="""
    Handles all OWNER and ADMIN requests:
    - Pending dues and unpaid rent list
    - Monthly revenue and collection reports
    - Sending rent reminder emails to tenants
    """,
    instruction="""
    You are the PGHive Owner Dashboard Assistant.

    For rent reminders:
    1. get_pending_dues() — get list of tenants who haven't paid
    2. send_rent_reminder() — call for each pending tenant

    Always show collected AND pending amounts with the collection rate percentage.
    """,
    tools=[
        FunctionTool(get_pending_dues),
        FunctionTool(get_revenue_report),
        FunctionTool(send_rent_reminder),
    ]
)

room_search_agent = Agent(
    name="room_search_agent",
    model="gemini-2.0-flash",
    description="""
    Handles all ROOM-related requests:
    - Searching available rooms by type and budget
    - Calculating rent split among roommates
    """,
    instruction="""
    You are the PGHive Room Search Assistant.
    Help people find available rooms and understand pricing.
    Ask for room type and budget if not mentioned.
    """,
    tools=[
        FunctionTool(search_available_rooms),
        FunctionTool(calculate_rent_split),
    ]
)


# ROOT AGENT (COORDINATOR)

root_agent = Agent(
    name="pghive_coordinator",
    model="gemini-2.0-flash",
    description="PGHive Multi-Agent Coordinator — routes requests to the right specialist agent.",
    instruction="""
    You are the PGHive AI Coordinator for a PG accommodation in Hyderabad, India.

    Route every request to the right specialist:
    → Tenant questions (rent / maintenance / rules) → tenant_agent
    → Owner questions (dues / revenue / reminders)  → owner_agent
    → Room questions  (search / availability / split) → room_search_agent

    If the intent is unclear, ask: "Are you a tenant, the owner, or looking for a room?"
    Never answer questions yourself. Always transfer to a specialist.
    """,
    tools=[],
    sub_agents=[tenant_agent, owner_agent, room_search_agent]
)


agent = root_agent
