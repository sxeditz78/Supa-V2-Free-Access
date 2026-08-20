# RNDAccess Bot

A Telegram bot (aiogram 3) that mirrors videos from a private channel to users on a **free-preview → verify → timed-access** model, with auto-indexing, drip-push of new content, and admin controls. Backed by PostgreSQL (asyncpg) — e.g. Supabase — and deployable on Railway.

## How it works

- New videos posted in `CHANNEL_ID` are auto-detected (`channel_post` handler) and indexed into `channel_videos`.
- Each user gets `FREE_VIDEOS` (default 3) free watches. After that, they hit a verification gate.
- Verification sends the user a link shortened via the vplink.in API. Completing it deep-links back into the bot (`/start verify-<uid>-<slot>-<token>`), which grants `ACCESS_HOURS` (default 12h) of unlimited access.
- Verification tokens alternate between two API slots/tokens (`VP_LINK_TOKEN_1` / `VP_LINK_TOKEN_2`) each time a user re-verifies.
- Video selection avoids repeats: picks unseen videos first, with a small `REPEAT_CHANCE` (2%) chance to re-serve a seen one; once everything is seen, it serves randomly from the full set.
- Sent videos/nav messages self-delete after `AUTO_DELETE_VIDEO` (10 min) and are also cleaned up as soon as the user navigates to the next one. `protect_content=True` blocks forwarding/saving.
- Free-watch counters reset every `FREE_RESET_HOURS` (24h) if the user has no active verified access.
- Users who've already seen the whole library automatically get pushed any newly indexed video.

## Requirements

```
aiogram==3.13.0
asyncpg==0.30.0
aiohttp==3.10.5
```

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `BOT_TOKEN` | Yes | Telegram bot token |
| `ADMIN_ID` | Yes | Telegram user ID with access to admin commands |
| `CHANNEL_ID` | Yes | Source channel ID videos are indexed/copied from |
| `DATABASE_URL` | Yes | Postgres connection string (SSL required) |
| `VP_LINK_TOKEN_1` | No | vplink.in API token, slot 1 (has a default) |
| `VP_LINK_TOKEN_2` | No | vplink.in API token, slot 2 (has a default) |
| `BOT_USERNAME` | No | Bot's @username, used to build the deep-link (default `RNDAccess_bot`) |

> The bot must be an **admin** in `CHANNEL_ID` with permission to read messages, so it can see and copy posted videos.

## Database

Schema is created automatically on startup (`init_db`), including idempotent `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` migrations, so it's safe to redeploy without a manual migration step.

Tables: `channel_videos`, `users`, `verifications`, `user_history`, `broadcast_msgs`.

## Commands

**User**
- `/start` — begin, or complete a verification deep-link
- `/help` — quick usage summary

**Admin only** (`ADMIN_ID`)
- `/status` — verification/user/video counts
- `/index <id> <id> ...` — manually index channel message IDs as videos
- `/reset` — clears every user's access/watch state
- `/broadcast` — reply to a message with this to copy it to all non-banned users (auto-deletes after 12h)

## Running

```bash
pip install -r requirements.txt
export BOT_TOKEN=... ADMIN_ID=... CHANNEL_ID=... DATABASE_URL=...
python bot.py
```

## Deployment notes (Railway + Supabase)

- Point `DATABASE_URL` at your Supabase Postgres connection string (`sslmode=require` is already enforced in code).
- `statement_cache_size=0` is set for asyncpg — needed for connection poolers like Supabase's PgBouncer (transaction mode).
- Background loops (`task_expire_access`, `task_delete_broadcasts`) run every 60s inside the same process — no separate worker needed.
