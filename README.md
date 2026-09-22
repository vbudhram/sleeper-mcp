# Sleeper MCP

An MCP server that gives AI agents access to Sleeper fantasy football leagues.
It reads league settings, rosters, matchups, transactions, projections, waivers, trends, and league chat.
It can post one league chat message after explicit user authorization.
It never changes a roster, lineup, waiver, or trade.

## Why not call the public API directly?

For raw league data, an agent with `curl` can use the public Sleeper REST API. This server adds what the public API lacks.

**Not available from the public API**

- League chat. Reads and posts need Sleeper's GraphQL endpoint and a session token. The server uses fixed query shapes, so the agent never builds a GraphQL document or handles the token.
- Waiver clear times. Whether an unrostered player is on waivers or a free agent comes from a token-only query.
- Projection batches for the players on your roster.

**Awkward through the public API**

- The player directory is a 14 MB JSON blob with no search. The server caches it once a day and adds name, position, and unrostered-in-this-league filters.
- Week projections arrive as raw stat lines. The server returns point totals with names, opponent, and injury status, and applies your league's scoring settings.
- Matchups and rosters use player IDs only. The server attaches a `player_names` map to both, and `get_matchup` returns both lineups fully resolved with league-scored projections.

**Built in, so the agent does not have to build it**

- A SQLite cache with per-resource freshness. Repeated questions do not re-hit Sleeper, and stale data is flagged.
- Snapshots and diffs, so `get_changes_since` reports what changed in a league since the last review.
- A searchable local chat archive across pages. The upstream returns one page at a time.
- Ownership resolution, so "my roster" works without roster IDs.

**Safety boundaries**

- The token never reaches public endpoints. No tool accepts a URL or an arbitrary query. The only mutation is one chat post that requires explicit user authorization.
- Every response carries fetch time, cache age, and a stale flag.

If you only want standings and scores, the public API is enough. Use this server for chat, waivers, projections with points, and repeat use.

## Agent instructions

If you are an AI agent, follow these steps in order. Steps 1 to 3 and 5 to 6 need no human input.
Step 4 needs the human to copy a value from their browser.

### 1. Check prerequisites

Confirm `uv` is installed. If it is not, install it:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
```

The server needs Python 3.11 or later. `uv` downloads one if needed.

### 2. Find the user's Sleeper user ID

Ask the human for their Sleeper username, then run:

```sh
curl -s https://api.sleeper.app/v1/user/<username>
```

Read `user_id` from the response. It is a long numeric string.

### 3. Create the config file

Pick a directory that the server can write to, such as `~/.sleeper-mcp`.
Create it and write `config.json` there:

```sh
mkdir -p ~/.sleeper-mcp
```

```json
{
  "user_id": "<user_id from step 2>",
  "timezone": "America/New_York",
  "leagues": []
}
```

Then list the user's leagues for the current season:

```sh
SLEEPER_CONFIG=~/.sleeper-mcp/config.json \
uvx --from git+https://github.com/vbudhram/sleeper-mcp sleeper-manager discover --season 2026
```

Add one object to `leagues` for each league the human wants:

```json
{
  "user_id": "<user_id>",
  "timezone": "America/New_York",
  "leagues": [
    { "league_id": "<league_id>", "label": "<league name>", "chat_enabled": true }
  ]
}
```

Set `timezone` to the human's IANA timezone. `chat_enabled` controls chat reads and posts for that league.

### 4. Get a session token from the human

Public league data works without a token. League chat, waiver clear times, and per-player projection batches need one.
The token is a browser session credential. Ask the human to do this:

1. Sign in at https://sleeper.com in a browser.
2. Open the developer tools and select the Network tab.
3. Open any league page and filter requests by `graphql`.
4. Select a `graphql` request and read its request headers.
5. Copy the full `Authorization` header value.

Do not add a `Bearer` prefix. Do not paste the token into chat logs, shell history, or git.
Ask the human to export it in the shell that starts the MCP client:

```sh
export SLEEPER_SESSION_TOKEN="<value>"
```

Sleeper does not publish a token lifetime. If a tool returns `auth_required`, ask for a new token.

### 5. Register the server with the MCP client

The server runs over stdio. Every client needs the same three things:

| Item | Value |
| --- | --- |
| command | `uvx` |
| args | `--from git+https://github.com/vbudhram/sleeper-mcp sleeper-mcp` |
| env | `SLEEPER_CONFIG`, `SLEEPER_STATE_DIR`, `SLEEPER_SESSION_TOKEN` |

Use absolute paths in `env`. `SLEEPER_STATE_DIR` is where the SQLite cache lives.

**Claude Code**, from the shell:

```sh
claude mcp add sleeper \
  -e SLEEPER_CONFIG=$HOME/.sleeper-mcp/config.json \
  -e SLEEPER_STATE_DIR=$HOME/.sleeper-mcp/state \
  -e SLEEPER_SESSION_TOKEN=$SLEEPER_SESSION_TOKEN \
  -- uvx --from git+https://github.com/vbudhram/sleeper-mcp sleeper-mcp
```

**Claude Desktop, Cursor, or any JSON-configured client**:

```json
{
  "mcpServers": {
    "sleeper": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/vbudhram/sleeper-mcp", "sleeper-mcp"],
      "env": {
        "SLEEPER_CONFIG": "/Users/<name>/.sleeper-mcp/config.json",
        "SLEEPER_STATE_DIR": "/Users/<name>/.sleeper-mcp/state",
        "SLEEPER_SESSION_TOKEN": "<value>"
      }
    }
  }
}
```

