from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "sqlite:///./payhub.db"
    public_base_url: str = "http://127.0.0.1:8000"

    encryption_key: str = ""
    jwt_secret: str = "change-me-jwt-secret-min-32-chars-long"
    jwt_expire_hours: int = 72

    admin_email: str = "admin@example.com"
    admin_password: str = "change-this-strong-password"

    default_deposit_coin: str = "USDT"
    default_deposit_network: str = "TRX"
    match_lookback_minutes: int = 120
    invoice_expire_minutes: int = 60

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
