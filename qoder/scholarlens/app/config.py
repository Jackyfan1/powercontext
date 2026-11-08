import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

POWERCONTEXT_URL = os.getenv("POWERCONTEXT_URL", "http://127.0.0.1:8710")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2")
DATABASE_PATH = os.getenv("DATABASE_PATH", str(DATA_DIR / "scholarlens.db"))
SERVER_PORT = int(os.getenv("SERVER_PORT", "8000"))

CHUNK_MAX_CHARS = 800
CHUNK_OVERLAP = 100
SEARCH_TOP_K = 10
VECTOR_WEIGHT = 0.7
KEYWORD_WEIGHT = 0.3
