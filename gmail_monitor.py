"""
Optional Gmail alert monitor.

Important:
- Gmail alerts are treated only as MATCH CANDIDATES, never as proof of payment.
- A human/admin must verify the credit in the actual bank account/statement
  before changing an order to success.
- Configure the exact bank sender address in BANK_ALERT_SENDER.
- Bank email formats differ. Tune parse_amount_and_utr() for your bank's alerts.
"""
import base64
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

load_dotenv()
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
DB_PATH = os.getenv("DATABASE_PATH", "payments.db")
CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
BANK_SENDER = os.getenv("BANK_ALERT_SENDER", "").strip().lower()
POLL_SECONDS = max(10, int(os.getenv("GMAIL_POLL_SECONDS", "15")))

_thread_started = False


def get_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            # Run this once interactively on a machine with a browser:
            # python gmail_monitor.py --authorize
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w", encoding="utf-8") as f:
            f.write(creds.to_json())

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def decode_body(payload):
    chunks = []

    def walk(part):
        body = part.get("body", {})
        data = body.get("data")
        if data and part.get("mimeType", "").startswith("text/"):
            try:
                chunks.append(base64.urlsafe_b64decode(data + "==").decode("utf-8", "ignore"))
            except Exception:
                pass
        for child in part.get("parts", []):
            walk(child)

    walk(payload)
    return "\n".join(chunks)


def parse_amount_and_utr(text):
    """
    Generic starter parser only. Adapt this to the exact bank notification template.
    Examples it may recognize: 'INR 100.00', 'Rs. 100.00', 'Amount: 100.00'
    and common UTR/reference labels. It can produce false matches.
    """
    amount_patterns = [
        r"(?:INR|Rs\.?|₹)\s*([0-9,]+(?:\.[0-9]{1,2})?)",
        r"(?:amount|credited)\s*[:\-]?\s*(?:INR|Rs\.?|₹)?\s*([0-9,]+(?:\.[0-9]{1,2})?)",
    ]
    amount_paise = None
    for pattern in amount_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            try:
                amount_paise = int(round(float(match.group(1).replace(",", "")) * 100))
                break
            except ValueError:
                continue

    utr = None
    utr_match = re.search(
        r"(?:UTR|RRN|reference(?:\s*no)?|transaction\s*id)\s*[:#\-]?\s*([A-Z0-9]{8,30})",
        text,
        re.IGNORECASE
    )
    if utr_match:
        utr = utr_match.group(1).upper()

    return amount_paise, utr


def save_candidate(message_id, sender, subject, amount_paise, utr, snippet):
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    try:
        # Avoid reprocessing the same Gmail message.
        exists = conn.execute(
            "SELECT 1 FROM bank_alerts WHERE gmail_message_id=?", (message_id,)
        ).fetchone()
        if exists:
            return

        
        match = None

        if amount_paise is not None:
            now = datetime.now(timezone.utc).isoformat()

            rows = conn.execute("""
                SELECT order_id
                FROM orders
                WHERE status = 'processing'
                  AND payment_amount_paise = ?
                  AND expires_at >= ?
            """, (amount_paise, now)).fetchall()

            # Only match if exactly one active order exists
            if len(rows) == 1:
                candidate_order_id = rows[0]["order_id"]

                cursor = conn.execute("""
                    UPDATE orders
                    SET status = 'success',
                        candidate_utr = ?,
                        candidate_email_id = ?
                    WHERE order_id = ?
                      AND status = 'processing'
                """, (
                    utr,
                    message_id,
                    candidate_order_id
                ))

                if cursor.rowcount == 1:
                    match = candidate_order_id
                    print(
                        f"[PAYMENT MATCHED] {match} -> success"
                    )
                else:
                    print(
                        f"[UPDATE FAILED] Order {candidate_order_id} "
                        "was not updated."
                    )

            elif len(rows) > 1:
                print(
                    f"[AMBIGUOUS PAYMENT] Amount {amount_paise} "
                    "matches multiple orders."
                )

            else:
                print(
                    f"[NO MATCH] No active order for "
                    f"amount {amount_paise}"
                )

        conn.execute("""
            INSERT INTO bank_alerts (
                gmail_message_id, sender, subject,
                parsed_amount_paise, utr, received_at,
                matched_order_id, raw_snippet
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            message_id,
            sender,
            subject,
            amount_paise,
            utr,
            datetime.now(timezone.utc).isoformat(),
            match,
            snippet[:500]
        ))

        conn.commit()

    finally:
        conn.close()


def poll_once(service):
    if not BANK_SENDER:
        print("Gmail monitor disabled: set BANK_ALERT_SENDER in .env")
        return

    # Exact sender query. Gmail search is a filter, not sender authentication.
    result = service.users().messages().list(
        userId="me",
        q=f"from:{BANK_SENDER} newer_than:2d",
        maxResults=100
    ).execute()

    for item in result.get("messages", []):
        msg = service.users().messages().get(
            userId="me", id=item["id"], format="full"
        ).execute()
        payload = msg.get("payload", {})
        headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
        sender = headers.get("from", "")
        # Enforce the exact configured sender address as an additional basic check.
        if BANK_SENDER not in sender.lower():
            continue

        body = decode_body(payload)
        text = (msg.get("snippet", "") + "\n" + body)
        amount, utr = parse_amount_and_utr(text)
        save_candidate(
            item["id"], sender, headers.get("subject", ""),
            amount, utr, msg.get("snippet", "")
        )


def monitor_loop():
    try:
        service = get_service()
    except Exception as exc:
        print("Gmail monitor could not authenticate:", exc)
        print("Run: python gmail_monitor.py --authorize")
        return

    print("Gmail bank-alert monitor started.")
    while True:
        try:
            poll_once(service)
        except Exception as exc:
            print("Gmail polling error:", exc)
        time.sleep(POLL_SECONDS)


def start_monitor():
    global _thread_started
    if _thread_started:
        return
    _thread_started = True
    thread = threading.Thread(target=monitor_loop, daemon=True)
    thread.start()


if __name__ == "__main__":
    import sys
    if "--authorize" in sys.argv:
        service = get_service()
        print("Gmail OAuth authorized. User:", service.users().getProfile(userId="me").execute()["emailAddress"])
    else:
        monitor_loop()
