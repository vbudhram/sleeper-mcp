import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from sleeper_mcp.client import SleeperClient
from sleeper_mcp.config import Config
from sleeper_mcp.service import Manager
from sleeper_mcp.storage import Store


@pytest.fixture
def store(tmp_path):
    instance = Store(str(tmp_path / "state"))
    yield instance
    instance.db.close()


@pytest.fixture
def config():
    return Config(
        user_id="1",
        leagues=[
            {"league_id": "11", "chat_enabled": True},
            {"league_id": "22", "chat_enabled": True},
        ],
    )


async def test_rest_never_sends_token(store, monkeypatch):
    monkeypatch.setenv("SLEEPER_SESSION_TOKEN", "test-secret")

    def respond(request):
        assert request.method == "GET"
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"week": 1})

    client = SleeperClient(store, httpx.MockTransport(respond))
    result = await client.rest("/state/nfl")
    assert result["data"]["week"] == 1
    assert not result["stale"]
    await client.close()


async def test_graphql_allowlisted_query_and_token(store, monkeypatch):
    monkeypatch.setenv("SLEEPER_SESSION_TOKEN", "test-secret")

    def respond(request):
        assert request.url.host == "sleeper.com"
        assert request.headers["authorization"] == "test-secret"
        body = json.loads(request.content)
        assert body["query"].startswith("query get_player_score_and_projections_batch")
        assert "mutation" not in body["query"]
        assert '"NE"' in body["query"]
        return httpx.Response(200, json={"data": {"stat": [], "proj": []}})

    client = SleeperClient(store, httpx.MockTransport(respond))
    result = await client.projections(["NE"], "2026", 1, "regular")
    assert result["data"] == {"stat": [], "proj": []}
    assert "test-secret" not in json.dumps(result)
    await client.close()


async def test_no_token_does_not_make_request(store, monkeypatch):
    monkeypatch.delenv("SLEEPER_SESSION_TOKEN", raising=False)

    def respond(request):
        pytest.fail("No network request is permitted")

    client = SleeperClient(store, httpx.MockTransport(respond))
    result = await client.messages("11")
    assert result["errors"][0]["code"] == "auth_required"
    await client.close()


@pytest.mark.parametrize("status,code", [(401, "auth_required"), (403, "access_denied")])
async def test_access_failures_do_not_retry(store, monkeypatch, status, code):
    monkeypatch.setenv("SLEEPER_SESSION_TOKEN", "test-secret")
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(status, json={"secret": "must not appear"})

    client = SleeperClient(store, httpx.MockTransport(respond))
    result = await client.messages("11")
    assert len(calls) == 1
    assert result["errors"][0]["code"] == code
    assert "must not appear" not in json.dumps(result)
    await client.close()


async def test_graphql_partial_errors_are_not_cached(store, monkeypatch):
    monkeypatch.setenv("SLEEPER_SESSION_TOKEN", "test-secret")
    client = SleeperClient(
        store,
        httpx.MockTransport(
            lambda r: httpx.Response(
                200, json={"data": {"stat": []}, "errors": [{"message": "secret"}]}
            )
        ),
    )
    result = await client.projections(["NE"], "2026", 1, "regular")
    assert result["partial"]
    assert "secret" not in json.dumps(result)
    assert store.db.execute("SELECT COUNT(*) FROM cache").fetchone()[0] == 0
    await client.close()


async def test_stale_fallback(store):
    client = SleeperClient(store, httpx.MockTransport(lambda r: httpx.Response(200, json=[])))
    await client.rest("/state/nfl")
    store.db.execute("UPDATE cache SET fetched=?", (time.time() - 100,))
    store.db.commit()
    client.http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(429, headers={"Retry-After": "999"}))
    )
    result = await client.rest("/state/nfl")
    assert result["stale"] and result["partial"]
    assert result["errors"][0]["code"] == "rate_limited"
    await client.close()


async def test_player_daily_limit(store):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={})

    client = SleeperClient(store, httpx.MockTransport(respond))
    await client.rest("/players/nfl", 86400)
    result = await client.rest("/players/nfl", 86400, max_age=0)
    assert result["refresh_limited"]
    assert len(calls) == 1
    await client.close()


async def test_concurrent_requests_share_cache(store):
    calls = []

    async def respond(request):
        calls.append(request)
        await asyncio.sleep(0.01)
        return httpx.Response(200, json={"week": 1})

    client = SleeperClient(store, httpx.MockTransport(respond))
    results = await asyncio.gather(client.rest("/state/nfl"), client.rest("/state/nfl"))
    assert len(calls) == 1
    assert results[0]["data"] == results[1]["data"]
    await client.close()