Claude Desktop reads `claude_desktop_config.json`. Cursor reads `.cursor/mcp.json`. Claude Code also reads `.mcp.json` in a project root.
Restart the client after the change. Approve the server when the client asks.

### 6. Verify

Call these tools in order:

1. `get_data_health`. It returns the configured leagues without a network request. `session_token_configured` shows whether the token reached the server.
2. `get_nfl_state`. It confirms public API access.
3. `check_auth`. It makes one authenticated request. When `ok` is false, ask the human for a new token.

Then read `docs/manager-prompt.md` for how to act as a fantasy manager with these tools.

## Tools

| Tool | Result |
| --- | --- |
| `discover_leagues` | Leagues for the configured user and a season |
| `get_nfl_state` | Current NFL season and week |
| `get_matchup` | My lineup and my opponent's lineup for a week: names, injuries, league-scored projections, actual points, and totals |
| `get_standings` | Standings sorted by wins then points, with team and manager names |
| `get_schedule` | NFL games for a week with date and status, plus the teams on bye |
| `get_league_data` | Settings, rosters, users, matchups, transactions, traded picks, drafts, or brackets. Rosters and matchups include a `player_names` map |
| `resolve_players` | Map player IDs to `Name POS-TEAM` |
| `get_draft_data` | A verified league draft and its picks |
| `search_players` | Player directory search |
| `get_unrostered_players` | Players absent from a league's rosters, with waiver clear times |
| `get_week_projections` | Public week projections or stats with Sleeper point totals, plus `pts_league` when a league ID is given |
| `get_trending_players` | Sleeper add and drop counts with names and positions |
| `get_player_week_stats_and_projections` | Raw actual and projected statistics for given players |
| `get_league_chat` | One chat page |
| `search_league_chat` | Search of locally collected chat |
| `get_manager_context` | League facts, owned roster, projections, and chat. Pass `sections` to limit the output |
| `get_all_leagues_summary` | A context for every configured league |
| `run_manager_review` | Context plus a stored public-data snapshot |
| `get_changes_since` | Changes between a snapshot and the latest one |
| `get_data_health` | Configuration and capability limits |
| `check_auth` | One authenticated request to test the session token |
| `get_cache_status` | Local cache counts and policy |
| `post_league_chat` | Send one chat message. Needs the token and explicit user authorization |
| `prepare_chat_post`, `claim_chat_post`, `get_chat_post_status` | Browser-assisted posting for agents without the token |

See `docs/mcp-usage.md` for arguments and examples.

Invalid league IDs fail before any request. Public requests never receive the session token.
No tool accepts a URL or an arbitrary GraphQL document. The only mutation is the fixed `create_message` post.

## Data freshness

| Data | Default cache period | Limit |
| --- | --- | --- |
| Player directory | 24 hours | No more than one full refresh per day |
| League rules, users, drafts | 1 hour | Rules can change between calls |
| Rosters, matchups, transactions, NFL state | 1 minute | No atomic snapshot across endpoints |
| Trends | 5 minutes | Popularity is not a forecast |
| Chat | 5 minutes | Pagination and historical completeness remain unverified |
| Statistics and projections | 5 minutes | Field meanings require live validation |

Responses include source URLs, fetch time, cache age, a stale flag, and structured errors.
HTTP 401 and 403 never return cached authenticated content as current data.

Optional cache settings in `config.json`:

```json
"cache": {
  "mode": "normal",
  "league_settings_seconds": 3600,
  "league_rosters_seconds": 60,
  "chat_seconds": 300,
  "chat_history_seconds": 86400,
  "projections_seconds": 300
}
```

`normal` reuses fresh data and refreshes expired entries. `cache_only` never makes a network request. `refresh` requests fresh data.
`get_manager_context` accepts a `mode` argument to override this for one call.

## Known limits

- `get_matchup` and `get_week_projections` with a league ID apply the league's scoring settings to Sleeper's projected stat lines. Other projection tools return Sleeper's stock totals.
- Without the token, `waiver_status` is `unknown`.
- `get_schedule` gives game dates and bye teams. Sleeper does not publish kickoff times through this endpoint.
- No inactive-list or news provider is connected.
- Chat search covers stored messages only, not complete upstream history.
- There is no automatic token renewal.

## Command line

The package also installs `sleeper-manager` for scheduled, non-MCP use:

```sh
uvx --from git+https://github.com/vbudhram/sleeper-mcp sleeper-manager discover --season 2026
uvx --from git+https://github.com/vbudhram/sleeper-mcp sleeper-manager review --week 3
uvx --from git+https://github.com/vbudhram/sleeper-mcp sleeper-manager health
uvx --from git+https://github.com/vbudhram/sleeper-mcp sleeper-manager prune --days 30
uvx --from git+https://github.com/vbudhram/sleeper-mcp sleeper-manager with-token
```

`review` prints facts as JSON, not advice. `with-token` reads the token from hidden input and starts the stdio server once.

## Development

```sh
git clone git@github.com:vbudhram/sleeper-mcp.git
cd sleeper-mcp
uv sync --frozen
uv run pytest -q
uv run ruff check src tests
```

To run the local checkout as the MCP server, copy `.mcp.example.json` to `.mcp.json` and set the paths.
`config.json`, `.mcp.json`, `.state/`, and `.env` are git-ignored.

## License

MIT. See `LICENSE`.
