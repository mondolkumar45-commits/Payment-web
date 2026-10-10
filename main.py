import logging
import secrets
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_HALF_UP

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Query
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func, and_, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import (
    create_access_token,
    get_current_user,
    hash_password,
    new_bot_api_key,
    require_admin,
    verify_password,
)
from app.binance import get_deposit_address, get_deposit_history
from app.mobile_money import (
    DEFAULT_SENDERS,
    PROVIDERS,
    aware,
    dec_eq,
    normalize_msisdn,
    parse_sms,
    pick_invoice,
    provider_ok,
    sender_allowed,
)
from app.config import settings
from app.crypto_util import decrypt_secret, encrypt_secret
from app.database import Base, SessionLocal, engine, get_db
from app.models import BinanceConfig, Invoice, MatchedTx, MfsAccount, MfsConfig, SmsTx, User
from app.schemas import (
    AdminStats,
    AdminUserOut,
    BinanceConfigIn,
    BinanceConfigOut,
    ChangePasswordIn,
    ClaimIn,
    DepositAddressOut,
    InvoiceCreate,
    InvoiceOut,
    LoginIn,
    MatchResult,
    MfsAccountOut,
    MfsConfigIn,
    MfsConfigOut,
    ProfileUpdateIn,
    RegisterIn,
    SmsTestIn,
    SmsTxOut,
    SuggestAmountOut,
    TokenOut,
    UserOut,
    UserStats,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("payhub")

app = FastAPI(
    title="PayHub Multi-Tenant",
    description="Multi-tenant payment matcher: personal Binance deposits + bKash/Nagad/Rocket/Upay personal accounts via SMS forwarding.",
    version="4.3.0-mt",
)
templates = Jinja2Templates(directory="app/templates")


def _utcnow():
    return datetime.now(timezone.utc)


def _ensure_admin():
    db = SessionLocal()
    try:
        if db.scalar(select(User).where(User.is_admin == True)):  # noqa: E712
            return
        db.add(
            User(
                email=settings.admin_email.lower().strip(),
                password_hash=hash_password(settings.admin_password),
                display_name="Admin",
                is_admin=True,
                is_active=True,
                bot_api_key=new_bot_api_key(),
            )
        )
        db.commit()
        logger.info("Bootstrap admin: %s", settings.admin_email)
    finally:
        db.close()


Base.metadata.create_all(bind=engine)


def _ensure_columns() -> None:
    """Tiny migration: add columns introduced after the first release."""
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    if insp.has_table("payhub_sms_tx"):
        cols = {c["name"] for c in insp.get_columns("payhub_sms_tx")}
        if "provider" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE payhub_sms_tx ADD COLUMN provider VARCHAR(16) NOT NULL DEFAULT 'bkash'"))


_ensure_columns()
_ensure_admin()


def _user_out(user: User, db: Session) -> UserOut:
    cfg = db.scalar(select(BinanceConfig).where(BinanceConfig.user_id == user.id))
    mfs = db.scalar(select(MfsConfig).where(MfsConfig.user_id == user.id))
    return UserOut(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        is_admin=user.is_admin,
        is_active=user.is_active,
        bot_api_key=user.bot_api_key,
        default_notify_url=user.default_notify_url,
        created_at=user.created_at,
        binance_configured=bool(cfg and cfg.is_configured),
        mfs_configured=bool(mfs and mfs.is_configured),
    )


def _get_creds(user: User, db: Session) -> tuple[str, str, str, str]:
    cfg = db.scalar(select(BinanceConfig).where(BinanceConfig.user_id == user.id))
    if not cfg or not cfg.is_configured:
        raise HTTPException(400, "Binance API not configured")
    return (
        decrypt_secret(cfg.api_key_enc),
        decrypt_secret(cfg.api_secret_enc),
        cfg.deposit_coin,
        cfg.deposit_network,
    )


def _dec_eq(a, b) -> bool:
    try:
        return Decimal(str(a)) == Decimal(str(b))
    except Exception:
        return False


async def _send_notify(invoice: Invoice, owner: User):
    url = invoice.notify_url or owner.default_notify_url
    if not url or invoice.notify_sent:
        return
    payload = {
        "event": "invoice.paid",
        "reference": invoice.reference,
        "customer_id": invoice.customer_id,
        "amount": str(invoice.amount),
        "currency": invoice.currency,
        "provider_tx_id": invoice.provider_tx_id,
        "paid_at": invoice.paid_at.isoformat() if invoice.paid_at else None,
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(url, json=payload)
            if r.status_code < 300:
                invoice.notify_sent = True
                logger.info("Notify OK %s -> %s", invoice.reference, url)
            else:
                logger.warning("Notify HTTP %s for %s", r.status_code, invoice.reference)
    except Exception as exc:
        logger.warning("Notify failed %s: %s", invoice.reference, exc)


# ═══════════════ Pages ═══════════════

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    return templates.TemplateResponse("admin.html", {"request": request})


@app.get("/health")
def health(db: Session = Depends(get_db)):
    try:
        db.scalar(select(func.count()).select_from(User))
        db_ok = True
    except Exception:
        db_ok = False
    return {"status": "ok" if db_ok else "degraded", "db": db_ok, "mode": "multi-tenant"}


# ═══════════════ Auth ═══════════════

@app.post("/api/v1/auth/register", response_model=TokenOut)
def register(payload: RegisterIn, db: Session = Depends(get_db)):
    if db.scalar(select(User).where(User.email == payload.email)):
        raise HTTPException(400, "Email already registered")
    user = User(
        email=payload.email,
        password_hash=hash_password(payload.password),
        display_name=payload.display_name or payload.email.split("@")[0],
        bot_api_key=new_bot_api_key(),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return TokenOut(
        access_token=create_access_token(user.id, False),
        bot_api_key=user.bot_api_key,
    )


@app.post("/api/v1/auth/login", response_model=TokenOut)
def login(payload: LoginIn, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == payload.email))
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(401, "Invalid email or password")
    if not user.is_active:
        raise HTTPException(403, "Account disabled")
    return TokenOut(
        access_token=create_access_token(user.id, user.is_admin),
        bot_api_key=user.bot_api_key,
        is_admin=user.is_admin,
    )


@app.get("/api/v1/me", response_model=UserOut)
def me(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return _user_out(user, db)


@app.patch("/api/v1/me", response_model=UserOut)
def update_profile(
    payload: ProfileUpdateIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if payload.display_name is not None:
        user.display_name = payload.display_name
    if payload.default_notify_url is not None:
        user.default_notify_url = payload.default_notify_url.strip() or None
    db.commit()
    db.refresh(user)
    return _user_out(user, db)


@app.post("/api/v1/me/change-password")
def change_password(
    payload: ChangePasswordIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(400, "Current password is wrong")
    user.password_hash = hash_password(payload.new_password)
    db.commit()
    return {"ok": True}


@app.post("/api/v1/me/rotate-bot-key", response_model=UserOut)
def rotate_bot_key(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    user.bot_api_key = new_bot_api_key()
    db.commit()
    db.refresh(user)
    return _user_out(user, db)


@app.get("/api/v1/me/stats", response_model=UserStats)
def my_stats(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    base = select(Invoice).where(Invoice.owner_id == user.id)
    pending = db.scalar(select(func.count()).select_from(Invoice).where(Invoice.owner_id == user.id, Invoice.status == "PENDING")) or 0
    paid = db.scalar(select(func.count()).select_from(Invoice).where(Invoice.owner_id == user.id, Invoice.status == "PAID")) or 0
    expired = db.scalar(select(func.count()).select_from(Invoice).where(Invoice.owner_id == user.id, Invoice.status == "EXPIRED")) or 0
    total = db.scalar(select(func.count()).select_from(Invoice).where(Invoice.owner_id == user.id)) or 0
    vol = db.scalar(
        select(func.coalesce(func.sum(Invoice.amount), 0)).where(
            Invoice.owner_id == user.id, Invoice.status == "PAID"
        )
    )
    return UserStats(
        pending=pending,
        paid=paid,
        expired=expired,
        total=total,
        paid_volume=Decimal(str(vol or 0)),
    )


# ═══════════════ Binance ═══════════════

@app.put("/api/v1/me/binance", response_model=BinanceConfigOut)
def set_binance(payload: BinanceConfigIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    cfg = db.scalar(select(BinanceConfig).where(BinanceConfig.user_id == user.id))
    if not cfg:
        cfg = BinanceConfig(user_id=user.id)
        db.add(cfg)
    cfg.api_key_enc = encrypt_secret(payload.api_key.strip())
    cfg.api_secret_enc = encrypt_secret(payload.api_secret.strip())
    cfg.deposit_coin = payload.deposit_coin.upper().strip()
    cfg.deposit_network = payload.deposit_network.upper().strip()
    cfg.is_configured = True
    cfg.updated_at = _utcnow()
    db.commit()
    k = payload.api_key
    masked = (k[:4] + "…" + k[-4:]) if len(k) > 8 else "****"
    return BinanceConfigOut(
        is_configured=True,
        deposit_coin=cfg.deposit_coin,
        deposit_network=cfg.deposit_network,
        api_key_masked=masked,
    )


@app.get("/api/v1/me/binance", response_model=BinanceConfigOut)
def get_binance(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    cfg = db.scalar(select(BinanceConfig).where(BinanceConfig.user_id == user.id))
    if not cfg:
        return BinanceConfigOut(is_configured=False, deposit_coin="USDT", deposit_network="TRX")
    return BinanceConfigOut(
        is_configured=cfg.is_configured,
        deposit_coin=cfg.deposit_coin,
        deposit_network=cfg.deposit_network,
        api_key_masked="****" if cfg.is_configured else None,
    )


@app.get("/api/v1/me/deposit-address", response_model=DepositAddressOut)
async def my_deposit_address(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    api_key, api_secret, coin, network = _get_creds(user, db)
    try:
        data = await get_deposit_address(api_key, api_secret, coin, network)
    except Exception as exc:
        raise HTTPException(502, f"Binance error: {exc}") from exc
    return DepositAddressOut(
        coin=str(data.get("coin") or coin).upper(),
        network=str(data.get("network") or network).upper(),
        address=str(data.get("address") or ""),
        tag=data.get("tag") or data.get("addressTag"),
    )


@app.get("/api/v1/me/suggest-amount", response_model=SuggestAmountOut)
def suggest_amount(
    base: Decimal = Query(..., gt=0),
    currency: str = Query(default="USDT"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Suggest a unique-ish amount (base + 0.01..0.99) not used by pending invoices."""
    currency = currency.upper()
    pending_amounts = set(
        Decimal(str(a))
        for a in db.scalars(
            select(Invoice.amount).where(
                Invoice.owner_id == user.id,
                Invoice.status == "PENDING",
                Invoice.currency == currency,
            )
        ).all()
    )
    base = base.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    for i in range(0, 100):
        candidate = (base + Decimal(i) / 100).quantize(Decimal("0.01"))
        if candidate not in pending_amounts:
            return SuggestAmountOut(
                amount=candidate,
                currency=currency,
                reason=f"base {base} + {i/100:.2f}" if i else "base amount free",
            )
    raise HTTPException(409, "Could not find free amount variant")


# ═══════════════ Invoices ═══════════════

@app.post("/api/v1/invoices", response_model=InvoiceOut)
async def create_invoice(
    payload: InvoiceCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    currency = payload.currency.strip().upper()
    amount = payload.amount
    is_mfs = currency == "BDT"
    if is_mfs:
        _cfg, mfs_accounts = _require_mfs(user, db)
        amount = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        api_key = api_secret = ""
        method = (payload.method or "").strip().lower()
        if method:
            acct = next((a for a in mfs_accounts if a.provider == method), None)
            if not acct:
                raise HTTPException(400, f"Method '{method}' not configured. Configured: "
                                         + ", ".join(a.provider for a in mfs_accounts))
            mfs_address = acct.number
            network = method.upper()
        else:
            mfs_address = "; ".join(f"{PROVIDERS[a.provider].label}: {a.number}" for a in mfs_accounts)
            network = "MFS"
    else:
        api_key, api_secret, coin, network = _get_creds(user, db)

    if payload.unique_amount:
        pending_amounts = {
            Decimal(str(a))
            for a in db.scalars(
                select(Invoice.amount).where(
                    Invoice.owner_id == user.id,
                    Invoice.status == "PENDING",
                    Invoice.currency == currency,
                )
            ).all()
        }
        base = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        found = None
        for i in range(0, 100):
            cand = (base + Decimal(i) / 100).quantize(Decimal("0.01"))
            if cand not in pending_amounts:
                found = cand
                break
        if found is None:
            raise HTTPException(409, "No free unique amount")
        amount = found

    reference = "PH" + secrets.token_hex(8).upper()
    expire_at = _utcnow() + timedelta(minutes=settings.invoice_expire_minutes)
    deposit_address = None
    if is_mfs:
        deposit_address = mfs_address
    else:
        try:
            addr = await get_deposit_address(api_key, api_secret, currency, network)
            deposit_address = addr.get("address")
            network = addr.get("network") or network
        except Exception as exc:
            logger.warning("addr fetch user=%s: %s", user.id, exc)

    inv = Invoice(
        owner_id=user.id,
        reference=reference,
        customer_id=payload.customer_id,
        amount=amount,
        currency=currency,
        status="PENDING",
        deposit_address=deposit_address,
        network=network,
        note=payload.note,
        notify_url=payload.notify_url,
        expire_at=expire_at,
    )
    db.add(inv)
    db.commit()
    db.refresh(inv)
    return inv


@app.get("/api/v1/invoices/{reference}", response_model=InvoiceOut)
def get_invoice(reference: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    inv = db.scalar(
        select(Invoice).where(Invoice.reference == reference, Invoice.owner_id == user.id)
    )
    if not inv:
        raise HTTPException(404, "Invoice not found")
    return inv


@app.get("/api/v1/invoices", response_model=list[InvoiceOut])
def list_invoices(
    customer_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    q = (
        select(Invoice)
        .where(Invoice.owner_id == user.id)
        .order_by(Invoice.id.desc())
        .offset(max(offset, 0))
        .limit(min(limit, 200))
    )
    if customer_id:
        q = q.where(Invoice.customer_id == customer_id)
    if status:
        q = q.where(Invoice.status == status.upper())
    return list(db.scalars(q).all())


@app.post("/api/v1/invoices/{reference}/expire", response_model=InvoiceOut)
def expire_invoice(reference: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    inv = db.scalar(
        select(Invoice).where(Invoice.reference == reference, Invoice.owner_id == user.id).with_for_update()
    )
    if not inv:
        raise HTTPException(404, "Invoice not found")
    if inv.status == "PAID":
        raise HTTPException(400, "Already paid")
    inv.status = "EXPIRED"
    db.commit()
    db.refresh(inv)
    return inv


async def _run_binance_match(user: User, db: Session, only_reference: str | None = None) -> MatchResult:
    now = _utcnow()

    # Auto-expire
    expired_n = 0
    expired_rows = list(
        db.scalars(
            select(Invoice).where(
                Invoice.owner_id == user.id,
                Invoice.status == "PENDING",
                Invoice.expire_at.is_not(None),
                Invoice.expire_at <= now,
            )
        ).all()
    )
    for inv in expired_rows:
        inv.status = "EXPIRED"
        expired_n += 1
    if expired_n:
        db.commit()

    q = select(Invoice).where(
        Invoice.owner_id == user.id,
        Invoice.status == "PENDING",
        Invoice.currency != "BDT",
        or_(Invoice.expire_at.is_(None), Invoice.expire_at > now),
    )
    if only_reference:
        q = q.where(Invoice.reference == only_reference)
    pending = list(db.scalars(q).all())
    if not pending:
        return MatchResult(checked=0, matched=0, expired=expired_n)

    api_key, api_secret, coin, network = _get_creds(user, db)
    lookback_ms = int((now - timedelta(minutes=settings.match_lookback_minutes)).timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)

    try:
        deposits = await get_deposit_history(
            api_key, api_secret,
            coin=coin,
            start_time_ms=lookback_ms,
            end_time_ms=end_ms,
            status=1,
            limit=200,
        )
    except Exception as exc:
        raise HTTPException(502, f"Binance error: {exc}") from exc

    used = set(db.scalars(select(MatchedTx.tx_id).where(MatchedTx.owner_id == user.id)).all())
    for inv in db.scalars(
        select(Invoice).where(Invoice.owner_id == user.id, Invoice.provider_tx_id.is_not(None))
    ).all():
        if inv.provider_tx_id:
            used.add(inv.provider_tx_id)

    candidates: list[tuple[str, Decimal, str]] = []
    for d in deposits:
        tx_id = str(d.get("txId") or d.get("id") or "").strip()
        if not tx_id:
            continue
        try:
            amount = Decimal(str(d.get("amount")))
        except Exception:
            continue
        candidates.append((tx_id, amount, str(d.get("coin") or coin).upper()))

    matched: list[Invoice] = []
    for inv in pending:
        expected = Decimal(str(inv.amount))
        cur = inv.currency.upper()
        for tx_id, amount, c in candidates:
            if tx_id in used or c != cur or not _dec_eq(amount, expected):
                continue
            inv.status = "PAID"
            inv.provider_tx_id = tx_id
            inv.paid_at = now
            used.add(tx_id)
            db.add(
                MatchedTx(
                    owner_id=user.id,
                    tx_id=tx_id,
                    invoice_reference=inv.reference,
                    amount=amount,
                    currency=cur,
                )
            )
            matched.append(inv)
            break

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Duplicate transaction race — retry")

    for inv in matched:
        db.refresh(inv)
        await _send_notify(inv, user)
    if matched:
        db.commit()

    return MatchResult(
        checked=len(pending),
        matched=len(matched),
        expired=expired_n,
        invoices=[InvoiceOut.model_validate(i) for i in matched],
    )


# ═══════════════ Mobile money via SMS forwarding (bKash / Nagad / Rocket / Upay) ═══════════════

def _new_sms_secret() -> str:
    return "mf_" + secrets.token_urlsafe(24)


def _mfs_accounts(user: User, db: Session) -> list[MfsAccount]:
    return list(db.scalars(select(MfsAccount).where(MfsAccount.user_id == user.id).order_by(MfsAccount.id)).all())


def _mfs_out(cfg: MfsConfig | None, accounts: list[MfsAccount]) -> MfsConfigOut:
    base = settings.public_base_url.rstrip("/")
    ok = bool(cfg and cfg.is_configured)
    return MfsConfigOut(
        is_configured=ok,
        accounts=[MfsAccountOut(provider=a.provider, number=a.number) for a in accounts],
        allowed_senders=cfg.allowed_senders if cfg else DEFAULT_SENDERS,
        sms_url=f"{base}/api/v1/sms/{cfg.sms_secret}" if ok else None,
        supported=list(PROVIDERS.keys()),
    )


def _require_mfs(user: User, db: Session) -> tuple[MfsConfig, list[MfsAccount]]:
    cfg = db.scalar(select(MfsConfig).where(MfsConfig.user_id == user.id))
    accounts = _mfs_accounts(user, db)
    if not cfg or not cfg.is_configured or not accounts:
        raise HTTPException(400, "Mobile-money (bKash/Nagad/Rocket/Upay) not configured")
    return cfg, accounts


def _mark_mfs_paid(db: Session, user: User, inv: Invoice, sms: SmsTx, now: datetime) -> None:
    inv.status = "PAID"
    inv.provider_tx_id = sms.trx_id
    inv.paid_at = now
    sms.invoice_reference = inv.reference
    sms.matched_at = now
    db.add(
        MatchedTx(
            owner_id=user.id,
            tx_id=sms.trx_id,
            invoice_reference=inv.reference,
            amount=sms.amount,
            currency="BDT",
        )
    )


async def _run_mfs_match(user: User, db: Session, only_reference: str | None = None) -> MatchResult:
    """Auto-match unmatched SMS to PENDING BDT invoices.

    Matches only when exactly ONE pending invoice fits (same amount, allowed service,
    created before the SMS arrived). Ambiguous cases wait for the TrxID claim.
    """
    now = _utcnow()
    pending_all = list(
        db.scalars(
            select(Invoice).where(
                Invoice.owner_id == user.id,
                Invoice.status == "PENDING",
                Invoice.currency == "BDT",
                or_(Invoice.expire_at.is_(None), Invoice.expire_at > now),
            )
        ).all()
    )
    targets = [i for i in pending_all if only_reference is None or i.reference == only_reference]
    if not targets:
        return MatchResult(checked=0, matched=0)

    sms_rows = list(
        db.scalars(
            select(SmsTx)
            .where(SmsTx.owner_id == user.id, SmsTx.invoice_reference.is_(None))
            .order_by(SmsTx.received_at)
        ).all()
    )
    matched: list[Invoice] = []
    for sms in sms_rows:
        inv = pick_invoice(sms, pending_all, targets)
        if inv:
            _mark_mfs_paid(db, user, inv, sms, now)
            matched.append(inv)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Duplicate transaction race — retry")

    for inv in matched:
        db.refresh(inv)
        await _send_notify(inv, user)
    if matched:
        db.commit()
    return MatchResult(
        checked=len(targets),
        matched=len(matched),
        invoices=[InvoiceOut.model_validate(i) for i in matched],
    )


async def _run_match_for_user(user: User, db: Session, only_reference: str | None = None) -> MatchResult:
    """Run Binance (non-BDT) and mobile-money (BDT) matching and merge results."""
    res = await _run_binance_match(user, db, only_reference)
    mfs = await _run_mfs_match(user, db, only_reference)
    return MatchResult(
        checked=res.checked + mfs.checked,
        matched=res.matched + mfs.matched,
        expired=res.expired,
        invoices=list(res.invoices) + list(mfs.invoices),
    )


@app.put("/api/v1/me/mfs", response_model=MfsConfigOut)
def set_mfs(payload: MfsConfigIn, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    clean: dict[str, str] = {}
    for a in payload.accounts:
        key = a.provider.strip().lower()
        if key not in PROVIDERS:
            raise HTTPException(400, f"Unsupported provider '{a.provider}'. Use: {', '.join(PROVIDERS)}")
        num = normalize_msisdn(a.number)
        if not num:
            raise HTTPException(400, f"Invalid number for {key} (expected 01XXXXXXXXX)")
        clean[key] = num

    cfg = db.scalar(select(MfsConfig).where(MfsConfig.user_id == user.id))
    if not cfg:
        cfg = MfsConfig(user_id=user.id, sms_secret=_new_sms_secret())
        db.add(cfg)
    cfg.allowed_senders = payload.allowed_senders.strip()
    cfg.is_configured = True
    cfg.updated_at = _utcnow()

    for old in _mfs_accounts(user, db):
        db.delete(old)
    db.flush()
    for key, num in clean.items():
        db.add(MfsAccount(user_id=user.id, provider=key, number=num))
    db.commit()
    return _mfs_out(cfg, _mfs_accounts(user, db))


@app.get("/api/v1/me/mfs", response_model=MfsConfigOut)
def get_mfs(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    cfg = db.scalar(select(MfsConfig).where(MfsConfig.user_id == user.id))
    return _mfs_out(cfg, _mfs_accounts(user, db))


@app.post("/api/v1/me/mfs/rotate-secret", response_model=MfsConfigOut)
def rotate_mfs_secret(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    cfg, accounts = _require_mfs(user, db)
    cfg.sms_secret = _new_sms_secret()
    cfg.updated_at = _utcnow()
    db.commit()
    return _mfs_out(cfg, accounts)


@app.get("/api/v1/me/mfs/sms", response_model=list[SmsTxOut])
def list_mfs_sms(
    limit: int = Query(default=50, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return list(
        db.scalars(
            select(SmsTx).where(SmsTx.owner_id == user.id).order_by(SmsTx.received_at.desc()).limit(limit)
        ).all()
    )


@app.post("/api/v1/me/mfs/test-parse")
def test_parse_mfs(payload: SmsTestIn, user: User = Depends(get_current_user)):
    """Dry-run the parser on a REAL SMS (nothing is stored)."""
    p = parse_sms(payload.message, payload.sender)
    if not p:
        return {"parsed": False, "hint": "Check the sender (bKash/NAGAD/16216/Upay) and the SMS wording"}
    return {
        "parsed": True,
        "provider": p.provider,
        "amount": str(p.amount),
        "payer_number": p.payer_number,
        "trx_id": p.trx_id,
    }


_SENDER_KEYS = ("from", "sender", "address", "number", "originating_address")
_MESSAGE_KEYS = ("message", "text", "body", "content", "sms", "msg")


def _first(data: dict, keys: tuple[str, ...]) -> str:
    for k in keys:
        v = data.get(k)
        if v not in (None, ""):
            return str(v)
    return ""


@app.post("/api/v1/sms/{secret}")
@app.post("/api/v1/bkash/sms/{secret}")  # legacy path
async def sms_ingest(secret: str, request: Request, db: Session = Depends(get_db)):
    """Webhook for the SMS-forwarder app on the phone that holds the SIM(s)."""
    cfg = db.scalar(select(MfsConfig).where(MfsConfig.sms_secret == secret))
    if not cfg or not cfg.is_configured:
        raise HTTPException(404, "Not found")
    user = db.scalar(select(User).where(User.id == cfg.user_id))
    if not user or not user.is_active:
        raise HTTPException(404, "Not found")

    data: dict = {}
    try:
        ctype = request.headers.get("content-type", "")
        if "json" in ctype:
            j = await request.json()
            if isinstance(j, dict):
                data = j
        elif "form" in ctype:
            data = {k: str(v) for k, v in (await request.form()).items()}
    except Exception:
        data = {}
    for k, v in request.query_params.items():
        data.setdefault(k, v)

    sender = _first(data, _SENDER_KEYS).strip()
    message = _first(data, _MESSAGE_KEYS).strip()[:1000]
    if not message:
        return {"ok": True, "ignored": "empty_message"}
    if sender and not sender_allowed(sender, cfg.allowed_senders):
        return {"ok": True, "ignored": "sender_not_allowed"}

    parsed = parse_sms(message, sender)
    if not parsed:
        return {"ok": True, "ignored": "not_a_receive_sms"}

    enabled = {a.provider for a in _mfs_accounts(user, db)}
    if parsed.provider not in enabled:
        return {"ok": True, "ignored": f"{parsed.provider}_not_enabled"}

    db.add(
        SmsTx(
            owner_id=user.id,
            provider=parsed.provider,
            trx_id=parsed.trx_id,
            amount=parsed.amount,
            payer_number=parsed.payer_number,
            raw_message=message,
        )
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return {"ok": True, "ignored": "duplicate"}

    res = await _run_mfs_match(user, db)
    return {"ok": True, "stored": True, "provider": parsed.provider, "trx_id": parsed.trx_id, "matched": res.matched}


@app.post("/api/v1/invoices/{reference}/claim", response_model=InvoiceOut)
@app.post("/api/v1/invoices/{reference}/claim-bkash", response_model=InvoiceOut)  # legacy path
async def claim_mfs(
    reference: str,
    payload: ClaimIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Customer gives their TrxID/TxnID; verify against received SMS and mark PAID."""
    inv = db.scalar(select(Invoice).where(Invoice.reference == reference, Invoice.owner_id == user.id))
    if not inv:
        raise HTTPException(404, "Invoice not found")
    if inv.currency != "BDT":
        raise HTTPException(400, "Not a BDT (mobile-money) invoice")
    if inv.status == "PAID":
        return inv
    if inv.status != "PENDING":
        raise HTTPException(409, f"Invoice is {inv.status}")
    now = _utcnow()
    if inv.expire_at and aware(inv.expire_at) <= now:
        inv.status = "EXPIRED"
        db.commit()
        raise HTTPException(409, "Invoice expired")

    trx = payload.trx_id.strip().upper()
    sms = db.scalar(select(SmsTx).where(SmsTx.owner_id == user.id, SmsTx.trx_id == trx))
    if not sms:
        raise HTTPException(404, "TrxID not found (SMS may not have arrived yet — retry shortly)")
    if sms.invoice_reference:
        raise HTTPException(409, "TrxID already used")
    if not provider_ok(inv.network, sms.provider):
        raise HTTPException(409, f"Payment was made via {sms.provider}, invoice expects {inv.network}")
    if not dec_eq(sms.amount, inv.amount):
        raise HTTPException(409, "Amount does not match invoice")
    if aware(sms.received_at) < aware(inv.created_at):
        raise HTTPException(409, "Payment predates this invoice")

    _mark_mfs_paid(db, user, inv, sms, now)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Duplicate transaction race — retry")
    db.refresh(inv)
    await _send_notify(inv, user)
    db.commit()
    return inv


@app.post("/api/v1/match", response_model=MatchResult)
async def match_all(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return await _run_match_for_user(user, db)


@app.post("/api/v1/invoices/{reference}/match", response_model=MatchResult)
async def match_one(reference: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return await _run_match_for_user(user, db, only_reference=reference)


@app.post("/api/v1/expire-stale", response_model=dict)
def expire_stale(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    now = _utcnow()
    rows = list(
        db.scalars(
            select(Invoice).where(
                Invoice.owner_id == user.id,
                Invoice.status == "PENDING",
                Invoice.expire_at.is_not(None),
                Invoice.expire_at <= now,
            )
        ).all()
    )
    for inv in rows:
        inv.status = "EXPIRED"
    db.commit()
    return {"expired": len(rows)}


# ═══════════════ Admin ═══════════════

@app.get("/api/v1/admin/stats", response_model=AdminStats)
def admin_stats(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    users_total = db.scalar(select(func.count()).select_from(User)) or 0
    users_active = db.scalar(select(func.count()).select_from(User).where(User.is_active == True)) or 0  # noqa
    users_bn = db.scalar(select(func.count()).select_from(BinanceConfig).where(BinanceConfig.is_configured == True)) or 0  # noqa
    inv_total = db.scalar(select(func.count()).select_from(Invoice)) or 0
    inv_pending = db.scalar(select(func.count()).select_from(Invoice).where(Invoice.status == "PENDING")) or 0
    inv_paid = db.scalar(select(func.count()).select_from(Invoice).where(Invoice.status == "PAID")) or 0
    vol = db.scalar(select(func.coalesce(func.sum(Invoice.amount), 0)).where(Invoice.status == "PAID"))
    return AdminStats(
        users_total=users_total,
        users_active=users_active,
        users_with_binance=users_bn,
        invoices_total=inv_total,
        invoices_pending=inv_pending,
        invoices_paid=inv_paid,
        paid_volume=Decimal(str(vol or 0)),
    )


@app.get("/api/v1/admin/users", response_model=list[AdminUserOut])
def admin_list_users(
    q: str | None = None,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    query = select(User).order_by(User.id.desc())
    if q:
        like = f"%{q.strip()}%"
        query = query.where(or_(User.email.ilike(like), User.display_name.ilike(like)))
    users = list(db.scalars(query.limit(200)).all())
    out = []
    for u in users:
        cfg = db.scalar(select(BinanceConfig).where(BinanceConfig.user_id == u.id))
        cnt = db.scalar(select(func.count()).select_from(Invoice).where(Invoice.owner_id == u.id)) or 0
        paid = db.scalar(
            select(func.count()).select_from(Invoice).where(Invoice.owner_id == u.id, Invoice.status == "PAID")
        ) or 0
        out.append(
            AdminUserOut(
                id=u.id,
                email=u.email,
                display_name=u.display_name,
                is_admin=u.is_admin,
                is_active=u.is_active,
                created_at=u.created_at,
                binance_configured=bool(cfg and cfg.is_configured),
                invoice_count=cnt,
                paid_count=paid,
            )
        )
    return out


@app.post("/api/v1/admin/users/{user_id}/toggle-active", response_model=AdminUserOut)
def admin_toggle(user_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    u = db.scalar(select(User).where(User.id == user_id))
    if not u:
        raise HTTPException(404, "User not found")
    if u.is_admin:
        raise HTTPException(400, "Cannot disable admin")
    u.is_active = not u.is_active
    db.commit()
    db.refresh(u)
    cfg = db.scalar(select(BinanceConfig).where(BinanceConfig.user_id == u.id))
    cnt = db.scalar(select(func.count()).select_from(Invoice).where(Invoice.owner_id == u.id)) or 0
    paid = db.scalar(
        select(func.count()).select_from(Invoice).where(Invoice.owner_id == u.id, Invoice.status == "PAID")
    ) or 0
    return AdminUserOut(
        id=u.id,
        email=u.email,
        display_name=u.display_name,
        is_admin=u.is_admin,
        is_active=u.is_active,
        created_at=u.created_at,
        binance_configured=bool(cfg and cfg.is_configured),
        invoice_count=cnt,
        paid_count=paid,
    )


@app.get("/api/v1/admin/users/{user_id}/invoices", response_model=list[InvoiceOut])
def admin_user_invoices(
    user_id: int,
    limit: int = 50,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return list(
        db.scalars(
            select(Invoice)
            .where(Invoice.owner_id == user_id)
            .order_by(Invoice.id.desc())
            .limit(min(limit, 200))
        ).all()
    )


@app.post("/api/v1/admin/users/{user_id}/match", response_model=MatchResult)
async def admin_match_user(user_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    u = db.scalar(select(User).where(User.id == user_id))
    if not u:
        raise HTTPException(404, "User not found")
    return await _run_match_for_user(u, db)
