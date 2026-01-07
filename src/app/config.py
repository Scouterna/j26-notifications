from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    POSTGRES_DSN: str
    DEFAULT_TENANT: str = "jamboree26"
    DEFAULT_TENANT_NAME: str = "J26 Notifications"
    FCM_PROJECT_ID: str
    FCM_CREDENTIALS_JSON: str
    API_PREFIX: str = "/api"
    SESSION_SECRET_KEY: str = "change-me"
    OAUTH_CLIENT_ID: str
    OAUTH_CLIENT_SECRET: str
    OAUTH_METATADATA_URL: str

    model_config = SettingsConfigDict(env_file=".env")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
