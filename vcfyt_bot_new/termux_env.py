"""Interactive .env bootstrap (Termux friendly)."""
import os
from pathlib import Path

ENV_PATH = Path(__file__).parent / ".env"

FIELDS = [
    ("API_ID", "Telegram API ID (my.telegram.org)", True),
    ("API_HASH", "Telegram API HASH", True),
    ("BOT_TOKEN", "Bot token from @BotFather", True),
    ("OWNER_ID", "Aapka Telegram numeric user id (optional, skip ok)", False),
    ("OWNER_USERNAME", "Owner username (buttons ke liye, optional)", False),
    ("TUTORIAL_URL", "Tutorial link (optional)", False),
]


def wizard() -> None:
    print("\n=== BANALL SETUP ===")
    print("Values ek baar bharo, .env me save ho jayengi. Optional ko blank chhod do.\n")
    lines = []
    for key, label, required in FIELDS:
        while True:
            val = input(f"{label}\n  {key}= ").strip()
            if val or not required:
                break
            print("  ! required")
        if val:
            lines.append(f"{key}={val}")
    ENV_PATH.write_text("\n".join(lines) + "\n")
    print(f"\nSaved -> {ENV_PATH}\n")


def load_env() -> None:
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def bootstrap() -> None:
    if not ENV_PATH.exists() and os.isatty(0):
        wizard()
    load_env()
