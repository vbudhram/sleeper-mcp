import asyncio
import json
import time
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime

import httpx

from .config import CacheSettings, token
from .storage import Store, digest

cache_mode = ContextVar("cache_mode", default=None)

REST = "https://api.sleeper.app/v1"
STATS = "https://api.sleeper.com"
GRAPHQL = "https://sleeper.com/graphql"
FIELDS = "attachment author_display_name author_id created edited message_id parent_id parent_type pinned text text_map"


class UpstreamError(Exception):
    def __init__(self, code: str, retryable=False):
        self.code, self.retryable = code, retryable
        super().__init__(code)


def iso(timestamp):
    return datetime.fromtimestamp(timestamp, UTC).isoformat()


class SleeperClient:
    def __init__(self, store: Store, transport=None, policy=None):
        self.store = store
        self.policy = policy or CacheSettings()
        self.http = httpx.AsyncClient(
            timeout=20,
            follow_redirects=False,
            transport=transport,
            headers={"User-Agent": "sleeper-manager-mcp/0.1"},
        )
        self.semaphore = asyncio.Semaphore(3)

    async def close(self):
        await self.http.aclose()

    async def _fetch(self, url, body=None):
        headers = {}
        if body is not None:
            credential = token()
            if not credential:
                raise UpstreamError("auth_required")
            headers = {"Authorization": credential, "X-Sleeper-GraphQL-Op": body["operationName"]}
        for attempt in range(3):
            try:
                async with self.semaphore:
                    response = await self.http.request(
                        "POST" if body else "GET", url, json=body, headers=headers
                    )
            except httpx.TransportError:
                if attempt == 2:
                    raise UpstreamError("network_error", True) from None
                await asyncio.sleep(0.5 * 2**attempt)
                continue
            if response.status_code == 401:
                raise UpstreamError("auth_required")
            if response.status_code == 403:
                raise UpstreamError("access_denied")
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == 2:
                    raise UpstreamError(
                        "rate_limited" if response.status_code == 429 else "upstream_unavailable",
                        True,
                    )
                try:
                    delay = max(0, float(response.headers.get("Retry-After", 2**attempt)))
                except ValueError:
                    raise UpstreamError("rate_limited", True) from None
                if delay > 5:
                    raise UpstreamError("rate_limited", True)
                await asyncio.sleep(delay)
                continue
            if response.status_code != 200:
                raise UpstreamError("not_found" if response.status_code == 404 else "http_error")
            try:
                payload = response.json()
            except ValueError:
                raise UpstreamError("invalid_response") from None
            if body:
                if not isinstance(payload, dict):
                    raise UpstreamError("invalid_response")
                # Do not echo upstream error text. It can contain request details.
                if payload.get("errors"):
                    return {
                        "data": payload.get("data"),
                        "partial": True,
                        "errors": [{"code": "graphql_error"}],
                    }
                return {"data": payload.get("data"), "partial": False, "errors": []}
            return {"data": payload, "partial": False, "errors": []}
        raise UpstreamError("upstream_unavailable", True)

    async def request(self, url, ttl, body=None, max_age=None):
        key = digest([url, body])
        cached = self.store.cached(key)
        mode = cache_mode.get() or self.policy.mode
        age_limit = ttl if max_age is None else min(ttl, max_age)
        if mode == "cache_only":
            if cached:
                result = self._envelope(url, *cached, age_limit, True)
                result["cache_only"] = True
                return result
            return {
                "data": None,
                "source": url,
                "partial": True,
                "stale": True,
                "cache_only": True,
                "errors": [{"code": "cache_miss", "retryable": False}],
            }
        if mode == "refresh":
            age_limit = 0
        if cached and time.time() - cached[1] <= age_limit:
            return self._envelope(url, *cached, age_limit, True)
        lock = "fetch:" + key
        acquired = self.store.acquire(lock)
        if not acquired:
            for _ in range(40):
                await asyncio.sleep(0.25)
                fresh = self.store.cached(key)
                if fresh and (cached is None or fresh[1] > cached[1]):
                    return self._envelope(url, *fresh, age_limit, True)
            return {
                "data": None,
                "errors": [{"code": "request_in_progress", "retryable": True}],
                "partial": True,
                "source": url,
                "stale": True,
            }
        try:
            # A player directory refresh must respect its daily limit.
            if url == REST + "/players/nfl" and cached and time.time() - cached[1] < 86400:
                result = self._envelope(url, *cached, age_limit, True)
                result["refresh_limited"] = True
                return result
            payload = await self._fetch(url, body)
            if not payload["partial"] and payload["data"] is not None:
                self.store.cache(key, payload)
            return self._envelope(url, payload, time.time(), ttl, False)
        except UpstreamError as error:
            if cached and error.code not in {"auth_required", "access_denied"}:
                result = self._envelope(url, *cached, age_limit, True)
                result.update(stale=True, partial=True)
            else:
                result = {"data": None, "source": url, "partial": True, "stale": True}
            result["errors"] = [{"code": error.code, "retryable": error.retryable}]
            return result
        finally:
            self.store.release(lock)

    @staticmethod
    def _envelope(url, payload, fetched, ttl, cached):
        age = max(0, time.time() - fetched)
        return {
            **payload,
            "source": url,
            "fetched_at": iso(fetched),
            "source_updated_at": None,
            "cache_age_seconds": round(age, 3),
            "stale": age > ttl,
            "cached": cached,
        }

    async def rest(self, path, ttl=60, max_age=None):
        if path.startswith("/league/"):
            resource = path.split("/")[3:]
            if not resource or resource[0] in {"users", "drafts", "traded_picks"}:
                ttl = self.policy.league_settings_seconds
            elif resource[0] == "rosters":
                ttl = self.policy.league_rosters_seconds
        return await self.request(REST + path, ttl, max_age=max_age)

    async def messages(self, league_id, before=None):
        # JSON string serialization also escapes GraphQL string literals.
        args = "parent_id: " + json.dumps(league_id)
        if before:
            args += ", before: " + json.dumps(before)
        body = {
            "operationName": "messages",
            "variables": {},
            "query": "query messages { messages(" + args + ") { " + FIELDS + " } }",
        }
        return await self.request(
            GRAPHQL, self.policy.chat_history_seconds if before else self.policy.chat_seconds, body
        )

    async def league_players(self, league_id):
        """Read per-league player settings such as waiver clearance. Needs the token."""
        body = {
            "operationName": "league_players",
            "variables": {},
            "query": "query league_players { league_players(league_id: "
            + json.dumps(league_id)
            + ") { player_id settings } }",
        }
        return await self.request(GRAPHQL, self.policy.league_rosters_seconds, body)

    async def week_projections(self, season, week, position, category):
        """Read public week projections or stats with Sleeper's own point totals."""
        path = f"/{'projections' if category == 'proj' else 'stats'}/nfl/{season}/{week}"
        query = f"?season_type=regular&position={position}&order_by=pts_ppr"
        return await self.request(STATS + path + query, self.policy.projections_seconds)

    async def create_message(self, league_id, text):
        """Post one league chat message. This never reads or writes the cache."""
        client_id = str(uuid.uuid4())
        args = (
            f"parent_id: {json.dumps(league_id)}, client_id: {json.dumps(client_id)}, "
            'parent_type: "league", text: $text'
        )
        body = {
            "operationName": "create_message",
            "variables": {"text": text},
            "query": "mutation create_message($text: String) { create_message("
            + args
            + ") { "
            + FIELDS.replace(" edited", "")
            + " } }",
        }
        try:
            payload = await self._fetch(GRAPHQL, body)
        except UpstreamError as error:
            payload = {
                "data": None,
                "partial": True,
                "errors": [{"code": error.code, "retryable": error.retryable}],
            }
        return {
            **payload,
            "client_id": client_id,
            "source": GRAPHQL,
            "fetched_at": iso(time.time()),
        }

    async def projections(self, player_ids, season, week, season_type):
        ids = sorted(set(player_ids))
        ttl = self.policy.projections_seconds
        mode = cache_mode.get() or self.policy.mode
        keys = {pid: digest(["player_week", season, week, season_type, pid]) for pid in ids}
        stored = {pid: self.store.cached(key) for pid, key in keys.items()}
        missing = [
            pid
            for pid, entry in stored.items()
            if entry is None
            or (mode != "cache_only" and (mode == "refresh" or time.time() - entry[1] > ttl))
        ]
        fetched = None
        if missing:
            fetched = await self._projections_batch(missing, season, week, season_type)
            if fetched.get("data") is None or fetched.get("partial") or fetched.get("stale"):
                return fetched
            records = fetched["data"]
            if not all(isinstance(records.get(category), list) for category in ("stat", "proj")):
                return {**fetched, "partial": True, "errors": [{"code": "invalid_response"}]}
            for pid in missing:
                payload = {
                    "data": {
                        category: [r for r in records[category] if str(r.get("player_id")) == pid]
                        for category in ("stat", "proj")
                    },
                    "partial": False,
                    "errors": [],
                }
                # Preserve the upstream cache age when a batch supplies an individual entry.
                timestamp = datetime.fromisoformat(fetched["fetched_at"]).timestamp()
                self.store.cache(keys[pid], payload, fetched_at=timestamp)
                stored[pid] = self.store.cached(keys[pid])
        data = {
            category: [row for pid in ids for row in stored[pid][0]["data"][category]]
            for category in ("stat", "proj")
        }
        oldest = min(stored[pid][1] for pid in ids)
        result = self._envelope(
            GRAPHQL,
            {"data": data, "partial": False, "errors": []},
            oldest,
            ttl,
            fetched is None or fetched.get("cached", False),
        )
        result["cache_only"] = mode == "cache_only"
        return result

    async def _projections_batch(self, player_ids, season, week, season_type):
        parts = []
        for category in ("stat", "proj"):
            args = (
                f'sport: "nfl", season: {json.dumps(season)}, '
                f'category: "{category}", season_type: {json.dumps(season_type)}, '
                f"week: {week}, player_ids: {json.dumps(sorted(set(player_ids)))}"
            )
            parts.append(
                f"{category}: stats_for_players_in_week({args}) "
                "{ game_id opponent player_id stats team week season }"
            )
        body = {
            "operationName": "get_player_score_and_projections_batch",
            "variables": {},
            "query": "query get_player_score_and_projections_batch { " + " ".join(parts) + " }",
        }
        return await self.request(GRAPHQL, self.policy.projections_seconds, body)
