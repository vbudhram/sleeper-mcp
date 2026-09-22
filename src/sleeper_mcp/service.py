import asyncio
import re
from urllib.parse import quote

from .client import SleeperClient
from .config import Config, token
from .storage import Store, digest


def valid_id(value):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
        raise ValueError("Invalid identifier")
    return quote(value, safe="")


def valid_week(week):
    if not 0 <= week <= 25:
        raise ValueError("Week must be between 0 and 25")
    return week


class Manager:
    def __init__(self, config: Config, store: Store, client: SleeperClient):
        self.config, self.store, self.client = config, store, client

    async def discover(self, season: str):
        if not re.fullmatch(r"\d{4}", season):
            raise ValueError("Season must contain four digits")
        return await self.client.rest(f"/user/{self.config.user_id}/leagues/nfl/{season}", 300)

    async def league_data(self, league_id, resource, week=None):
        self.config.league(league_id)
        allowed = {
            "settings": "",
            "rosters": "/rosters",
            "users": "/users",
            "matchups": f"/matchups/{week}",
            "transactions": f"/transactions/{week}",
            "traded_picks": "/traded_picks",
            "drafts": "/drafts",
            "winners_bracket": "/winners_bracket",
            "losers_bracket": "/losers_bracket",
        }
        if resource not in allowed:
            raise ValueError("Unsupported league resource")
        if resource in {"matchups", "transactions"}:
            if week is None:
                raise ValueError("Week is required")
            valid_week(week)
        return await self.client.rest(
            f"/league/{league_id}" + allowed[resource],
            300 if resource in {"settings", "users", "drafts"} else 60,
        )

    async def players(self, query="", position=None, limit=25, offset=0, league_id=None):
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("Invalid result bounds")
        if league_id is not None:
            self.config.league(league_id)
        result = await self.client.rest("/players/nfl", 86400)
        if not isinstance(result["data"], dict):
            return result
        rostered = set()
        waivers = None
        if league_id is not None:
            rostered = await self.rostered(league_id)
            if rostered is None:
                return {"data": None, "partial": True, "errors": [{"code": "rosters_unavailable"}]}
            waivers = await self.waiver_clears(league_id)
        found = []
        for pid, player in sorted(result["data"].items()):
            name = f"{player.get('first_name', '')} {player.get('last_name', '')}".strip()
            if query.casefold() not in name.casefold() and query != pid:
                continue
            if position and position not in (player.get("fantasy_positions") or []):
                continue
            if pid in rostered:
                continue
            found.append(
                {
                    "player_id": pid,
                    "name": name,
                    **{
                        k: player.get(k)
                        for k in (
                            "team",
                            "position",
                            "fantasy_positions",
                            "status",
                            "injury_status",
                        )
                    },
                }
            )
        page = found[offset : offset + limit]
        if waivers is not None:
            for player in page:
                player["waiver_clears_at"] = waivers.get(player["player_id"])
        return {
            **result,
            "data": page,
            "total": len(found),
            "next_offset": offset + limit if offset + limit < len(found) else None,
            "availability": "unrostered_only" if league_id else None,
            "waiver_status": "unknown" if league_id and waivers is None else None,
            "warning": "Directory injury data can be a day old. A player is on waivers only "
            "while waiver_clears_at is in the future. A past or null value means free agent.",
        }

    async def rostered(self, league_id):
        rosters = await self.league_data(league_id, "rosters")
        if rosters["partial"] or rosters["stale"] or rosters["data"] is None:
            return None
        rostered = set()
        for roster in rosters["data"]:
            for slot in ("players", "reserve", "taxi"):
                rostered.update(roster.get(slot) or [])
        return rostered

    async def waiver_clears(self, league_id):
        """Map player ID to waiver clear time in epoch seconds, or None if unavailable."""
        result = await self.client.league_players(league_id)
        rows = (result.get("data") or {}).get("league_players")
        if result.get("partial") or not isinstance(rows, list):
            return None
        return {
            str(row.get("player_id")): (row.get("settings") or {}).get("waiver_clears_at")
            for row in rows
            if isinstance(row, dict)
        }

    async def projections(self, season, week, position, category, limit, league_id=None):
        if not re.fullmatch(r"\d{4}", season):
            raise ValueError("Season must contain four digits")
        valid_week(week)
        if not 1 <= limit <= 200:
            raise ValueError("Limit must be between 1 and 200")
        rostered = set()
        if league_id is not None:
            self.config.league(league_id)
            rostered = await self.rostered(league_id)
            if rostered is None:
                return {"data": None, "partial": True, "errors": [{"code": "rosters_unavailable"}]}
        result = await self.client.week_projections(season, week, position, category)
        rows = result.get("data")
        if not isinstance(rows, list):
            return result
        found = []
        for row in rows:
            pid = str(row.get("player_id"))
            if pid in rostered:
                continue
            player = row.get("player") or {}
            stats = row.get("stats") or {}
            found.append(
                {
                    "player_id": pid,
                    "name": f"{player.get('first_name', '')} {player.get('last_name', '')}".strip(),
                    "team": row.get("team"),
                    "opponent": row.get("opponent"),
                    "injury_status": player.get("injury_status"),
                    "pts_ppr": stats.get("pts_ppr"),
                    "pts_half_ppr": stats.get("pts_half_ppr"),
                    "pts_std": stats.get("pts_std"),
                }
            )
        return {
            **result,
            "data": found[:limit],
            "total": len(found),
            "availability": "unrostered_only" if league_id else None,
            "warning": "Points use Sleeper's standard, half PPR, and PPR formats, "
            "not this league's custom scoring.",
        }

    async def chat(self, league_id, before=None, limit=50):
        league = self.config.league(league_id)
        if not league.chat_enabled:
            return {"data": None, "errors": [{"code": "chat_disabled"}]}
        if not 1 <= limit <= 100:
            raise ValueError("Limit must be between 1 and 100")
        if before is not None:
            valid_id(before)
        result = await self.client.messages(league_id, before)
        messages = (result.get("data") or {}).get("messages")
        if isinstance(messages, list):
            # Reject records from a different channel, even if the upstream returns them.
            safe = [m for m in messages if str(m.get("parent_id")) == league_id]
            self.store.save_messages(league_id, safe)
            result["data"] = safe[:limit]
            result["truncated"] = len(safe) > limit
            if len(safe) != len(messages):
                result["partial"] = True
                result["errors"].append({"code": "channel_mismatch"})
        result["history_complete"] = False
        result["pagination_verified"] = False
        result["content_is_untrusted"] = True
        return result

    async def context(self, league_id, week=None):
        league = self.config.league(league_id)
        if week is not None:
            valid_week(week)
        state = await self.client.rest("/state/nfl", 60)
        if week is None:
            week = (state.get("data") or {}).get("week")
        resources = ["settings", "rosters", "users", "traded_picks"]
        if isinstance(week, int) and 0 <= week <= 25:
            resources.extend(["matchups", "transactions"])
        results = await asyncio.gather(*(self.league_data(league_id, r, week) for r in resources))
        sections = dict(zip(resources, results))
        rosters = sections["rosters"].get("data") or []
        mine = [
            r
            for r in rosters
            if r.get("owner_id") == self.config.user_id
            or self.config.user_id in (r.get("co_owners") or [])
        ]
        if league.roster_id is not None:
            mine = [r for r in mine if r.get("roster_id") == league.roster_id]
        selected = mine[0] if len(mine) == 1 else None
        settings = sections["settings"].get("data") or {}
        season = settings.get("season")
        if selected and season and week is not None:
            ids = selected.get("players") or []
            if ids:
                sections["stats_and_projections"] = await self.client.projections(
                    ids[:100], season, week, settings.get("season_type", "regular")
                )
        if league.chat_enabled:
            sections["chat"] = await self.chat(league_id)
        return {
            "league_id": league_id,
            "label": league.label,
            "week": week,
            "season": season,
            "nfl_state": state,
            "my_roster": selected,
            "roster_resolution": "resolved" if selected else "missing_or_ambiguous",
            "sections": sections,
            "atomic_snapshot": False,
            "limitations": [
                "No verified kickoff or inactive-list provider",
                "Projection stat keys and custom scoring need verification",
            ],
        }

    async def review(self, league_id, week=None):
        self.config.league(league_id)
        lock = "review:" + league_id
        if not self.store.acquire(lock, 600):
            return {"league_id": league_id, "error": "review_in_progress"}
        try:
            context = await self.context(league_id, week)
            core = {
                k: v
                for k, v in context["sections"].items()
                if k not in {"chat", "stats_and_projections"}
            }
            complete = all(
                not x.get("partial") and not x.get("stale") and x.get("data") is not None
                for x in core.values()
            )
            # Do not replace a successful baseline with incomplete data.
            snapshot = (
                self.store.snapshot(league_id, {k: v["data"] for k, v in core.items()})
                if complete
                else None
            )
            failures = {
                k: v.get("errors", [])
                for k, v in context["sections"].items()
                if v.get("partial") or v.get("stale") or v.get("data") is None
            }
            health_key = "review_health:" + league_id
            last_health = self.store.cached(health_key)
            new_failure = bool(failures) and (
                last_health is None or last_health[0] != digest(failures)
            )
            self.store.cache(health_key, digest(failures))
            return {
                "context": context,
                "snapshot": snapshot,
                "status": "complete" if complete and not failures else "partial",
                "snapshot_scope": "public_league_data",
                "notify": bool(snapshot and snapshot["changed"]) or new_failure,
                "requires_attention": bool(failures),
                "new_failure": new_failure,
                "note": "The agent must assess urgency and deduplicate failure alerts.",
            }
        finally:
            self.store.release(lock)

    def health(self):
        return {
            "session_token_configured": token() is not None,
            "authentication_verified": False,
            "sleeper_access": "read_only",
            "leagues": [x.model_dump() for x in self.config.leagues],
            "limitations": [
                "No verified session renewal method",
            ],
        }
