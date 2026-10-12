# PayHub Multi-Tenant (Full)

Complete multi-tenant payment board using **personal Binance API** (no Merchant account) + **bKash / Nagad / Rocket / Upay** via SMS forwarding.

**Version:** 4.3.0-mt

## Features

### User
- Register / Login (JWT)
- Profile + default notify URL
- Change password
- Rotate bot API key
- Encrypted personal Binance API keys
- Deposit address
- Unique amount suggestion
- Create invoice (optional unique amount + per-invoice notify URL)
- List / filter invoices
- Match all or single invoice
- Auto-expire stale + manual expire
- Stats (pending / paid / volume)
- Webhook callback when paid (`notify_url`)

### Admin
- Platform stats
- User list + search
- Toggle user active
- View user invoices
- Force match for any user
- Web admin panel

### Mobile Money (SMS)
- bKash / Nagad / Rocket / Upay
- SMS forwarder webhook
- Auto-match (exact one pending invoice)
- TrxID claim when amounts collide
- Test Parse tool

### Security
- Fernet encryption for Binance secrets
- Per-owner unique `txId` / TrxID
- Scoped data isolation
- Bot key + JWT auth

## Project structure

```
payhub/
├── app/
│   ├── __init__.py
│   ├── main.py
│   ├── models.py
│   ├── schemas.py
│   ├── auth.py
│   ├── binance.py
│   ├── mobile_money.py
│   ├── config.py
│   ├── database.py
│   ├── crypto_util.py
│   └── templates/
│       ├── index.html
│       ├── dashboard.html
│       └── admin.html
├── requirements.txt
├── .env.example
├── .gitignore
├── railway.json
├── app-debug.apk          # PayHub SMS Forwarder (debug)
└── README.md
```

## Deploy

```bash
cd payhub
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Generate ENCRYPTION_KEY:
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

cp .env.example .env
# Edit .env: ENCRYPTION_KEY, JWT_SECRET, ADMIN_EMAIL, ADMIN_PASSWORD, PUBLIC_BASE_URL

uvicorn app.main:app --host 0.0.0.0 --port 8000
# or: uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

## Pages
- `/` home
- `/dashboard` user panel (login, invoices, Binance, MFS, profile)
- `/admin` admin panel
- `/docs` OpenAPI
- `/health` health check

## Bot integration
Header: `X-API-Key: ph_...`

1. `POST /api/v1/invoices`
2. Send `deposit_address` + `amount` to customer
3. `POST /api/v1/match` or per-invoice match
4. Optional: set `notify_url` → receives JSON when paid:

```json
{
  "event": "invoice.paid",
  "reference": "PHxxxxxxxx",
  "customer_id": "order-123",
  "amount": "500.00",
  "currency": "BDT",
  "provider_tx_id": "TRX...",
  "paid_at": "2026-10-12T..."
}
```

## Mobile Money via SMS — bKash / Nagad / Rocket / Upay

None of these offer an API for personal accounts. One Android app (PayHub SMS Forwarder)
forwards the "money received" SMS from ALL your SIMs to one webhook; PayHub parses and matches.

### Setup
1. Set `PUBLIC_BASE_URL` in `.env` to your public https domain.
2. Dashboard → **Mobile Money (SMS)** → fill numbers → Save.
3. Copy the **SMS Webhook URL** into the forwarder app on the phone(s).
4. Verify parser: Dashboard → Test Parse (real SMS).

### Usage
- Create invoice with `"currency": "BDT"` and optional `"method": "bkash|nagad|rocket|upay"`.
- Auto-match when exactly ONE pending invoice fits.
- TrxID claim: `POST /api/v1/invoices/{reference}/claim` with `{"trx_id": "..."}`.

### Limits & risks
- SMS is weaker proof than an API.
- Phone offline = verification waits until SMS is delivered.
- Using personal accounts for business may violate service terms.

## Railway
`railway.json` is included. Set env vars in the Railway dashboard and deploy.
