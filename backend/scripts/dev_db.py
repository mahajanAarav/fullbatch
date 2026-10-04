"""
Start (or reuse) a local Postgres for development, and bring its tables up to date.

The data lives in backend/.devdb (git-ignored) and the database keeps running
after this script exits, so run the script once and then start the API.

    python backend/scripts/dev_db.py

It prints the DATABASE_URL line to put in your .env.
"""

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import pixeltable_pgserver  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

DATA_DIR = BACKEND / ".devdb"


def main() -> None:
    # cleanup_mode=None: leave Postgres running when this script ends.
    server = pixeltable_pgserver.get_server(DATA_DIR, cleanup_mode=None)
    url = server.get_uri().replace("postgresql://", "postgresql+psycopg://", 1)

    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    command.upgrade(cfg, "head")

    print("\nLocal database is up and its tables are current.")
    print("Put this line in your .env (replacing any existing DATABASE_URL):\n")
    print(f"DATABASE_URL={url}\n")


if __name__ == "__main__":
    main()