async def test_league_and_chat_isolation(store, config, monkeypatch):
    monkeypatch.setenv("SLEEPER_SESSION_TOKEN", "test-secret")
    client = SleeperClient(
        store,
        httpx.MockTransport(
            lambda r: httpx.Response(
                200,
                json={
                    "data": {
                        "messages": [
                            {"message_id": "7", "parent_id": "11", "text": "Trade"},
                            {"message_id": "8", "parent_id": "22", "text": "Private"},
                        ]
                    }
                },
            )
        ),
    )
    manager = Manager(config, store, client)
    with pytest.raises(ValueError):
        await manager.league_data("33", "rosters")
    result = await manager.chat("11")
    assert len(result["data"]) == 1
    assert result["partial"]
    assert store.search_messages("22", "", 50) == []
    assert len(store.search_messages("11", "trade", 50)) == 1
    await client.close()


def test_snapshot_scope_and_stability(store):
    first = store.snapshot("11", {"rosters": [1]})
    same = store.snapshot("11", {"rosters": [1]})
    assert not same["changed"]
    store.snapshot("22", {"rosters": [2]})
    with pytest.raises(ValueError):
        store.changes("22", first["snapshot_id"])
    store.snapshot("11", {"rosters": [3]})
    assert store.changes("11", first["snapshot_id"])["changes"]["rosters"]["after"] == [3]


def test_config_rejects_duplicate_leagues():
    with pytest.raises(ValidationError):
        Config(user_id="1", leagues=[{"league_id": "11"}, {"league_id": "11"}])


async def test_mcp_tool_discovery_and_no_arbitrary_execution():
    from sleeper_mcp.server import mcp

    tools = await mcp.list_tools()
    names = {tool.name for tool in tools}
    assert "get_league_chat" in names
    assert "run_manager_review" in names
    assert "post_league_chat" in names
    assert (
        not {
            "execute_graphql",
            "fetch_url",
            "set_lineup",
            "prepare_league_message",
        }
        & names
    )


async def test_partial_optional_data_keeps_public_baseline(store, config):
    manager = Manager(config, store, None)

    async def context(league_id, week):
        return {
            "league_id": league_id,
            "sections": {
                "rosters": {"data": [{"roster_id": 1}], "partial": False, "stale": False},
                "chat": {"data": None, "partial": True, "errors": [{"code": "auth_required"}]},
            },
        }

    manager.context = context
    first = await manager.review("11")
    second = await manager.review("11")
    assert first["snapshot"]["changed"]
    assert first["status"] == "partial"
    assert first["new_failure"]
    assert not second["new_failure"]
    assert not second["notify"]
    assert second["requires_attention"]


async def test_stdio_session(tmp_path):
    import os
    import sys

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"user_id": "1", "leagues": [{"league_id": "11"}]}))
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "sleeper_mcp"],
        env={
            **os.environ,
            "SLEEPER_CONFIG": str(config_file),
            "SLEEPER_STATE_DIR": str(tmp_path / "state"),
            "SLEEPER_SESSION_TOKEN": "",
        },
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        listed = await session.list_tools()
        assert len(listed.tools) >= 15
        health = await session.call_tool("get_data_health", {})
        assert not health.isError
        chat = await session.call_tool("search_league_chat", {"league_id": "11", "query": "test"})
        assert not chat.isError


async def test_cache_only_persists_across_clients(store):
    from sleeper_mcp.config import CacheSettings

    first = SleeperClient(
        store, httpx.MockTransport(lambda r: httpx.Response(200, json={"week": 1}))
    )
    await first.rest("/state/nfl")
    await first.close()
    store.db.execute("UPDATE cache SET fetched=?", (time.time() - 500,))
    store.db.commit()

    def no_network(request):
        pytest.fail("Cache-only mode must not use the network")

    second = SleeperClient(store, httpx.MockTransport(no_network), CacheSettings(mode="cache_only"))
    result = await second.rest("/state/nfl")
    assert result["data"] == {"week": 1}
    assert result["cached"] and result["stale"]
    missing = await second.rest("/missing")
    assert missing["errors"][0]["code"] == "cache_miss"
    await second.close()


