"""Offline database initialization and retention cleanup for the login-free service."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "api"))

from dotenv import load_dotenv

load_dotenv()

from settings import Settings
from store import Store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["init-db", "cleanup"])
    args = parser.parse_args()
    store = Store(Settings.from_env().database_url)
    try:
        if args.command == "init-db":
            store.initialize()
        else:
            store.cleanup()
    finally:
        store.engine.dispose()


if __name__ == "__main__":
    main()
