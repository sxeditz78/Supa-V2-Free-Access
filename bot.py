import os
import asyncio
import logging
import random
import hashlib
import urllib.parse
from datetime import datetime, timedelta, timezone

import asyncpg
import aiohttp
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandStart
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

BOT_TOKEN    = os.environ["BOT_TOKEN"]
ADMIN_ID     = int(os.environ["ADMIN_ID"])
CHANNEL_ID   = int(os.environ["CHANNEL_ID"])
DATABASE_URL = os.environ["DATABASE_URL"]
VP_TOKEN_1   = os.environ.get("VP_LINK_TOKEN_1", "74a6ed2dfdde5ead4af3763d6e35330761a8c00c")
VP_TOKEN_2   = os.environ.get("VP_LINK_TOKEN_2", "f978712f218b482b5b66d00bb570e97a49bd4d08")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "RNDAccess_bot").lstrip("@")

FREE_VIDEOS       = 3
ACCESS_HOURS      = 12
FREE_RESET_HOURS  = 24
AUTO_DELETE_VIDEO = 600   # 10 min
AUTO_DELETE_CMD   = 10
BROADCAST_TTL     = 43200
RANDOM_INJECT_PCT = 0.02
REPEAT_CHANCE     = 0.02   # 2% chance to repeat a seen video when unseen exist

SCHEMA = """
CREATE TABLE IF NOT EXISTS channel_videos (
    id         SERIAL PRIMARY KEY,
    message_id BIGINT NOT NULL UNIQUE,
    added_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS users (
    user_id          BIGINT PRIMARY KEY,
    current_index    INT         NOT NULL DEFAULT 0,
    videos_watched   INT         NOT NULL DEFAULT 0,
    free_start_ts    TIMESTAMPTZ,
    access_until     TIMESTAMPTZ,
    last_verify_msg  BIGINT,
    last_video_msg   BIGINT,
    last_nav_msg     BIGINT,
    is_banned        BOOLEAN     NOT NULL DEFAULT FALSE,
    has_seen_all     BOOLEAN     NOT NULL DEFAULT FALSE,
    verify_slot      INT         NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS verifications (
    id          SERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL,
    verified_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS user_history (
    user_id    BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    seen_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, message_id)
);
CREATE TABLE IF NOT EXISTS broadcast_msgs (
    id          SERIAL PRIMARY KEY,
    chat_id     BIGINT NOT NULL,
    message_id  BIGINT NOT NULL,
    delete_at   TIMESTAMPTZ NOT NULL
);
"""

pool = None

async def init_db():
    global pool
    pool = await asyncpg.create_pool(
        DATABASE_URL,
        ssl="require",
        min_size=1,
        max_size=5,
        statement_cache_size=0,
        server_settings={"search_path": "public"},
    )
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA)
        for m in [
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS free_start_ts TIMESTAMPTZ",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS access_until TIMESTAMPTZ",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_verify_msg BIGINT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_video_msg BIGINT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_nav_msg BIGINT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS current_index INT NOT NULL DEFAULT 0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_banned BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS videos_watched INT NOT NULL DEFAULT 0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS has_seen_all BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS verify_slot INT NOT NULL DEFAULT 1",
        ]:
            try:
                await conn.execute(m)
            except Exception:
                pass
    logger.info("DB pool ready")

async def get_user(conn, user_id):
    row = await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)
    if row is None:
        await conn.execute("INSERT INTO users(user_id) VALUES($1) ON CONFLICT DO NOTHING", user_id)
        row = await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)
    return row

async def get_video_ids():
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT message_id FROM channel_videos ORDER BY id ASC")
    return [r["message_id"] for r in rows]

async def mark_seen(conn, user_id, msg_id):
    """Record that user has watched this video."""
    await conn.execute(
        "INSERT INTO user_history(user_id, message_id) VALUES($1,$2) ON CONFLICT DO NOTHING",
        user_id, msg_id,
    )

async def get_seen_ids(conn, user_id):
    """Return set of message_ids user has already seen."""
    rows = await conn.fetch("SELECT message_id FROM user_history WHERE user_id=$1", user_id)
    return {r["message_id"] for r in rows}