async def test_projection_subsets_share_cache(store, monkeypatch):
    monkeypatch.setenv("SLEEPER_SESSION_TOKEN", "test-secret")
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "data": {
                    "stat": [],
                    "proj": [
                        {"player_id": "NE", "stats": {"pts": 3}},
                        {"player_id": "TEN", "stats": {"pts": 5}},
                    ],
                }
            },
        )

    client = SleeperClient(store, httpx.MockTransport(respond))
    await client.projections(["NE", "TEN"], "2026", 1, "regular")
    result = await client.projections(["TEN"], "2026", 1, "regular")
    assert len(calls) == 1
    assert [p["player_id"] for p in result["data"]["proj"]] == ["TEN"]
    assert result["cached"]
    await client.close()


def test_history_preserves_versions_and_deduplicates_snapshots(store):
    store.cache("key", {"value": 1})
    store.cache("key", {"value": 1})
    store.cache("key", {"value": 2})
    assert store.cache_info()["response_versions"] == 2
    first = store.snapshot("11", {"rosters": []})
    second = store.snapshot("11", {"rosters": []})
    assert first["snapshot_id"] == second["snapshot_id"]
    assert store.cache_info()["snapshots"] == 1


async def test_refresh_overrides_fresh_cache(store):
    from sleeper_mcp.client import cache_mode

    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"week": len(calls)})

    client = SleeperClient(store, httpx.MockTransport(respond))
    await client.rest("/state/nfl")
    selected = cache_mode.set("refresh")
    try:
        result = await client.rest("/state/nfl")
    finally:
        cache_mode.reset(selected)
    assert result["data"]["week"] == 2
    await client.close()


async def test_browser_post_claim_and_verification(store, config):
    from sleeper_mcp import posting

    manager = Manager(config, store, None)
    prepared = posting.prepare(manager, "11", "👀", "eyes-1")
    assert not prepared["sent_by_this_tool"]
    with pytest.raises(ValueError):
        posting.prepare(manager, "22", "👀", "eyes-1")
    assert posting.claim(manager, "eyes-1")["may_submit_once"]
    assert not posting.claim(manager, "eyes-1")["may_submit_once"]

    async def chat(league_id, limit):
        return {
            "data": [
                {"message_id": "99", "author_id": "1", "text": "👀", "created": time.time() * 1000}
            ],
            "stale": False,
            "partial": False,
        }

    manager.chat = chat
    assert (await posting.verify(manager, "eyes-1"))["status"] == "confirmed"


async def test_unrostered_players_merge_waiver_clears(store, config, monkeypatch):
    monkeypatch.setenv("SLEEPER_SESSION_TOKEN", "test-secret")

    def respond(request):
        if request.url.host == "sleeper.com":
            assert json.loads(request.content)["query"].startswith("query league_players")
            return httpx.Response(
                200,
                json={
                    "data": {
                        "league_players": [
                            {"player_id": "2", "settings": {"waiver_clears_at": 1789145571}},
                            {"player_id": "3", "settings": None},
                        ]
                    }
                },
            )
        if request.url.path.endswith("/rosters"):
            return httpx.Response(200, json=[{"players": ["1"]}])
        return httpx.Response(
            200,
            json={
                "1": {"first_name": "A", "last_name": "One", "fantasy_positions": ["RB"]},
                "2": {"first_name": "B", "last_name": "Two", "fantasy_positions": ["RB"]},
                "3": {"first_name": "C", "last_name": "Three", "fantasy_positions": ["RB"]},
            },
        )

    manager = Manager(config, store, SleeperClient(store, httpx.MockTransport(respond)))
    result = await manager.players("", "RB", 25, 0, "11")
    assert [p["player_id"] for p in result["data"]] == ["2", "3"]
    assert result["data"][0]["waiver_clears_at"] == 1789145571
    assert result["data"][1]["waiver_clears_at"] is None
    assert result["waiver_status"] is None
    await manager.client.close()


async def test_rosters_and_matchups_include_player_names(store, config):
    def respond(request):
        if request.url.path.endswith("/rosters"):
            return httpx.Response(200, json=[{"players": ["1", "NE"], "starters": ["1"]}])
        if "/matchups/" in request.url.path:
            return httpx.Response(200, json=[{"starters": ["1", "0"], "players": ["1", "9"]}])
        return httpx.Response(
            200,
            json={
                "1": {"first_name": "A", "last_name": "One", "position": "RB", "team": "SF"},
                "NE": {"first_name": "New England", "last_name": "Patriots", "position": "DEF"},
            },
        )

    manager = Manager(config, store, SleeperClient(store, httpx.MockTransport(respond)))
    rosters = await manager.league_data("11", "rosters", names=True)
    assert rosters["player_names"] == {"1": "A One RB-SF", "NE": "New England Patriots DEF-FA"}
    matchups = await manager.league_data("11", "matchups", 1, names=True)
    assert matchups["player_names"] == {"1": "A One RB-SF"}
    assert "player_names" not in await manager.league_data("11", "users", names=True)
    assert "player_names" not in await manager.league_data("11", "rosters")
    await manager.client.close()


