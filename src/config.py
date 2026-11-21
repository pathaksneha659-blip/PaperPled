import os
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    GITHUB_TOKENS: str = ""

    OUTPUT_DIR: Path = Path("output")

    @property
    def github_token_list(self) -> list[str]:
        tokens = [t.strip() for t in self.GITHUB_TOKENS.split(",") if t.strip()]
        return tokens

settings = Settings()
settings.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)