async def pick_video(conn, user_id, video_ids):
    """
    Pick next video for user based on history:
      Case 1 — unseen exist:  98% unseen random, 2% seen random repeat
      Case 2 — all seen:      100% seen random repeat
      Case 3 — new DB media:  auto appears in unseen → 98% chance it gets picked
    Returns (msg_id, all_seen: bool)
    """
    if not video_ids:
        return None, False
    seen    = await get_seen_ids(conn, user_id)
    unseen  = [v for v in video_ids if v not in seen]
    all_ids = video_ids

    if unseen:
        # Case 1 / Case 3
        if random.random() < REPEAT_CHANCE and seen:
            msg_id = random.choice([v for v in all_ids if v in seen])
        else:
            msg_id = random.choice(unseen)
        all_seen = False
    else:
        # Case 2 — all seen
        msg_id   = random.choice(all_ids)
        all_seen = True

    return msg_id, all_seen

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher()

def now_utc():
    return datetime.now(timezone.utc)

def _vp_token(slot: int) -> str:
    return VP_TOKEN_1 if slot == 1 else VP_TOKEN_2

def make_token(user_id, slot: int = 1):
    # slot determines which VP token was used — included in hash so tokens differ per slot
    secret = _vp_token(slot)
    return hashlib.sha256(f"{user_id}:{secret}:{slot}".encode()).hexdigest()[:32]

async def make_verify_url(user_id, slot: int = 1):
    token   = make_token(user_id, slot)
    payload = f"verify-{user_id}-{slot}-{token}"
    dest    = f"https://t.me/{BOT_USERNAME}?start={payload}"
    encoded = urllib.parse.quote(dest, safe="")
    vp_tok  = _vp_token(slot)
    api_url = f"https://vplink.in/api?api={vp_tok}&url={encoded}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json(content_type=None)
                if data.get("status") == "success":
                    return data["shortenedUrl"]
                else:
                    logger.warning(f"VPLink API error uid={user_id} slot={slot}: {data}")
    except Exception as e:
        logger.warning(f"VPLink request failed uid={user_id} slot={slot}: {e}")
    return dest

def nav_kb(index):
    b = InlineKeyboardBuilder()
    b.button(text="◀️  Previous", callback_data=f"nav:prev:{index}")
    b.button(text="Next  ▶️", callback_data=f"nav:next:{index}")
    b.adjust(2)
    return b.as_markup()

def verify_kb(url):
    b = InlineKeyboardBuilder()
    b.button(text="🔓  Unlock Full Access  →", url=url)
    return b.as_markup()

async def silent_delete(chat_id, msg_id):
    if not msg_id:
        return
    try:
        await bot.delete_message(chat_id, msg_id)
    except Exception:
        pass

async def delete_after(chat_id, msg_id, delay):
    await asyncio.sleep(delay)
    await silent_delete(chat_id, msg_id)

async def has_access(conn, user_id, next_index=None):
    """
    Check if user can watch the next video.
    Gate triggers after FREE_VIDEOS total watches (not index-based).
    next_index: kept for signature compatibility but watch count is the real gate.
    """
    row = await get_user(conn, user_id)
    if row["is_banned"]:
        return False
    # Active verified access
    if row["access_until"] and row["access_until"] > now_utc():
        return True
    # Free window: allow only first FREE_VIDEOS watches within 24h window
    if row["videos_watched"] < FREE_VIDEOS:
        return True
    return False

async def show_gate(user_id, conn):
    """Delete old gate message and send fresh verification gate using correct token slot."""
    row  = await get_user(conn, user_id)
    slot = int(row["verify_slot"] or 1)
    await silent_delete(user_id, row["last_verify_msg"])
    vp_url = await make_verify_url(user_id, slot)
    sent = await bot.send_message(
        user_id,
        "🔒 *Access Required*\n\n"
        "You've enjoyed your free previews\\.\n"
        "Unlock *12 hours of unlimited access* — completely free\\.\n\n"
        "⚡ One quick verification, that's it\\.\n"
        "_Takes under 30 seconds\\._",
        parse_mode="MarkdownV2",
        reply_markup=verify_kb(vp_url),
    )
    await conn.execute("UPDATE users SET last_verify_msg=$1 WHERE user_id=$2", sent.message_id, user_id)

async def delete_prev_video(user_id, conn):
    """Delete user's previously sent nav + video message."""
    row = await get_user(conn, user_id)
    # Delete nav first, then video (cleaner UX)
    await silent_delete(user_id, row["last_nav_msg"])
    await silent_delete(user_id, row["last_video_msg"])