async def test_week_projections_filter_rostered_and_keep_points(store, config):
    def respond(request):
        assert "authorization" not in request.headers
        if request.url.path.endswith("/rosters"):
            return httpx.Response(200, json=[{"players": ["1"]}])
        if request.url.path == "/v1/league/11":
            return httpx.Response(200, json={"scoring_settings": {"rec": 0.5, "rec_yd": 0.1}})
        assert request.url.host == "api.sleeper.com"
        assert request.url.path == "/projections/nfl/2026/2"
        assert request.url.params["position"] == "RB"
        return httpx.Response(
            200,
            json=[
                {"player_id": "1", "stats": {"pts_ppr": 20.0}, "player": {"last_name": "Taken"}},
                {
                    "player_id": "2",
                    "team": "SF",
                    "opponent": "SEA",
                    "stats": {
                        "pts_ppr": 12.5,
                        "pts_half_ppr": 11.0,
                        "pts_std": 9.5,
                        "rec": 4,
                        "rec_yd": 50,
                    },
                    "player": {"first_name": "Kaelon", "last_name": "Black"},
                },
            ],
        )

    manager = Manager(config, store, SleeperClient(store, httpx.MockTransport(respond)))
    result = await manager.projections("2026", 2, "RB", "proj", 50, "11")
    assert len(result["data"]) == 1
    assert result["data"][0]["name"] == "Kaelon Black"
    assert result["data"][0]["pts_ppr"] == 12.5
    assert result["data"][0]["pts_league"] == 7.0
    with pytest.raises(ValueError):
        await manager.projections("26", 2, "RB", "proj", 50)
    await manager.client.close()


async def test_direct_post_sends_mutation_and_records_result(store, config, monkeypatch):
    from sleeper_mcp import posting

    monkeypatch.setenv("SLEEPER_SESSION_TOKEN", "test-secret")
    seen = {}

    def respond(request):
        body = json.loads(request.content)
        seen["body"] = body
        assert request.headers["authorization"] == "test-secret"
        assert request.headers["x-sleeper-graphql-op"] == "create_message"
        assert body["query"].startswith("mutation create_message($text: String)")
        assert 'parent_id: "11"' in body["query"]
        return httpx.Response(
            200,
            json={
                "data": {"create_message": {"message_id": "77", "parent_id": "11", "text": "hi"}}
            },
        )

    manager = Manager(config, store, SleeperClient(store, httpx.MockTransport(respond)))
    result = await posting.send(manager, "11", "hi")
    assert seen["body"]["variables"] == {"text": "hi"}
    assert result["status"] == "confirmed" and result["message_id"] == "77"
    assert result["request_id"] in seen["body"]["query"]
    assert store.db.execute("SELECT COUNT(*) FROM cache").fetchone() == (0,)
    assert store.db.execute("SELECT status, message_id FROM chat_posts").fetchone() == (
        "confirmed",
        "77",
    )
    with pytest.raises(ValueError):
        await posting.send(manager, "11", " ")
    await manager.client.close()


FIXTURE = json.loads(Path(__file__).with_name("fixtures").joinpath("league.json").read_text())


@pytest.fixture
async def fixture_manager(store):
    """A manager backed by recorded responses for one real league and one matchup."""

    def respond(request):
        path = request.url.path
        if path == "/v1/state/nfl":
            return httpx.Response(200, json=FIXTURE["state"])
        if path == "/v1/league/11":
            return httpx.Response(200, json=FIXTURE["settings"])
        if path.endswith("/rosters"):
            return httpx.Response(200, json=FIXTURE["rosters"])
        if path.endswith("/users"):
            return httpx.Response(200, json=FIXTURE["users"])
        if "/matchups/" in path:
            return httpx.Response(200, json=FIXTURE["matchups"])
        if path == "/v1/players/nfl":
            return httpx.Response(200, json=FIXTURE["players"])
        if path.startswith("/projections/"):
            return httpx.Response(200, json=FIXTURE["projections"])
        if path.startswith("/schedule/"):
            return httpx.Response(200, json=FIXTURE["schedule"])
        if path.startswith("/v1/players/nfl/trending/"):
            return httpx.Response(200, json=[{"player_id": "4034", "count": 5}])
        pytest.fail(f"Unexpected request {request.url}")

    owner = FIXTURE["rosters"][0]["owner_id"]
    config = Config(user_id=owner, leagues=[{"league_id": "11", "chat_enabled": True}])
    manager = Manager(config, store, SleeperClient(store, httpx.MockTransport(respond)))
    yield manager
    await manager.client.close()


