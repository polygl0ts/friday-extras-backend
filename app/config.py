from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EXTRAS_")

    # Base URL of the rCTF instance this service is a companion to, e.g.
    # "https://friday.polygl0ts.ch". No trailing slash.
    rctf_origin: str = "http://localhost:8080"

    # rCTF's REST API path prefix for v2.
    rctf_api_base: str = "/api/v2"

    # Optional bearer forwarded with the challenge-list read.
    rctf_admin_token: str = ""

    # SQLAlchemy URL for this service's own database.
    database_url: str = "sqlite:///./friday-extras-backend.db"

    # Discord webhook URL for writeups, injected from the Ansible vault at deploy time.
    discord_webhook_url: str = ""

    # Public origin of the friday-frontend frontend, e.g.
    # "https://friday.polygl0ts.ch". Used to deep-link Discord notifications
    # at the admin review queue. No trailing slash. Left empty, notifications
    # simply carry no link.
    web_origin: str = ""

    # Origins allowed to call this API from a browser. Injected as
    # EXTRAS_CORS_ORIGINS by the Ansible role, from `extras_cors_origins`.
    cors_origins: list[str] = []

    # Flag prefix for identification and removal from public writeup part,
    # matched case-insensitively against `prefix{...}`.
    flag_prefixes: list[str] = ["friday", "EPFL"]

    # How long a resolved (token -> team identity) lookup is cached before
    # we ask rCTF again.
    identity_cache_seconds: int = 30

    # Lake Quals invitations. A Discord application of its own, separate from
    # the writeup webhook above, because its buttons call back into this
    # service. Left empty, invites are stored but nothing is posted.
    discord_bot_token: str = ""
    discord_public_key: str = ""
    discord_invite_channel_id: str = ""

    # Mailbox the invitation tokens are sent from.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    # Defaults to `smtp_user`.
    smtp_from: str = ""


settings = Settings()
