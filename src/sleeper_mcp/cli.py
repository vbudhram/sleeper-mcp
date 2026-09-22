import argparse
import asyncio
import getpass
import json
import os
import subprocess
import sys

from .client import SleeperClient
from .config import load_config
from .service import Manager
from .storage import Store


async def execute(args):
    store = Store(os.getenv("SLEEPER_STATE_DIR", ".state"))
    config = load_config()
    if getattr(args, "cache_mode", None):
        config.cache.mode = args.cache_mode
    client = SleeperClient(store, policy=config.cache)
    manager = Manager(config, store, client)
    try:
        if args.command == "discover":
            print(json.dumps(await manager.discover(args.season), indent=2))
        elif args.command == "review":
            reports = []
            for league in manager.config.leagues:
                if args.league and args.league != league.league_id:
                    continue
                try:
                    reports.append(await manager.review(league.league_id, args.week))
                except (ValueError, TypeError, KeyError) as error:
                    reports.append(
                        {
                            "league_id": league.league_id,
                            "status": "invalid_upstream_data",
                            "detail": f"{type(error).__name__}: {error}",
                        }
                    )
            print(json.dumps({"reports": reports}, indent=2))
        elif args.command == "health":
            print(json.dumps(manager.health(), indent=2))
        elif args.command == "prune":
            if args.days < 1:
                raise ValueError("Retention must be at least one day")
            store.prune(args.days)
            print(json.dumps({"retention_days": args.days}))
    finally:
        await client.close()
        store.db.close()


def main():
    parser = argparse.ArgumentParser(description="Sleeper manager setup and scheduled review")
    commands = parser.add_subparsers(dest="command", required=True)
    discover = commands.add_parser("discover")
    discover.add_argument("--season", required=True)
    review = commands.add_parser("review")
    review.add_argument("--league")
    review.add_argument("--week", type=int)
    review.add_argument("--cache-mode", choices=["normal", "cache_only", "refresh"])
    commands.add_parser("health")
    prune = commands.add_parser("prune")
    prune.add_argument("--days", type=int, default=30)
    commands.add_parser("with-token", help="Read a hidden token and run the MCP server once")
    args = parser.parse_args()
    if args.command == "with-token":
        # The child receives the secret through its environment, not its argument list.
        secret = getpass.getpass("Sleeper session token: ")
        env = {**os.environ, "SLEEPER_SESSION_TOKEN": secret}
        raise SystemExit(subprocess.call([sys.executable, "-m", "sleeper_mcp"], env=env))
    asyncio.run(execute(args))
