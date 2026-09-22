import asyncio
import re
from urllib.parse import quote

from .client import SleeperClient, cache_mode
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


def score(stats, scoring):
    """Apply a league's scoring settings to one stat line."""
    total = 0.0
    for key, value in (stats or {}).items():
        if key in scoring and isinstance(value, (int, float)):
            total += value * scoring[key]
    return round(total, 2)


def display_name(player):
    name = f"{player.get('first_name', '')} {player.get('last_name', '')}".strip()
    return f"{name} {player.get('position') or ''}-{player.get('team') or 'FA'}"


class Manager:
    def __init__(self, config: Config, store: Store, client: SleeperClient):
        self.config, self.store, self.client = config, store, client

    async def discover(self, season: str):
        if not re.fullmatch(r"\d{4}", season):
            raise ValueError("Season must contain four digits")
        return await self.client.rest(f"/user/{self.config.user_id}/leagues/nfl/{season}", 300)

    async def league_data(self, league_id, resource, week=None, names=False):
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
        result = await self.client.rest(
            f"/league/{league_id}" + allowed[resource],
            300 if resource in {"settings", "users", "drafts"} else 60,
        )
        if names and resource in {"rosters", "matchups"} and isinstance(result.get("data"), list):
            result["player_names"] = await self.player_names(result["data"])
        return result

    async def directory(self):
        """The cached player directory, or None when it is unavailable."""
        result = await self.client.rest("/players/nfl", 86400)
        return result["data"] if isinstance(result.get("data"), dict) else None

    async def resolve(self, player_ids):
        """Map player IDs to display names. Unknown IDs are omitted."""
        players = await self.directory()
        if players is None:
            return None
        return {
            pid: display_name(players[pid]) for pid in sorted(set(player_ids)) if pid in players
        }

    async def player_names(self, rows):
        """Map every player ID in the given roster or matchup rows to a display name."""
        ids = set()
        for row in rows:
            for slot in ("players", "starters", "reserve", "taxi"):
                ids.update(x for x in (row.get(slot) or []) if isinstance(x, str))
        return await self.resolve(ids) if ids else None

    async def trending(self, kind, lookback_hours, limit):
        if not 1 <= lookback_hours <= 168 or not 1 <= limit <= 100:
            raise ValueError("Invalid trend bounds")
        result = await self.client.rest(
            f"/players/nfl/trending/{kind}?lookback_hours={lookback_hours}&limit={limit}", 300
        )
        rows = result.get("data")
        players = await self.directory() if isinstance(rows, list) else None
        if players is not None:
            for row in rows:
                player = players.get(str(row.get("player_id"))) or {}
                row["name"] = display_name(player).rsplit(" ", 1)[0] if player else None
                row["position"] = player.get("position")
                row["team"] = player.get("team")
        result["attribution"] = "Sleeper"
        return result

    async def standings(self, league_id):
        rosters, users = await asyncio.gather(
            self.league_data(league_id, "rosters"), self.league_data(league_id, "users")
        )
        if not isinstance(rosters.get("data"), list):
            return rosters
        owners = {u.get("user_id"): u for u in users.get("data") or []}
        rows = []
        for r in rosters["data"]:
            st = r.get("settings") or {}
            user = owners.get(r.get("owner_id")) or {}
            rows.append(
                {
                    "roster_id": r.get("roster_id"),
                    "owner_id": r.get("owner_id"),
                    "display_name": user.get("display_name"),
                    "team_name": (user.get("metadata") or {}).get("team_name"),
                    "wins": st.get("wins", 0),
                    "losses": st.get("losses", 0),
                    "ties": st.get("ties", 0),
                    "points_for": st.get("fpts", 0) + st.get("fpts_decimal", 0) / 100,
                    "points_against": st.get("fpts_against", 0)
                    + st.get("fpts_against_decimal", 0) / 100,
                    "streak": (r.get("metadata") or {}).get("streak"),
                }
            )
        rows.sort(key=lambda x: (-x["wins"], x["losses"], -x["points_for"]))
        for rank, row in enumerate(rows, 1):
            row["rank"] = rank
        return {**rosters, "data": rows, "source": [rosters["source"], users.get("source")]}

    async def schedule(self, season, week):
        if not re.fullmatch(r"\d{4}", season):
            raise ValueError("Season must contain four digits")
        valid_week(week)
        result = await self.client.schedule(season)
        games = result.get("data")
        if not isinstance(games, list):
            return result
        teams = {g.get(side) for g in games for side in ("home", "away")} - {None}
        this_week = [g for g in games if g.get("week") == week]
        playing = {g.get(side) for g in this_week for side in ("home", "away")}
        return {
            **result,
            "data": this_week,
            "bye_teams": sorted(teams - playing),
            "warning": "Upstream gives a game date and status, not a kickoff time.",
        }

    def my_roster(self, league, rosters):
        mine = [
            r
            for r in rosters
            if r.get("owner_id") == self.config.user_id
            or self.config.user_id in (r.get("co_owners") or [])
        ]
        if league.roster_id is not None:
            mine = [r for r in mine if r.get("roster_id") == league.roster_id]
        return mine[0] if len(mine) == 1 else None

    async def matchup(self, league_id, week=None):
        league = self.config.league(league_id)
        if week is None:
            state = await self.client.rest("/state/nfl", 60)
            week = (state.get("data") or {}).get("week")
        if not isinstance(week, int):
            return {"data": None, "partial": True, "errors": [{"code": "week_unavailable"}]}
        valid_week(week)
        settings, rosters, users, matchups = await asyncio.gather(
            *(
                self.league_data(league_id, r, week)
                for r in ("settings", "rosters", "users", "matchups")
            )
        )
        if not isinstance(matchups.get("data"), list) or not isinstance(rosters.get("data"), list):
            return {"data": None, "partial": True, "errors": [{"code": "league_data_unavailable"}]}
        rules = settings.get("data") or {}
        scoring = rules.get("scoring_settings") or {}
        season = rules.get("season")
        projections = await self.client.week_projections_all(season, week) if season else {}
        proj = {
            str(r.get("player_id")): r
            for r in (projections.get("data") or [])
            if isinstance(r, dict)
        }
        players = await self.directory() or {}
        owners = {u.get("user_id"): u for u in users.get("data") or []}
        by_roster = {r.get("roster_id"): r for r in rosters["data"]}
        mine = self.my_roster(league, rosters["data"])
        if mine is None:
            return {"data": None, "partial": True, "errors": [{"code": "roster_unresolved"}]}
        rows = {m.get("roster_id"): m for m in matchups["data"]}
        me = rows.get(mine["roster_id"])
        if me is None:
            return {"data": None, "partial": True, "errors": [{"code": "no_matchup_this_week"}]}
        opp = next(
            (
                m
                for m in matchups["data"]
                if m.get("matchup_id") == me.get("matchup_id") and m is not me
            ),
            None,
        )
        slots = [x for x in rules.get("roster_positions") or [] if x != "BN"]

        def side(row):
            roster = by_roster.get(row.get("roster_id")) or {}
            user = owners.get(roster.get("owner_id")) or {}
            actual = row.get("players_points") or {}
            starters = row.get("starters") or []

            def entry(pid, slot=None):
                player = players.get(pid) or {}
                stats = (proj.get(pid) or {}).get("stats") or {}
                return {
                    "slot": slot,
                    "player_id": pid,
                    "name": display_name(player) if player else pid,
                    "position": player.get("position"),
                    "team": player.get("team"),
                    "opponent": (proj.get(pid) or {}).get("opponent"),
                    "injury_status": player.get("injury_status"),
                    "projected": score(stats, scoring) if stats else None,
                    "actual": actual.get(pid),
                }

            lineup = [
                entry(pid, slots[i] if i < len(slots) else None) for i, pid in enumerate(starters)
            ]
            bench = [entry(pid) for pid in row.get("players") or [] if pid not in starters]
            return {
                "roster_id": row.get("roster_id"),
                "display_name": user.get("display_name"),
                "team_name": (user.get("metadata") or {}).get("team_name"),
                "starters": lineup,
                "bench": bench,
                "projected_total": round(sum(x["projected"] or 0 for x in lineup), 2),
                "actual_total": row.get("points"),
            }

        return {
            "league_id": league_id,
            "week": week,
            "scoring": "league scoring applied to Sleeper projections",
            "me": side(me),
            "opponent": side(opp) if opp else None,
            "partial": any(
                x.get("partial") for x in (settings, rosters, users, matchups, projections)
            ),
            "stale": any(x.get("stale") for x in (settings, rosters, users, matchups, projections)),
            "projections_fetched_at": projections.get("fetched_at"),
            "warning": "Projected uses this league's scoring settings. A None projection means no upstream line.",
        }

    async def check_auth(self):
        """Make one authenticated request and report whether the token works."""
        league = next((x for x in self.config.leagues if x.chat_enabled), None)
        if league is None:
            return {"ok": False, "error": "no_chat_enabled_league"}
        marker = cache_mode.set("refresh")
        try:
            result = await self.client.messages(league.league_id)
        finally:
            cache_mode.reset(marker)
        errors = result.get("errors") or []
        return {"ok": not errors, "error": errors[0]["code"] if errors else None}

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
        scoring = None
        if league_id is not None:
            self.config.league(league_id)
            rostered = await self.rostered(league_id)
            if rostered is None:
                return {"data": None, "partial": True, "errors": [{"code": "rosters_unavailable"}]}
            settings = await self.league_data(league_id, "settings")
            scoring = (settings.get("data") or {}).get("scoring_settings")
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
                    "pts_league": score(stats, scoring) if scoring else None,
                }
            )
        if scoring:
            found.sort(key=lambda x: -(x["pts_league"] or 0))
        return {
            **result,
            "data": found[:limit],
            "total": len(found),
            "availability": "unrostered_only" if league_id else None,
            "warning": "pts_league applies this league's scoring settings. The other totals "
            "use Sleeper's standard, half PPR, and PPR formats.",
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

    SECTIONS = (
        "settings",
        "rosters",
        "users",
        "traded_picks",
        "matchups",
        "transactions",
        "stats_and_projections",
        "chat",
    )

    async def context(self, league_id, week=None, sections=None):
        league = self.config.league(league_id)
        if week is not None:
            valid_week(week)
        wanted = set(self.SECTIONS if sections is None else sections) | {"settings", "rosters"}
        unknown = wanted - set(self.SECTIONS)
        if unknown:
            raise ValueError(f"Unknown sections: {sorted(unknown)}")
        state = await self.client.rest("/state/nfl", 60)
        if week is None:
            week = (state.get("data") or {}).get("week")
        resources = [r for r in ("settings", "rosters", "users", "traded_picks") if r in wanted]
        if isinstance(week, int) and 0 <= week <= 25:
            resources.extend(r for r in ("matchups", "transactions") if r in wanted)
        results = await asyncio.gather(*(self.league_data(league_id, r, week) for r in resources))
        sections = dict(zip(resources, results))
        selected = self.my_roster(league, sections["rosters"].get("data") or [])
        settings = sections["settings"].get("data") or {}
        season = settings.get("season")
        if "stats_and_projections" in wanted and selected and season and week is not None:
            ids = selected.get("players") or []
            if ids:
                sections["stats_and_projections"] = await self.client.projections(
                    ids[:100], season, week, settings.get("season_type", "regular")
                )
        if "chat" in wanted and league.chat_enabled:
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
