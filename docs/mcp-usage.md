# Use the Sleeper MCP server

This guide shows how to connect the server to an MCP client and how to use its tools.
The server reads league data. It never changes a roster, lineup, waiver, or trade.

## 1. Get a session token

Chat reads and raw projections need a Sleeper session token. Public league data does not.

1. Sign in at https://sleeper.com in a browser.
2. Open the developer tools and select the Network tab.
3. Filter by `graphql` and open any league page.
4. Select a `graphql` request and read its request headers.
5. Copy the full `Authorization` header value. Do not add a `Bearer` prefix.

The token gives full account access. Keep it out of chat logs, shell history, and git.
Sleeper does not publish a token lifetime. If a tool returns HTTP 401 or 403, capture a new token.

## 2. Configure the client

Claude Code reads `.mcp.json` from the project root. The file is git-ignored. Copy `.mcp.example.json` to start.

```json
{
  "mcpServers": {
    "sleeper": {
      "command": "/path/to/sleeper-mcp/.venv/bin/sleeper-mcp",
      "env": {
        "SLEEPER_CONFIG": "/path/to/sleeper-mcp/config.json",
        "SLEEPER_STATE_DIR": "/path/to/sleeper-mcp/.state",
        "SLEEPER_SESSION_TOKEN": "REPLACE_WITH_YOUR_SESSION_TOKEN"
      }
    }
  }
}
```

Replace the placeholder with the token. Restart the client, then approve the server when asked.
To keep the token out of the file, set the value to `"${SLEEPER_SESSION_TOKEN}"` and export the variable in the shell that starts the client.

Other MCP clients use the same `command` and `env` fields. Run `uv sync --frozen` first so the `.venv` command exists.

## 3. Check the connection

Ask the client to call `get_data_health`. It reports the configured leagues and limits without a network request.
Ask for `get_cache_status` to see local cache counts and policy.
Ask for `get_league_chat` with a league ID. A result with `errors: []` confirms the token works.

## 4. Tools

League IDs come from `config.json`. Invalid IDs fail before any request.

| Tool | Arguments | Use |
| --- | --- | --- |
| `discover_leagues` | `season` | Find leagues for the configured user |
| `get_nfl_state` | none | Current season and week |
| `get_league_data` | `league_id`, `resource`, `week` | Settings, rosters, users, matchups, transactions, traded picks, drafts, brackets. Matchups and transactions need a week |
| `get_draft_data` | `league_id`, `draft_id` | A verified league draft and its picks |
| `search_players` | query fields | Daily player directory |
| `get_unrostered_players` | `league_id` and filters | Players absent from league rosters. A player is on waivers while `waiver_clears_at` is in the future |
| `get_week_projections` | `season`, `week`, `position`, `category`, `limit`, `league_id` | Public projections or stats with `pts_ppr`, `pts_half_ppr`, `pts_std`. A league ID keeps only unrostered players |
| `get_trending_players` | type and window | Sleeper add and drop counts |
| `get_player_week_stats_and_projections` | player IDs, `season`, `week` | Raw statistics and projections, no league scoring |
| `get_league_chat` | `league_id`, `before`, `limit` | One chat page. Needs the token |
| `search_league_chat` | `league_id`, `query`, `limit` | Search of locally stored chat only |
| `get_manager_context` | `league_id`, `week`, `mode` | League facts, owned roster, projections, chat |
| `get_all_leagues_summary` | `week` | A context for every configured league |
| `run_manager_review` | `league_id`, `week` | Context plus a stored public-data snapshot |
| `get_changes_since` | `league_id`, `snapshot_id` | Differences between a snapshot and the latest one |
| `get_data_health` | none | Configuration and limits |
| `get_cache_status` | none | Local cache counts and policy |
| `post_league_chat` | `league_id`, `text` | Send one chat message. Needs the token and explicit user authorization |
| `prepare_chat_post` | `league_id`, `text`, `request_id` | Store an exact message for a browser agent to send |
| `claim_chat_post` | `request_id` | Permit one browser submission |
| `get_chat_post_status` | `request_id` | Look for the sent text in fresh chat |

## 5. Example prompts

- "Pull the latest chat for both leagues."
- "Show my roster and this week's matchup in League of Extraordinary Gentlemen."
- "Which unrostered running backs are trending up?"
- "Show the top open wide receivers by week 2 PPR projection in Whiskey Dicks 2."
- "Run a manager review for week 2 and tell me what changed since the last snapshot."
- "Search the league chat for trade offers."

## 6. Cache modes

`get_manager_context` accepts a `mode` argument for one call.

| Mode | Behavior |
| --- | --- |
| `normal` | Reuse fresh data, refresh expired entries |
| `cache_only` | No network request. Missing data returns `cache_miss` |
| `refresh` | Request fresh data. The daily player-directory limit still applies |

Every response includes fetch time, cache age, and a stale flag. Check them before advice.
The chat cache period is 5 minutes. A second chat request inside that window returns the stored page.

## 7. Chat posts

`post_league_chat` sends one message as the configured user. Confirm the exact text and league with the user first.
The result has `status: confirmed` and a `message_id` on success. An `unknown` status needs a check with `get_chat_post_status` before any retry.
`prepare_chat_post` and `claim_chat_post` remain for a browser-capable agent without the token.

## 8. Test without a client

This script starts the server over stdio and calls one tool:

```python
import asyncio, json
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

cfg = json.load(open(".mcp.json"))["mcpServers"]["sleeper"]


async def main():
    params = StdioServerParameters(command=cfg["command"], env=cfg["env"])
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool("get_league_chat", {"league_id": "<league_id>", "limit": 10})
            print(res.content[0].text)


asyncio.run(main())
```

Run it with `.venv/bin/python script.py` from the project root.
