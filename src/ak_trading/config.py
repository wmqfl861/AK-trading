import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    tq_user: str
    tq_pass: str = field(repr=False)
    default_symbol: str = "KQ.m@SHFE.rb"


def load_settings(env_file: Path | None = None) -> Settings:
    load_dotenv(env_file or ROOT / ".env")
    user = os.getenv("TQ_USER", "").strip()
    password = os.getenv("TQ_PASS", "").strip()
    if not user or not password:
        raise RuntimeError(
            "Missing TQ_USER / TQ_PASS. Copy .env.example to .env and fill credentials."
        )
    symbol = os.getenv("TQ_DEFAULT_SYMBOL", "KQ.m@SHFE.rb").strip() or "KQ.m@SHFE.rb"
    return Settings(tq_user=user, tq_pass=password, default_symbol=symbol)