async def send_video(user_id, index, video_ids, conn):
    """Send video using history-based pick logic. Deletes previous video first."""
    if not video_ids:
        return

    await delete_prev_video(user_id, conn)

    msg_id, all_seen = await pick_video(conn, user_id, video_ids)
    if msg_id is None:
        return

    # Update has_seen_all flag
    await conn.execute(
        "UPDATE users SET has_seen_all=$1 WHERE user_id=$2", all_seen, user_id
    )

    # Clamp index for nav buttons
    safe_index = index % len(video_ids)

    try:
        vid = await bot.copy_message(
            chat_id=user_id,
            from_chat_id=CHANNEL_ID,
            message_id=msg_id,
            protect_content=True,
        )
    except TelegramBadRequest as e:
        logger.warning(f"copy_message uid={user_id} mid={msg_id}: {e}")
        return

    try:
        nav = await bot.send_message(
            chat_id=user_id,
            text="🎬  *More videos below — keep going\\!*",
            parse_mode="MarkdownV2",
            reply_markup=nav_kb(safe_index),
        )
    except Exception as e:
        logger.warning(f"nav send failed uid={user_id}: {e}")
        nav = None

    nav_id = nav.message_id if nav else None

    # Mark as seen + save IDs + bump watch count
    await mark_seen(conn, user_id, msg_id)
    await conn.execute(
        "UPDATE users SET last_video_msg=$1, last_nav_msg=$2, videos_watched=videos_watched+1 WHERE user_id=$3",
        vid.message_id, nav_id, user_id,
    )

    asyncio.create_task(delete_after(user_id, vid.message_id, AUTO_DELETE_VIDEO))
    if nav_id:
        asyncio.create_task(delete_after(user_id, nav_id, AUTO_DELETE_VIDEO))


