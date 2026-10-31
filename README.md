# FastAPI Bank Email Payment Reconciliation (Prototype)

This project creates bank-transfer orders, displays a UPI payment page, watches a Gmail inbox for bank notification emails, and flags possible matching transactions for **manual verification**.

## Critical payment-safety note

A bank notification email is not authoritative proof that money has arrived. Email can be delayed, spoofed, duplicated, or formatted unexpectedly. This project therefore **never marks an order successful from email alone**. A matching email moves an order to `pending_review`; an administrator must verify the credit in the actual bank account/statement before marking it `success`.

For production payments, use an RBI-authorized payment aggregator or a bank-provided verified transaction API/webhook. Do not use this prototype as a production payment gateway or hold customer funds on behalf of others without checking applicable regulatory requirements.

## Features

- FastAPI backend and SQLite database
- Order creation with exact amount (no hidden ₹0.01 price variation)
- Five-minute order expiry (configurable)
- Customer payment page with a dynamically generated UPI QR code and deep link
- QR amount is taken from the saved order amount (`am`) and includes the order ID as the transaction note
- Gmail read-only polling for a configured bank-alert sender
- Email amount/UTR extraction as a *candidate match*
- Admin-only review endpoint
- Customer page polls order status every 30 seconds

## 1. Requirements

- Python 3.10+
- A Gmail account receiving bank transaction alerts
- Google Cloud project with Gmail API enabled
- OAuth Desktop App credentials
- A UPI ID configured to receive transfers

## 2. Install

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt
```

Copy `.env.example` to `.env` and set at least:

- `UPI_ID`
- `PAYEE_NAME`
- `APP_BASE_URL`
- `BANK_ALERT_SENDER` (exact bank alert sender address)
- `ADMIN_API_KEY` (long random secret)

For local development, `APP_BASE_URL=http://127.0.0.1:8000` is fine. For external use, deploy behind HTTPS and set the public URL.

## 3. Configure Gmail API

1. Open https://console.cloud.google.com/
2. Create/select a Google Cloud project.
3. Enable Gmail API.
4. Configure OAuth consent screen.
5. Create OAuth Client ID, choose **Desktop app**, and download JSON.
6. Save the downloaded file as `credentials.json` in this project folder.
7. Set `BANK_ALERT_SENDER` in `.env` to the exact sender address used by your bank.

Authorize once from a machine with a browser:

```bash
python gmail_monitor.py --authorize
```

This creates `token.json`. Keep both credentials and token private; do not commit them to Git.

The app uses Gmail's `gmail.readonly` OAuth scope. Google may require verification for public use of restricted Gmail scopes. For a personal/local prototype, follow the OAuth testing configuration shown in Google Cloud Console.

## 4. Start server

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

Open http://127.0.0.1:8000

The Gmail monitor starts automatically if `credentials.json` exists. You can also run it separately:

```bash
python gmail_monitor.py
```

Do not run both the embedded monitor and separate monitor at the same time; duplicate polling is unnecessary (database deduplication prevents repeat processing of the same Gmail message).

## Dynamic UPI QR

The payment page automatically loads `/api/orders/{order_id}/qr`. The backend builds a UPI URI using the configured `UPI_ID`, the exact saved order amount in INR (`am`), currency (`cu=INR`), and order ID as the note (`tn`). It then returns a PNG QR image. You do not need to paste a separate QR-generation snippet anywhere.

Example UPI URI structure:

```text
upi://pay?pa=your-upi-id&pn=Your%20Business&am=100.00&cu=INR&tn=ORD_ORDERREFERENCE
```

The payment page also provides an "Open UPI app" link. Test the QR with your own UPI app and confirm the payee name and amount before using it.

## 5. API examples

### Create order

```bash
curl -X POST http://127.0.0.1:8000/api/orders \
  -H "Content-Type: application/json" \
  -d '{"user_id":"USER_123","amount":100}'
```

Response includes `order_id`, `status`, `amount`, `expires_at`, and `payment_url`.

### Check order status

```bash
curl http://127.0.0.1:8000/api/orders/ORD_YOUR_ORDER_ID
```

### List orders needing review

```bash
curl http://127.0.0.1:8000/api/admin/reviews \
  -H "X-Admin-API-Key: YOUR_ADMIN_API_KEY"
```

### Mark verified payment successful

Only after checking the actual bank account/statement:

```bash
curl -X POST http://127.0.0.1:8000/api/admin/orders/ORD_YOUR_ORDER_ID/decision \
  -H "Content-Type: application/json" \
  -H "X-Admin-API-Key: YOUR_ADMIN_API_KEY" \
  -d '{"decision":"success","note":"Verified credit in bank statement; UTR checked"}'
```

To mark failed, use `"decision":"failed"`.

## 6. Bank email parsing

Bank notification formats vary. The starter parser in `gmail_monitor.py` recognizes common amount and reference labels, but must be adapted to your bank's exact email template. It records possible matches only. If two active orders have the same amount, it deliberately does not guess which order was paid.

Do not rely on sender text alone as proof of authenticity. Review the actual bank ledger/statement and verify amount, UTR/reference, credit direction, and transaction uniqueness.

## 7. Production checklist

- Use HTTPS.
- Use PostgreSQL instead of SQLite for concurrent production workloads.
- Store OAuth tokens and admin secrets securely.
- Restrict admin endpoints behind authentication and network controls.
- Add audit logs and rate limiting.
- Add bank-specific parser tests.
- Use a bank-provided verified API/webhook or authorized payment aggregator for automatic confirmation.
- Handle refunds, partial payments, duplicate transfers, chargebacks/disputes, and reconciliation.
- Do not expose customer PII or bank email contents in public endpoints.
- Set correct order ownership checks before allowing a customer to view an order.

## Status meanings

- `processing`: order created; awaiting a possible bank alert.
- `pending_review`: a matching alert was found; human verification required.
- `success`: administrator verified the bank credit.
- `failed`: administrator rejected/marked payment failed.
- `expired`: five-minute order window elapsed before review.