def test_score_applies_league_weights():
    from sleeper_mcp.service import score

    scoring = FIXTURE["settings"]["scoring_settings"]
    assert scoring["pass_td"] == 6.0
    assert score({"pass_td": 2, "pass_yd": 100, "unknown": 9}, scoring) == 16.0
    assert score(None, scoring) == 0.0


async def test_matchup_view_joins_names_and_league_scoring(fixture_manager):
    result = await fixture_manager.matchup("11")
    assert result["week"] == 2
    assert result["me"]["roster_id"] == 1
    assert result["me"]["team_name"] == "Buck Nasty"
    assert result["opponent"]["roster_id"] == 3
    first = result["me"]["starters"][0]
    assert first["slot"] == "QB"
    assert first["name"].endswith("QB-LAR")
    assert first["actual"] == 35.98
    assert isinstance(first["projected"], float)
    assert result["me"]["actual_total"] == 211.18
    assert result["me"]["projected_total"] > 0
    assert len(result["me"]["bench"]) == 5
    assert not result["partial"]


async def test_standings_sorted_with_names(fixture_manager):
    result = await fixture_manager.standings("11")
    rows = result["data"]
    assert [r["rank"] for r in rows] == [1, 2]
    assert rows[0]["team_name"] == "Buck Nasty"
    assert rows[0]["wins"] == 2 and rows[1]["wins"] == 0
    assert rows[0]["points_for"] == 361.52


async def test_schedule_derives_bye_teams(store):
    season = [
        {"week": 1, "home": "A", "away": "B", "date": "2026-09-13", "status": "complete"},
        {"week": 1, "home": "C", "away": "D", "date": "2026-09-13", "status": "complete"},
        {"week": 2, "home": "A", "away": "C", "date": "2026-09-20", "status": "pending"},
    ]
    client = SleeperClient(store, httpx.MockTransport(lambda r: httpx.Response(200, json=season)))
    manager = Manager(Config(user_id="1"), store, client)
    result = await manager.schedule("2026", 2)
    assert [g["home"] for g in result["data"]] == ["A"]
    assert result["bye_teams"] == ["B", "D"]
    await client.close()


async def test_trending_rows_carry_names(fixture_manager):
    result = await fixture_manager.trending("add", 24, 25)
    row = result["data"][0]
    assert row["name"] == "Christian McCaffrey"
    assert row["position"] == "RB" and row["team"] == "SF"


async def test_resolve_players_skips_unknown(fixture_manager):
    names = await fixture_manager.resolve(["4034", "nope"])
    assert names == {"4034": "Christian McCaffrey RB-SF"}


async def test_context_sections_limit_fetches(fixture_manager):
    result = await fixture_manager.context("11", 2, sections=["matchups"])
    assert set(result["sections"]) == {"settings", "rosters", "matchups"}
    assert result["my_roster"]["roster_id"] == 1
    with pytest.raises(ValueError):
        await fixture_manager.context("11", 2, sections=["nope"])


async def test_check_auth_reports_expired_token(store, config, monkeypatch):
    monkeypatch.setenv("SLEEPER_SESSION_TOKEN", "old")
    client = SleeperClient(store, httpx.MockTransport(lambda r: httpx.Response(401)))
    result = await Manager(config, store, client).check_auth()
    assert result == {"ok": False, "error": "auth_required"}
    await client.close()


async def test_summary_error_includes_detail(store, config, monkeypatch):
    from sleeper_mcp import server

    class Broken:
        pass

    Broken.config = config

    async def context(self, league_id, week=None):
        raise KeyError("season")

    Broken.context = context
    monkeypatch.setattr(server, "manager", lambda: Broken())
    result = await server.get_all_leagues_summary()
    assert result["leagues"][0]["detail"] == "KeyError: 'season'"
