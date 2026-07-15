from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    POSTGRES_DSN: str
    FCM_PROJECT_ID: str
    FCM_CREDENTIALS_JSON: str
    KC_API: str = "https://admin.dev.id.scouterna.se"
    KC_REALM: str = "jamboree26"
    KC_SA_ACCOUNT: str = "j26-notifications"
    KC_SA_ACCOUNT_KEY: str
    API_PREFIX: str = "/api"
    ROOT_PATH: str = ""
    APP_BASE_URL: str = "https://app.jamboree.se"
    OAUTH_CLIENT_ID: str = ""
    OAUTH_CLIENT_SECRET: str = ""
    OAUTH_METATADATA_URL: str = ""
    ACTIVE_USER_TIMEOUT_SECONDS: int = 600

    model_config = SettingsConfigDict(env_file=".env")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
