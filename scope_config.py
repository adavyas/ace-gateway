import os

from dotenv import load_dotenv


load_dotenv()

_ENV = os.getenv("ENV", "development").lower()
SECRET_KEY = os.getenv("SECRET_KEY", "dev-insecure-secret")
REFRESH_TOKEN_EXPIRE_DAYS = int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS", "30"))

SCOPE_DATA_DIR = os.path.expanduser(os.getenv("SCOPE_DATA_DIR", "~/.ace"))
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{os.path.join(SCOPE_DATA_DIR, 'scope.db')}")
