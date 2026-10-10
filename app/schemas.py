from datetime import datetime
from decimal import Decimal
from pydantic import BaseModel, Field, ConfigDict, field_validator


class RegisterIn(BaseModel):
    email: str = Field(min_length=5, max_length=255)
    password: str = Field(min_length=8, max_length=128)
    display_name: str | None = Field(default=None, max_length=128)

    @field_validator("email")
    @classmethod
    def email_lower(cls, v: str) -> str:
        return v.lower().strip()


class LoginIn(BaseModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def email_lower(cls, v: str) -> str:
        return v.lower().strip()


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    bot_api_key: str
    is_admin: bool = False


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=128)


class ProfileUpdateIn(BaseModel):
    display_name: str | None = Field(default=None, max_length=128)
    default_notify_url: str | None = Field(default=None, max_length=512)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    email: str
    display_name: str | None
    is_admin: bool
    is_active: bool
    bot_api_key: str
    default_notify_url: str | None = None
    created_at: datetime
    binance_configured: bool = False
    mfs_configured: bool = False


class BinanceConfigIn(BaseModel):
    api_key: str = Field(min_length=10)
    api_secret: str = Field(min_length=10)
    deposit_coin: str = Field(default="USDT", max_length=12)
    deposit_network: str = Field(default="TRX", max_length=32)


class BinanceConfigOut(BaseModel):
    is_configured: bool
    deposit_coin: str
    deposit_network: str
    api_key_masked: str | None = None


class InvoiceCreate(BaseModel):
    customer_id: str = Field(min_length=1, max_length=128)
    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=8)
    currency: str = Field(default="USDT", min_length=2, max_length=12)
    note: str | None = Field(default=None, max_length=256)
    notify_url: str | None = Field(default=None, max_length=512)
    # If true, system picks amount like base + 0.01, 0.02... to reduce collisions
    unique_amount: bool = False
    # Only for currency=BDT: "bkash" | "nagad" | "rocket" | "upay". Empty = customer may use any configured service.
    method: str | None = Field(default=None, max_length=16)


class InvoiceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    reference: str
    customer_id: str
    amount: Decimal
    currency: str
    status: str
    provider_tx_id: str | None = None
    deposit_address: str | None = None
    network: str | None = None
    note: str | None = None
    notify_url: str | None = None
    notify_sent: bool = False
    expire_at: datetime | None = None
    created_at: datetime | None = None
    paid_at: datetime | None = None


class DepositAddressOut(BaseModel):
    coin: str
    network: str
    address: str
    tag: str | None = None


class MatchResult(BaseModel):
    checked: int
    matched: int
    expired: int = 0
    invoices: list[InvoiceOut] = []


class UserStats(BaseModel):
    pending: int
    paid: int
    expired: int
    total: int
    paid_volume: Decimal
    currency: str = "USDT"


class SuggestAmountOut(BaseModel):
    amount: Decimal
    currency: str
    reason: str


class AdminUserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    email: str
    display_name: str | None
    is_admin: bool
    is_active: bool
    created_at: datetime
    binance_configured: bool = False
    invoice_count: int = 0
    paid_count: int = 0


class AdminStats(BaseModel):
    users_total: int
    users_active: int
    users_with_binance: int
    invoices_total: int
    invoices_pending: int
    invoices_paid: int
    paid_volume: Decimal


class MfsAccountIn(BaseModel):
    provider: str = Field(min_length=3, max_length=16)
    number: str = Field(min_length=10, max_length=20)


class MfsAccountOut(BaseModel):
    provider: str
    number: str


class MfsConfigIn(BaseModel):
    accounts: list[MfsAccountIn] = Field(min_length=1, max_length=8)
    allowed_senders: str = Field(default="bKash,NAGAD,Rocket,16216,Upay", max_length=256)


class MfsConfigOut(BaseModel):
    is_configured: bool
    accounts: list[MfsAccountOut] = []
    allowed_senders: str = "bKash,NAGAD,Rocket,16216,Upay"
    sms_url: str | None = None
    supported: list[str] = []


class ClaimIn(BaseModel):
    trx_id: str = Field(min_length=6, max_length=20)


class SmsTestIn(BaseModel):
    message: str = Field(min_length=5, max_length=1000)
    sender: str = Field(default="", max_length=64)


class SmsTxOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    provider: str = "bkash"
    trx_id: str
    amount: Decimal
    payer_number: str
    received_at: datetime
    invoice_reference: str | None = None
    raw_message: str
