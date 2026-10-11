# PayHub Multi-Tenant (Full)

Complete multi-tenant payment board using **personal Binance API** (no Merchant account).

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

### Security
- Fernet encryption for Binance secrets
- Per-owner unique `txId`
- Scoped data isolation
- Bot key + JWT auth

## Deploy

```bash
pip install -r requirements.txt
# Generate ENCRYPTION_KEY:
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
cp .env.example .env   # fill ENCRYPTION_KEY, JWT_SECRET, ADMIN_*
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

## Pages
- `/` home
- `/dashboard` user panel
- `/admin` admin panel
- `/docs` OpenAPI

## Bot integration
Header: `X-API-Key: ph_...`
1. `POST /api/v1/invoices`
2. Send deposit_address + amount to customer
3. `POST /api/v1/match` or per-invoice match
4. Optional: set `notify_url` to receive JSON when paid


## Mobile Money via SMS — bKash / Nagad / Rocket / Upay

None of these offer an API for personal accounts. One Android app (PayHubSmsForwarder v2)
forwards the "money received" SMS from ALL your SIMs to one webhook; PayHub works out which
service it came from, parses it and matches invoices.

### Setup
1. Set `PUBLIC_BASE_URL` in `.env` to your public https domain.
2. Dashboard -> **Mobile Money (SMS)** -> fill the number of each service you use -> Save.
3. Copy the **SMS Webhook URL** into the forwarder app on the phone(s) with those SIMs.
   Several phones can use the same URL.
4. **Verify the parser with REAL SMS** from each service: dashboard -> Test Parse
   (enter the sender, e.g. `bKash`, `NAGAD`, `16216`, and paste the SMS).
   Wording differs per service and may change. If it fails, edit the regexes in
   `app/mobile_money.py` (or send me the SMS with numbers masked).

### Usage
- Create invoice with `"currency": "BDT"` and optional `"method": "bkash|nagad|rocket|upay"`.
  - with `method`: `deposit_address` = that number, `network` = BKASH/NAGAD/..., only that service can pay it.
  - without: `network` = `MFS`, `deposit_address` lists all your numbers, any service can pay it.
- **Auto-match:** when an SMS arrives it is matched if exactly ONE pending BDT invoice fits
  (same amount, allowed service, created before the SMS). Use `unique_amount: true` to avoid collisions.
- **TrxID claim** (when amounts collide): `POST /api/v1/invoices/{reference}/claim`
  with `{"trx_id": "..."}`. Checks: SMS exists, TrxID unused, service allowed, amount equal,
  SMS arrived after invoice creation. (`/claim-bkash` still works.)
- Endpoints: `PUT/GET /api/v1/me/mfs`, `POST /api/v1/me/mfs/rotate-secret`,
  `GET /api/v1/me/mfs/sms`, `POST /api/v1/me/mfs/test-parse`,
  `POST /api/v1/sms/{secret}` (forwarder webhook; `/api/v1/bkash/sms/{secret}` still works).
- Upgrading from the earlier bKash-only version: the `provider` column is added automatically.
  Re-enter your number in the new dashboard card (old `payhub_bkash_config` table is unused).

### Adding another service (Tap, OK Wallet, ...)
Add an entry to `PROVIDERS` in `app/mobile_money.py` (key, label, sender names, keywords),
add its sender to "Allowed senders", and test with Test Parse.

### Limits & risks
- SMS is weaker proof than an API: a forged SMS reaching the phone could be accepted.
  URL secret, sender allow-list, unique TrxID and the amount/time checks reduce it, not eliminate it.
- Phone off / offline / app killed = verification waits until the SMS is delivered.
- Using personal accounts for business may violate each service's terms (limits/suspension).
