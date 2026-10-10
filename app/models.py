from datetime import datetime, timezone
from sqlalchemy import Boolean, DateTime, Integer, Numeric, String, Text, UniqueConstraint, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


def utcnow():
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "payhub_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    bot_api_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # Optional default webhook when any invoice is paid
    default_notify_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    binance_config: Mapped["BinanceConfig | None"] = relationship(
        "BinanceConfig", back_populates="user", uselist=False
    )
    invoices: Mapped[list["Invoice"]] = relationship("Invoice", back_populates="owner")


class BinanceConfig(Base):
    __tablename__ = "payhub_binance_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("payhub_users.id"), unique=True, index=True)
    api_key_enc: Mapped[str] = mapped_column(Text, default="")
    api_secret_enc: Mapped[str] = mapped_column(Text, default="")
    deposit_coin: Mapped[str] = mapped_column(String(12), default="USDT")
    deposit_network: Mapped[str] = mapped_column(String(32), default="TRX")
    is_configured: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped["User"] = relationship("User", back_populates="binance_config")


class Invoice(Base):
    __tablename__ = "payhub_invoices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("payhub_users.id"), index=True)
    reference: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    customer_id: Mapped[str] = mapped_column(String(128), index=True)
    amount: Mapped[object] = mapped_column(Numeric(18, 8))
    currency: Mapped[str] = mapped_column(String(12), default="USDT")
    status: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)

    provider_tx_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    deposit_address: Mapped[str | None] = mapped_column(String(256), nullable=True)
    network: Mapped[str | None] = mapped_column(String(32), nullable=True)
    note: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # Per-invoice callback (overrides user default)
    notify_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    notify_sent: Mapped[bool] = mapped_column(Boolean, default=False)

    expire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    owner: Mapped["User"] = relationship("User", back_populates="invoices")


class MatchedTx(Base):
    __tablename__ = "payhub_matched_tx"
    __table_args__ = (UniqueConstraint("owner_id", "tx_id", name="uq_owner_tx"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("payhub_users.id"), index=True)
    tx_id: Mapped[str] = mapped_column(String(128), index=True)
    invoice_reference: Mapped[str] = mapped_column(String(40), index=True)
    amount: Mapped[object] = mapped_column(Numeric(18, 8))
    currency: Mapped[str] = mapped_column(String(12))
    matched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MfsConfig(Base):
    """Mobile-money (SMS forwarding) settings: one webhook secret per user."""
    __tablename__ = "payhub_mfs_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("payhub_users.id"), unique=True, index=True)
    sms_secret: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    allowed_senders: Mapped[str] = mapped_column(String(256), default="bKash,NAGAD,Rocket,16216,Upay")
    is_configured: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MfsAccount(Base):
    """The receiving number for one service (bkash / nagad / rocket / upay)."""
    __tablename__ = "payhub_mfs_account"
    __table_args__ = (UniqueConstraint("user_id", "provider", name="uq_mfs_user_provider"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("payhub_users.id"), index=True)
    provider: Mapped[str] = mapped_column(String(16))
    number: Mapped[str] = mapped_column(String(20))


class SmsTx(Base):
    """Incoming-money SMS parsed from bKash. One row per TrxID per owner."""
    __tablename__ = "payhub_sms_tx"
    __table_args__ = (UniqueConstraint("owner_id", "trx_id", name="uq_sms_owner_trx"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("payhub_users.id"), index=True)
    provider: Mapped[str] = mapped_column(String(16), default="bkash")
    trx_id: Mapped[str] = mapped_column(String(32), index=True)
    amount: Mapped[object] = mapped_column(Numeric(18, 2))
    payer_number: Mapped[str] = mapped_column(String(20))
    raw_message: Mapped[str] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    invoice_reference: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    matched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
