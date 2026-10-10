"""Mobile-money (bKash / Nagad / Rocket / Upay) SMS parsing and invoice matching.

None of these services offer an API for personal accounts. The phone that holds the
SIM forwards the "money received" SMS to PayHub; this module parses it.

IMPORTANT: SMS wording differs per service and can change. Always verify with a REAL
SMS using POST /api/v1/me/mfs/test-parse (dashboard: "Test Parse"). To add or tweak a
service, edit PROVIDERS / the regexes below.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Iterable

_BN_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")


@dataclass(frozen=True)
class Provider:
    key: str
    label: str
    senders: tuple[str, ...]   # normalized (lowercase, no spaces/dashes/plus)
    keywords: tuple[str, ...]  # words that identify the service inside the body


PROVIDERS: dict[str, Provider] = {
    "bkash": Provider("bkash", "bKash", ("bkash",), ("bkash",)),
    "nagad": Provider("nagad", "Nagad", ("nagad",), ("nagad",)),
    "rocket": Provider("rocket", "Rocket", ("rocket", "16216", "dbbl"), ("rocket", "dbbl")),
    "upay": Provider("upay", "Upay", ("upay",), ("upay",)),
}

DEFAULT_SENDERS = "bKash,NAGAD,Rocket,16216,Upay"

# Outgoing / non-customer-payment messages — never treat as incoming payment.
_REJECT = re.compile(
    r"cash\s*-?\s*out|cash\s*-?\s*in|\bsent\b|send\s*money\s+to|payment\s+to|"
    r"recharge|\bbill\b|withdraw|transfer\s+to|\botp\b|"
    r"ক্যাশ|পাঠিয়েছেন|রিচার্জ|বিল",
    re.I,
)
_INCOMING = re.compile(r"received|পেয়েছেন|গ্রহণ", re.I)
_AMOUNT = re.compile(r"(?<![A-Za-z])(?:Tk\.?|৳|BDT)\s*:?\s*([\d,]+(?:\.\d{1,2})?)", re.I)
_SKIP_BEFORE_AMOUNT = re.compile(r"fee|balance|charge|bal\b|commission", re.I)
_PAYER = re.compile(
    r"(?:from|sender|a/c|ac|থেকে)\s*[:\-]?\s*(?:a/c\s*[:\-]?\s*)?"
    r"(\+?(?:880)?0?1[3-9]\d{8})(?:-?\d)?(?!\d)",
    re.I,
)
_TRX = re.compile(
    r"(?:Trx\s*ID|Txn\s*ID|Transaction\s*ID|Trans\s*ID|TrxID)\s*[:\-]?\s*([A-Z0-9]{6,20})\b",
    re.I,
)
# Accepts 01XXXXXXXXX, +8801XXXXXXXXX, 8801..., and Rocket's 12-digit "01XXXXXXXXX-X"
_MSISDN = re.compile(r"^(?:\+?880|0)?(1[3-9]\d{8})(?:-?\d)?$")


@dataclass
class ParsedSms:
    provider: str
    amount: Decimal
    payer_number: str
    trx_id: str


def normalize_msisdn(raw: str) -> str | None:
    """Return 01XXXXXXXXX or None."""
    s = re.sub(r"[\s]", "", (raw or "").translate(_BN_DIGITS))
    m = _MSISDN.match(s)
    return "0" + m.group(1) if m else None


def normalize_sender(s: str) -> str:
    return re.sub(r"[\s\-+]", "", s or "").lower()


def sender_allowed(sender: str, allowed_csv: str) -> bool:
    """True if sender is in the allowed list (empty list = allow all)."""
    allowed = [normalize_sender(x) for x in (allowed_csv or "").split(",") if x.strip()]
    if not allowed:
        return True
    return normalize_sender(sender) in allowed


def detect_provider(sender: str, text: str) -> str | None:
    s = normalize_sender(sender)
    if s:
        for p in PROVIDERS.values():
            if s in p.senders:
                return p.key
        return None  # sender given but not a known service
    low = (text or "").lower()
    for p in PROVIDERS.values():
        if any(k in low for k in p.keywords):
            return p.key
    if "trxid" in low:  # bKash spelling; Nagad/Rocket use TxnID
        return "bkash"
    return None


def parse_sms(text: str, sender: str = "") -> ParsedSms | None:
    """Parse an incoming-money SMS from a supported service. None for anything else."""
    if not text:
        return None
    t = text.translate(_BN_DIGITS)
    if _REJECT.search(t) or not _INCOMING.search(t):
        return None
    provider = detect_provider(sender, t)
    if not provider:
        return None

    amount = None
    for m in _AMOUNT.finditer(t):
        ctx = t[max(0, m.start() - 14): m.start()]
        if _SKIP_BEFORE_AMOUNT.search(ctx):
            continue
        try:
            amount = Decimal(m.group(1).replace(",", ""))
        except InvalidOperation:
            continue
        break

    m_payer = _PAYER.search(t)
    m_trx = _TRX.search(t)
    if amount is None or amount <= 0 or not m_payer or not m_trx:
        return None
    payer = normalize_msisdn(m_payer.group(1))
    if not payer:
        return None
    return ParsedSms(provider, amount, payer, m_trx.group(1).upper())


# ───────────── matching helpers (pure, unit-testable) ─────────────

def aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def dec_eq(a, b) -> bool:
    try:
        return Decimal(str(a)) == Decimal(str(b))
    except Exception:
        return False


def provider_ok(invoice_network: str | None, sms_provider: str) -> bool:
    """Invoice with a specific method only accepts that service; 'MFS'/empty accepts any."""
    n = (invoice_network or "").strip().lower()
    return n in ("", "mfs") or n == (sms_provider or "").lower()


def invoice_matches_sms(inv, sms) -> bool:
    return (
        provider_ok(inv.network, sms.provider)
        and dec_eq(inv.amount, sms.amount)
        and aware(inv.created_at) <= aware(sms.received_at)
    )


def pick_invoice(sms, pending_all: Iterable, targets: Iterable):
    """Auto-match only when exactly ONE pending invoice fits (and it is a target)."""
    targets = list(targets)
    cands = [i for i in pending_all if i.status == "PENDING" and invoice_matches_sms(i, sms)]
    if len(cands) == 1 and cands[0] in targets:
        return cands[0]
    return None
