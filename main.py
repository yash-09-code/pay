import os
import secrets
import sqlite3
from io import BytesIO
from urllib.parse import urlencode

import qrcode
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

load_dotenv()

DB_PATH = os.getenv("DATABASE_PATH", "payments.db")
EXPIRY_MINUTES = int(os.getenv("ORDER_EXPIRY_MINUTES", "5"))
UPI_ID = os.getenv("UPI_ID", "")
PAYEE_NAME = os.getenv("PAYEE_NAME", "Merchant")
APP_BASE_URL = os.getenv("APP_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")

app = FastAPI(title="Bank Email Payment Reconciliation", version="1.0.0")
templates = Jinja2Templates(directory="templates")


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.isoformat()


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                product_amount_paise INTEGER NOT NULL,
                payment_amount_paise INTEGER NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                candidate_utr TEXT,
                candidate_email_id TEXT,
                reviewed_at TEXT,
                review_note TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bank_alerts (
                gmail_message_id TEXT PRIMARY KEY,
                sender TEXT,
                subject TEXT,
                parsed_amount_paise INTEGER,
                utr TEXT,
                received_at TEXT NOT NULL,
                matched_order_id TEXT,
                raw_snippet TEXT
            )
        """)


init_db()


class CreateOrder(BaseModel):
    user_id: str = Field(min_length=1, max_length=128)
    amount: float = Field(gt=0, le=1000000)


class AdminDecision(BaseModel):
    decision: str = Field(pattern="^(success|failed)$")
    note: str = Field(default="", max_length=500)


def require_admin(key: str | None):
    if not ADMIN_API_KEY or not secrets.compare_digest(key or "", ADMIN_API_KEY):
        raise HTTPException(status_code=401, detail="Invalid admin API key")


def expire_orders():
    now = iso(utcnow())
    with db() as conn:
        conn.execute("""
            UPDATE orders
            SET status='expired'
            WHERE status IN ('processing', 'pending_review') AND expires_at < ?
        """, (now,))


@app.on_event("startup")
def startup():
    # Gmail polling is optional. The API still runs without OAuth configured.
    if os.path.exists(os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")):
        from gmail_monitor import start_monitor
        start_monitor()


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse("index.html", {
        "request": request,
        "upi_id": UPI_ID,
        "payee_name": PAYEE_NAME,
    })

def check_value(amount_paise, conn, now):
    while True:

        existing = conn.execute("""
            SELECT order_id
            FROM orders
            WHERE payment_amount_paise = ?
              AND status = ?
        """, (
            amount_paise,
            "processing"
        )).fetchone()

        if existing is None:
            return amount_paise

        amount_paise -= 1
        check_value(amount_paise,conn ,now)


        if amount_paise <= 0:
            raise HTTPException(
                status_code=400,
                detail="Cannot generate a unique payment amount"
            )

        

@app.post("/api/orders")
def create_order(payload: CreateOrder):

    if not UPI_ID:
        raise HTTPException(503, "UPI_ID is not configured")

    expire_orders()

    now = utcnow()
    expires = now + timedelta(minutes=EXPIRY_MINUTES)

    # Original product price in paise
    product_amount_paise = int(round(payload.amount * 100))

    order_id = (
        "ORD_" +
        secrets.token_urlsafe(9).replace("-", "").replace("_", "")[:12].upper()
    )

    with db() as conn:

        # Lock SQLite transaction while selecting a unique amount
        conn.execute("BEGIN IMMEDIATE")

        payment_amount_paise = check_value(
            product_amount_paise,
            conn,
            now
        )

        conn.execute("""
            INSERT INTO orders (
                order_id,
                user_id,
                product_amount_paise,
                payment_amount_paise,
                status,
                created_at,
                expires_at
            )
            VALUES (?, ?, ?, ?, 'processing', ?, ?)
        """, (
            order_id,
            payload.user_id,
            product_amount_paise,
            payment_amount_paise,
            iso(now),
            iso(expires)
        ))

    print("ORDER CREATED:", order_id)
    print("PRODUCT PRICE:", product_amount_paise / 100)
    print("PAYMENT AMOUNT:", payment_amount_paise / 100)

    return {
        "order_id": order_id,
        "user_id": payload.user_id,
        "product_amount": f"{product_amount_paise / 100:.2f}",
        "payment_amount": f"{payment_amount_paise / 100:.2f}",
        "currency": "INR",
        "status": "processing",
        "expires_at": iso(expires),
        "payment_url": f"{APP_BASE_URL}/pay/{order_id}"
    }


@app.get("/pay/{order_id}", response_class=HTMLResponse)
def payment_page(request: Request, order_id: str):
    expire_orders()
    with db() as conn:
        order = conn.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    amount = f"{order['payment_amount_paise'] / 100:.2f}"
    upi_link = (
        "upi://pay?pa=" + quote(UPI_ID)
        + "&pn=" + quote(PAYEE_NAME)
        + "&am=" + amount
        + "&cu=INR&tn=" + quote(order_id)
    )
    return templates.TemplateResponse("payment.html", {
        "request": request,
        "order": dict(order),
        "amount": amount,
        "upi_id": UPI_ID,
        "payee_name": PAYEE_NAME,
        "upi_link": upi_link,
    })


@app.get("/api/orders/{order_id}")
def get_order(order_id: str):
    expire_orders()
    with db() as conn:
        order = conn.execute("""
            SELECT order_id, user_id, product_amount_paise, payment_amount_paise,
                   status, created_at, expires_at, candidate_utr, reviewed_at, review_note
            FROM orders WHERE order_id=?
        """, (order_id,)).fetchone()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    result = dict(order)
    result["amount"] = f"{result.pop('payment_amount_paise') / 100:.2f}"
    result.pop("product_amount_paise")
    return result


@app.get("/api/admin/reviews")
def list_reviews(x_admin_api_key: str | None = Header(default=None)):
    require_admin(x_admin_api_key)
    with db() as conn:
        rows = conn.execute("""
            SELECT * FROM orders WHERE status='pending_review'
            ORDER BY created_at ASC
        """).fetchall()
    return {"reviews": [dict(r) for r in rows]}


@app.post("/api/admin/orders/{order_id}/decision")
def decide_order(
    order_id: str,
    payload: AdminDecision,
    x_admin_api_key: str | None = Header(default=None)
):
    require_admin(x_admin_api_key)
    with db() as conn:
        order = conn.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")
        if order["status"] != "pending_review":
            raise HTTPException(status_code=409, detail=f"Order status is {order['status']}")
        conn.execute("""
            UPDATE orders SET status=?, reviewed_at=?, review_note=?
            WHERE order_id=?
        """, (payload.decision, iso(utcnow()), payload.note, order_id))
    return {"order_id": order_id, "status": payload.decision}


@app.get("/api/orders/{order_id}/qr")
def payment_qr(order_id: str):
    """Generate a QR containing this order's exact amount and order reference."""
    with db() as conn:
        order = conn.execute(
            "SELECT order_id, payment_amount_paise, status FROM orders WHERE order_id=?",
            (order_id,)
        ).fetchone()

    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if not UPI_ID:
        raise HTTPException(status_code=503, detail="UPI_ID is not configured")

    amount = f"{order['payment_amount_paise'] / 100:.2f}"
    upi_uri = "upi://pay?" + urlencode({
        "pa": UPI_ID,
        "pn": PAYEE_NAME,
        "am": amount,
        "cu": "INR",
        "tn": order_id,
    })

    image = qrcode.make(upi_uri)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    buffer.seek(0)
    return StreamingResponse(buffer, media_type="image/png")


@app.get("/health")
def health():
    return {"ok": True, "service": "bank-email-payment-reconciliation"}


# This endpoint is intended for a trusted local/admin reconciliation tool.
# It does not allow the browser/customer to mark an order paid.
@app.post("/api/admin/orders/{order_id}/manual-review")
def manual_review(
    order_id: str,
    payload: AdminDecision,
    x_admin_api_key: str | None = Header(default=None)
):
    return decide_order(order_id, payload, x_admin_api_key)
