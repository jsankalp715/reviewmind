import os
from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
GITHUB_TOKEN: str = os.getenv("GITHUB_TOKEN", "")
GITHUB_WEBHOOK_SECRET: str = os.getenv("GITHUB_WEBHOOK_SECRET", "")
PORT: int = int(os.getenv("PORT", "8000"))

GEMINI_MODEL = "gemini-2.0-flash"
MAX_DIFF_TOKENS = 4000
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.0  # seconds

APP_VERSION = "1.0.0"
