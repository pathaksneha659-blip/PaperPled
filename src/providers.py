import os
from dataclasses import dataclass, field

from dotenv import dotenv_values


@dataclass(frozen=True)
class Provider:
    name: str
    url: str
    model: str
    key: str = field(repr=False)
    rpm: int = 8
    tpm: int = 5000
    context_tokens: int = 4096
    max_bytes: int = 24_000


def load_providers() -> list[Provider]:
    env = {**dotenv_values(".env", encoding="utf-8-sig"), **os.environ}
    endpoints = [
        ("GEMINI", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"),
        ("GROQ", "https://api.groq.com/openai/v1/chat/completions"),
        ("DEEPSEEK", "https://api.deepseek.com/chat/completions"),
    ]
    providers = []

    for name, url in endpoints:
        key = env.get(f"{name}_API_KEY")
        model = env.get(f"{name}_MODEL")
        if not key or not model:
            continue

        provider = Provider(
            name=name.lower(),
            url=url,
            model=model,
            key=key,
            rpm=int(env.get(f"{name}_RPM") or 8),
            tpm=int(env.get(f"{name}_TPM") or 5000),
            context_tokens=int(env.get(f"{name}_CONTEXT_TOKENS") or 4096),
            max_bytes=int(env.get(f"{name}_MAX_BYTES") or 24_000),
        )
        if min(provider.rpm, provider.tpm, provider.context_tokens, provider.max_bytes) <= 0:
            raise ValueError(f"{name}: limits must be positive")
        providers.append(provider)

    if not providers:
        raise ValueError("Set an API key and model ID for at least one LLM provider in .env.")
    return providers