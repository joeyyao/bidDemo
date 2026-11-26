from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    app_secret: str = "dev-secret-change-me"
    database_url: str = "sqlite:///./data/contracts.db"
    dashscope_api_key: str = ""
    dashscope_base_url: str = "https://gateway.ai.philips.com.cn/Smart_bidding_chat/v1/chat/completions"
    dashscope_timeout_seconds: float = 1800
    dashscope_model: str = "qwen3.8-flash"
    cors_origins: str = "http://localhost:5173"
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()