async def push_latest_to_seen_all(new_msg_id: int):
    """Send newly added video to all users who have seen the full playlist."""
    video_ids = await get_video_ids()
    if not video_ids:
        return
    new_index = len(video_ids) - 1

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT u.user_id FROM users u
               WHERE u.has_seen_all = TRUE
                 AND u.is_banned    = FALSE
                 AND NOT EXISTS (
                     SELECT 1 FROM user_history h
                     WHERE h.user_id = u.user_id AND h.message_id = $1
                 )""",
            new_msg_id,
        )

    logger.info(f"push_latest mid={new_msg_id} → {len(rows)} eligible users")

    for row in rows:
        uid = row["user_id"]
        try:
            async with pool.acquire() as conn:
                # No access gate — push regardless of verification status
                await delete_prev_video(uid, conn)

                try:
                    vid = await bot.copy_message(
                        chat_id=uid,
                        from_chat_id=CHANNEL_ID,
                        message_id=new_msg_id,
                        protect_content=True,
                    )
                except TelegramBadRequest as e:
                    logger.warning(f"push_latest copy uid={uid}: {e}")
                    continue

                try:
                    nav = await bot.send_message(
                        chat_id=uid,
                        text="🎬  *More videos below — keep going\\!*",
                        parse_mode="MarkdownV2",
                        reply_markup=nav_kb(new_index),
                    )
                except Exception:
                    nav = None

                nav_id = nav.message_id if nav else None

                # Mark seen + reset has_seen_all (new content = not all seen anymore)
                await mark_seen(conn, uid, new_msg_id)
                await conn.execute(
                    """UPDATE users
                       SET last_video_msg=$1, last_nav_msg=$2,
                           has_seen_all=FALSE, current_index=$3,
                           videos_watched=videos_watched+1
                       WHERE user_id=$4""",
                    vid.message_id, nav_id, new_index, uid,
                )

                asyncio.create_task(delete_after(uid, vid.message_id, AUTO_DELETE_VIDEO))
                if nav_id:
                    asyncio.create_task(delete_after(uid, nav_id, AUTO_DELETE_VIDEO))

        except TelegramForbiddenError:
            pass
        except Exception as e:
            logger.warning(f"push_latest uid={uid}: {e}")
        await asyncio.sleep(0.05)


# ── Channel post auto-indexer ─────────────────────────────────────────────────
@dp.channel_post()
async def on_channel_post(message: types.Message):
    if message.chat.id != CHANNEL_ID:
        return
    is_video = bool(message.video) or bool(
        message.document and message.document.mime_type
        and "video" in message.document.mime_type
    )
    if not is_video:
        return

    new_msg_id = message.message_id

    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO channel_videos(message_id) VALUES($1) ON CONFLICT DO NOTHING",
            new_msg_id,
        )

    logger.info(f"Auto-indexed video: msg_id={new_msg_id}")
    asyncio.create_task(push_latest_to_seen_all(new_msg_id))


# ── /index (admin) ────────────────────────────────────────────────────────────
@dp.message(Command("index"))
async def cmd_index(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split()[1:]
    if not parts:
        sent = await message.answer(
            "ℹ️ Usage: `/index 101 102 103`\nSpace-separated channel message IDs.",
            parse_mode="Markdown"
        )
        asyncio.create_task(delete_after(message.chat.id, sent.message_id, 30))
        try: await message.delete()
        except Exception: pass
        return
    added = 0
    last_added_id = None
    async with pool.acquire() as conn:
        for p in parts:
            try:
                await conn.execute(
                    "INSERT INTO channel_videos(message_id) VALUES($1) ON CONFLICT DO NOTHING", int(p)
                )
                last_added_id = int(p)
                added += 1
            except Exception:
                pass
    total = len(await get_video_ids())
    sent = await message.answer(
        f"✅ *{added} video\\(s\\) indexed\\.*\n📹 Total in DB: `{total}`",
        parse_mode="MarkdownV2",
    )
    asyncio.create_task(delete_after(message.chat.id, sent.message_id, 20))
    try: await message.delete()
    except Exception: pass
    # Push latest to has_seen_all users
    if last_added_id:
        asyncio.create_task(push_latest_to_seen_all(last_added_id))


# ── /start ────────────────────────────────────────────────────────────────────
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    parts   = (message.text or "").split(maxsplit=1)
    payload = parts[1] if len(parts) > 1 else ""

    async with pool.acquire() as conn:
        row = await get_user(conn, user_id)

        # ── Verify deep-link: format is  verify-USERID-SLOT-TOKEN ──────────
        if payload.startswith("verify-"):
            # verify-<uid>-<slot>-<token>
            segs = payload.split("-", 3)
            if len(segs) == 4 and segs[0] == "verify":
                _, uid_str, slot_str, tok = segs
                try:
                    uid_int  = int(uid_str)
                    slot_int = int(slot_str)
                except ValueError:
                    uid_int = slot_int = -1
                if uid_int == user_id and slot_int in (1, 2) and tok == make_token(user_id, slot_int):
                    expiry     = now_utc() + timedelta(hours=ACCESS_HOURS)
                    # Next verification should use the other slot (toggle 1↔2)
                    next_slot  = 2 if slot_int == 1 else 1
                    await conn.execute(
                        "UPDATE users SET access_until=$1, last_verify_msg=NULL, videos_watched=0, verify_slot=$2 WHERE user_id=$3",
                        expiry, next_slot, user_id,
                    )
                    await conn.execute("INSERT INTO verifications(user_id) VALUES($1)", user_id)
                    await silent_delete(user_id, row["last_verify_msg"])
                    s = await message.answer(
                        f"✅ *Access Granted\\!*\n\n"
                        f"Welcome back, *{message.from_user.first_name}*\\! 🎉\n\n"
                        f"⏳ You have *{ACCESS_HOURS} hours* of unlimited access\\.\n"
                        "🎬 Sit back and enjoy\\.",
                        parse_mode="MarkdownV2",
                    )
                    asyncio.create_task(delete_after(user_id, s.message_id, AUTO_DELETE_CMD))
                    try:
                        await bot.send_message(
                            ADMIN_ID,
                            f"✅ *Verified*\n"
                            f"👤 {message.from_user.full_name}\n"
                            f"🆔 `{user_id}`  •  Slot `{slot_int}`",
                            parse_mode="Markdown",
                        )
                    except Exception:
                        pass
                    video_ids = await get_video_ids()
                    if video_ids:
                        idx = row["current_index"]
                        await send_video(user_id, idx, video_ids, conn)
                    return
                else:
                    await message.answer(
                        "❌ *Link Expired*\n\n"
                        "_This verification link is no longer valid\\._\n"
                        "Tap /start to get a fresh one\\.",
                        parse_mode="MarkdownV2",
                    )
                    return

        # ── Normal /start ─────────────────────────────────────────────────
        video_ids = await get_video_ids()
        if not video_ids:
            await message.answer(
                "🕐 *Coming Soon*\n\n_Content is being prepared\\. Check back shortly\\!_",
                parse_mode="MarkdownV2",
            )
            return

        # Init free window
        if row["free_start_ts"] is None:
            await conn.execute("UPDATE users SET free_start_ts=$1 WHERE user_id=$2", now_utc(), user_id)
            row = await get_user(conn, user_id)

        # 24h reset
        if row["free_start_ts"]:
            elapsed = (now_utc() - row["free_start_ts"]).total_seconds()
            no_acc  = not row["access_until"] or row["access_until"] <= now_utc()
            if elapsed >= FREE_RESET_HOURS * 3600 and no_acc:
                await conn.execute(
                    "UPDATE users SET free_start_ts=$1, current_index=0, videos_watched=0, verify_slot=1 WHERE user_id=$2",
                    now_utc(), user_id,
                )
                row = await get_user(conn, user_id)

        idx    = row["current_index"]
        access = await has_access(conn, user_id, next_index=idx)

        if not access:
            await show_gate(user_id, conn)
            return

        await send_video(user_id, idx, video_ids, conn)


# ── Navigation callbacks ──────────────────────────────────────────────────────
@dp.callback_query(F.data.startswith("nav:"))
async def cb_nav(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    _, direction, idx_str = callback.data.split(":")
    idx = int(idx_str)

    async with pool.acquire() as conn:
        row       = await get_user(conn, user_id)
        video_ids = await get_video_ids()

        if not video_ids:
            await callback.answer("⚠️ No content available yet.", show_alert=True)
            return

        total   = len(video_ids)
        raw_idx = (idx + 1) if direction == "next" else (idx - 1)
        new_idx = raw_idx % total

        # 24h reset
        if row["free_start_ts"]:
            elapsed = (now_utc() - row["free_start_ts"]).total_seconds()
            no_acc  = not row["access_until"] or row["access_until"] <= now_utc()
            if elapsed >= FREE_RESET_HOURS * 3600 and no_acc:
                await conn.execute(
                    "UPDATE users SET free_start_ts=$1, current_index=0, videos_watched=0, verify_slot=1 WHERE user_id=$2",
                    now_utc(), user_id,
                )
                new_idx = 0
                row = await get_user(conn, user_id)

        # Gate check — pass new_idx so we check the video they want to see
        access = await has_access(conn, user_id, next_index=new_idx)
        if not access:
            await callback.answer()
            await silent_delete(user_id, row["last_nav_msg"])
            await show_gate(user_id, conn)
            return

        # Access granted — save index and send video
        await conn.execute("UPDATE users SET current_index=$1 WHERE user_id=$2", new_idx, user_id)
        await callback.answer()
        await send_video(user_id, new_idx, video_ids, conn)


# ── /help ─────────────────────────────────────────────────────────────────────
@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    sent = await message.answer(
        "📖 *How It Works*\n\n"
        "1\\. /start → watch your first 3 videos free\n"
        "2\\. Verify once → unlock *12 hours* of unlimited access\n"
        "3\\. Access resets every *24 hours* automatically\n\n"
        "💡 _Verification takes under 30 seconds\\._",
        parse_mode="MarkdownV2",
    )
    asyncio.create_task(delete_after(message.chat.id, sent.message_id, 30))


# ── /status (admin) ───────────────────────────────────────────────────────────
@dp.message(Command("status"))
async def cmd_status(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    async with pool.acquire() as conn:
        v = await conn.fetchval(
            "SELECT COUNT(*) FROM verifications WHERE verified_at >= $1",
            now_utc() - timedelta(hours=24),
        )
        u = await conn.fetchval("SELECT COUNT(*) FROM users")
    vids = await get_video_ids()
    sent = await message.answer(
        f"📊 *Bot Stats*\n\n"
        f"✅  Verifications \\(24h\\): `{v}`\n"
        f"👥  Total Users: `{u}`\n"
        f"📹  Videos Indexed: `{len(vids)}`",
        parse_mode="MarkdownV2",
    )
    asyncio.create_task(delete_after(message.chat.id, sent.message_id, 30))
    try: await message.delete()
    except Exception: pass


# ── /reset (admin) ────────────────────────────────────────────────────────────
@dp.message(Command("reset"))
async def cmd_reset(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    async with pool.acquire() as conn:
        await conn.execute("UPDATE users SET access_until=NULL, free_start_ts=NULL, current_index=0, videos_watched=0, has_seen_all=FALSE")
    sent = await message.answer("♻️ *All users have been reset successfully\\.*", parse_mode="MarkdownV2")
    asyncio.create_task(delete_after(message.chat.id, sent.message_id, AUTO_DELETE_CMD))
    try: await message.delete()
    except Exception: pass


# ── /broadcast (admin) ────────────────────────────────────────────────────────
@dp.message(Command("broadcast"))
async def cmd_broadcast(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    reply = message.reply_to_message
    if not reply:
        sent = await message.answer("↩️ *Reply to a message* with /broadcast to send it to all users\\.", parse_mode="MarkdownV2")
        asyncio.create_task(delete_after(message.chat.id, sent.message_id, AUTO_DELETE_CMD))
        try: await message.delete()
        except Exception: pass
        return

    async with pool.acquire() as conn:
        uids = [r["user_id"] for r in await conn.fetch("SELECT user_id FROM users WHERE is_banned=FALSE")]

    delete_at = now_utc() + timedelta(seconds=BROADCAST_TTL)
    ok = fail = 0
    async with pool.acquire() as conn:
        for uid in uids:
            try:
                sm = await reply.copy_to(uid)
                await conn.execute(
                    "INSERT INTO broadcast_msgs(chat_id,message_id,delete_at) VALUES($1,$2,$3)",
                    uid, sm.message_id, delete_at,
                )
                ok += 1
            except TelegramForbiddenError:
                fail += 1
            except Exception as e:
                logger.warning(f"broadcast uid={uid}: {e}")
                fail += 1
            await asyncio.sleep(0.05)

    sent = await message.answer(
        f"📣 *Broadcast Complete*\n\n✅ Sent: `{ok}`   ❌ Failed: `{fail}`",
        parse_mode="Markdown",
    )
    asyncio.create_task(delete_after(message.chat.id, sent.message_id, 30))
    try: await message.delete()
    except Exception: pass


# ── Background tasks ──────────────────────────────────────────────────────────
async def task_expire_access():
    while True:
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT user_id FROM users WHERE access_until IS NOT NULL AND access_until <= $1",
                    now_utc(),
                )
                for row in rows:
                    uid = row["user_id"]
                    try:
                        await bot.send_message(uid,
                            "⏳ *Your access has expired\\.*\n\n"
                            "Verify once more to unlock another *12 hours* — it's quick and free\\.\n\n"
                            "Tap /start to continue\\. 🎬",
                            parse_mode="MarkdownV2")
                    except Exception:
                        pass
                    await conn.execute("UPDATE users SET access_until=NULL WHERE user_id=$1", uid)
        except Exception as e:
            logger.error(f"expire task: {e}")
        await asyncio.sleep(60)

async def task_delete_broadcasts():
    while True:
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT id,chat_id,message_id FROM broadcast_msgs WHERE delete_at <= $1", now_utc()
                )
                for row in rows:
                    await silent_delete(row["chat_id"], row["message_id"])
                    await conn.execute("DELETE FROM broadcast_msgs WHERE id=$1", row["id"])
        except Exception as e:
            logger.error(f"broadcast cleanup: {e}")
        await asyncio.sleep(60)


# ── Startup / shutdown ────────────────────────────────────────────────────────
async def on_startup():
    await init_db()
    asyncio.create_task(task_expire_access())
    asyncio.create_task(task_delete_broadcasts())
    total = len(await get_video_ids())
    logger.info(f"Bot started — {total} videos in DB")
    if total == 0:
        try:
            await bot.send_message(
                ADMIN_ID,
                "⚠️ *0 videos indexed!*\n\n"
                "Channel mein bot ko Admin banao (Read Messages permission).\n"
                "Existing videos ke liye:\n`/index 101 102 103 ...`",
                parse_mode="Markdown",
            )
        except Exception:
            pass

async def on_shutdown():
    if pool:
        await pool.close()

async def main():
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    asyncio.run(main())
