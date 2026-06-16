import asyncio
import json
import logging
import os
import random
import re
import sqlite3
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv


load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
DEFAULT_TIMEZONE = os.getenv("DEFAULT_TIMEZONE", "Asia/Seoul")
DEFAULT_NOTICE_CHANNEL_ID = os.getenv("DEFAULT_NOTICE_CHANNEL_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
CURRENT_GEMINI_MODEL = GEMINI_MODEL
BOT_OWNER_IDS = {
    int(owner_id.strip())
    for owner_id in os.getenv("BOT_OWNER_IDS", "").split(",")
    if owner_id.strip().isdigit()
}
DB_PATH = "bot.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("schedule-bot")

ACTIVE_UPDOWN_GAMES: dict[int, int] = {}
ACTIVE_TYPING_GAMES: dict[int, tuple[str, datetime]] = {}
POLL_EMOJIS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
DB_ADMIN_TABLES = {
    "schedules",
    "bot_admins",
    "quotes",
    "warnings",
    "deleted_messages",
}
AVAILABLE_GEMINI_MODELS = [
    "gemini-3.5-flash",
    "gemini-3.5-pro",
    "gemini-3-flash",
    "gemini-3.1-pro",
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.5-pro",
]
AI_ALLOWED_CHANNEL_IDS = {
    int(channel_id.strip())
    for channel_id in os.getenv("AI_ALLOWED_CHANNEL_IDS", "").split(",")
    if channel_id.strip().isdigit()
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def local_tz() -> ZoneInfo:
    return ZoneInfo(DEFAULT_TIMEZONE)


def parse_datetime(value: str, all_day: bool = False) -> datetime:
    text = value.strip()
    now = datetime.now(local_tz())

    formats = [
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M",
        "%m/%d %H:%M",
        "%m-%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%m/%d",
        "%m-%d",
    ]
    for fmt in formats:
        try:
            parsed = datetime.strptime(text, fmt)
            if "%Y" not in fmt:
                parsed = parsed.replace(year=now.year)
            if "%H" not in fmt or all_day:
                parsed = parsed.replace(hour=0, minute=0)
            return parsed.replace(tzinfo=local_tz())
        except ValueError:
            pass

    korean_pattern = re.compile(
        r"(?:(?P<year>\d{4})\s*년\s*)?"
        r"(?P<month>\d{1,2})\s*월\s*"
        r"(?P<day>\d{1,2})\s*일"
        r"(?:\s*(?P<hour>\d{1,2})\s*시)?"
        r"(?:\s*(?P<minute>\d{1,2})\s*분?)?"
    )
    match = korean_pattern.fullmatch(text)
    if match:
        year = int(match.group("year") or now.year)
        month = int(match.group("month"))
        day = int(match.group("day"))
        hour = 0 if all_day else int(match.group("hour") or 0)
        minute = 0 if all_day else int(match.group("minute") or 0)
        return datetime(year, month, day, hour, minute, tzinfo=local_tz())

    raise ValueError("날짜 형식을 이해하지 못했습니다. 예: 6월8일 14시, 2026-06-08 14:00")


def to_utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def from_utc_iso(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def format_dt(value: str, all_day: bool) -> str:
    dt = from_utc_iso(value).astimezone(local_tz())
    if all_day:
        return dt.strftime("%Y-%m-%d")
    return dt.strftime("%Y-%m-%d %H:%M")


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER,
                title TEXT NOT NULL,
                description TEXT,
                category TEXT,
                start_at TEXT NOT NULL,
                end_at TEXT NOT NULL,
                all_day INTEGER NOT NULL DEFAULT 0,
                reminder_minutes INTEGER NOT NULL DEFAULT 0,
                reminder_sent INTEGER NOT NULL DEFAULT 0,
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_schedules_guild_start ON schedules(guild_id, start_at)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_admins (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                granted_by INTEGER NOT NULL,
                granted_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS quotes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                quote TEXT NOT NULL,
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS warnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                reason TEXT NOT NULL,
                warned_by INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS deleted_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                author_id INTEGER NOT NULL,
                author_name TEXT NOT NULL,
                content TEXT NOT NULL,
                deleted_at TEXT NOT NULL
            )
            """
        )


@dataclass
class Schedule:
    id: int
    guild_id: int
    channel_id: Optional[int]
    title: str
    description: Optional[str]
    category: Optional[str]
    start_at: str
    end_at: str
    all_day: bool
    reminder_minutes: int
    reminder_sent: bool
    created_by: int


def row_to_schedule(row: sqlite3.Row) -> Schedule:
    return Schedule(
        id=row["id"],
        guild_id=row["guild_id"],
        channel_id=row["channel_id"],
        title=row["title"],
        description=row["description"],
        category=row["category"],
        start_at=row["start_at"],
        end_at=row["end_at"],
        all_day=bool(row["all_day"]),
        reminder_minutes=row["reminder_minutes"],
        reminder_sent=bool(row["reminder_sent"]),
        created_by=row["created_by"],
    )


def add_schedule(
    guild_id: int,
    channel_id: int,
    title: str,
    start_at: datetime,
    end_at: datetime,
    all_day: bool,
    reminder_minutes: int,
    created_by: int,
    description: Optional[str] = None,
    category: Optional[str] = None,
) -> int:
    if end_at <= start_at:
        raise ValueError("종료 시간은 시작 시간보다 뒤여야 합니다.")
    if reminder_minutes < 0:
        raise ValueError("미리알림 시간은 0분 이상이어야 합니다.")

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            """
            INSERT INTO schedules (
                guild_id, channel_id, title, description, category, start_at, end_at,
                all_day, reminder_minutes, created_by, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                channel_id,
                title,
                description,
                category,
                to_utc_iso(start_at),
                to_utc_iso(end_at),
                int(all_day),
                reminder_minutes,
                created_by,
                utc_now().isoformat(),
            ),
        )
        return int(cursor.lastrowid)


def delete_schedule(guild_id: int, schedule_id: int) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            "DELETE FROM schedules WHERE guild_id = ? AND id = ?",
            (guild_id, schedule_id),
        )
        return cursor.rowcount > 0


def list_schedules(guild_id: int, limit: int = 20) -> list[Schedule]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT * FROM schedules
            WHERE guild_id = ? AND end_at >= ?
            ORDER BY start_at ASC
            LIMIT ?
            """,
            (guild_id, utc_now().isoformat(), limit),
        ).fetchall()
        return [row_to_schedule(row) for row in rows]


def get_schedule(guild_id: int, schedule_id: int) -> Optional[Schedule]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM schedules WHERE guild_id = ? AND id = ?",
            (guild_id, schedule_id),
        ).fetchone()
        return row_to_schedule(row) if row else None


def due_reminders() -> list[Schedule]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT * FROM schedules
            WHERE reminder_sent = 0
              AND reminder_minutes > 0
            ORDER BY start_at ASC
            LIMIT 100
            """,
        ).fetchall()
        schedules = [row_to_schedule(row) for row in rows]

    return [
        schedule
        for schedule in schedules
        if from_utc_iso(schedule.start_at) - timedelta(minutes=schedule.reminder_minutes) <= utc_now()
    ]


def mark_reminder_sent(schedule_id: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE schedules SET reminder_sent = 1 WHERE id = ?", (schedule_id,))


def grant_bot_admin(guild_id: int, user_id: int, granted_by: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO bot_admins (guild_id, user_id, granted_by, granted_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id)
            DO UPDATE SET granted_by = excluded.granted_by, granted_at = excluded.granted_at
            """,
            (guild_id, user_id, granted_by, utc_now().isoformat()),
        )


def revoke_bot_admin(guild_id: int, user_id: int) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            "DELETE FROM bot_admins WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        return cursor.rowcount > 0


def is_stored_bot_admin(guild_id: Optional[int], user_id: int) -> bool:
    if guild_id is None:
        return False
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT 1 FROM bot_admins WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()
        return row is not None


def bot_admin_exists(guild_id: int, user_id: int) -> bool:
    return is_stored_bot_admin(guild_id, user_id)


def list_bot_admin_ids(guild_id: int) -> list[int]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT user_id FROM bot_admins WHERE guild_id = ? ORDER BY granted_at ASC",
            (guild_id,),
        ).fetchall()
        return [int(row[0]) for row in rows]


def add_quote(guild_id: int, user_id: int, quote: str, created_by: int) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            """
            INSERT INTO quotes (guild_id, user_id, quote, created_by, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (guild_id, user_id, quote, created_by, utc_now().isoformat()),
        )
        return int(cursor.lastrowid)


def random_quote(guild_id: int) -> Optional[sqlite3.Row]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM quotes WHERE guild_id = ? ORDER BY RANDOM() LIMIT 1",
            (guild_id,),
        ).fetchone()


def add_warning(guild_id: int, user_id: int, reason: str, warned_by: int) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            """
            INSERT INTO warnings (guild_id, user_id, reason, warned_by, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (guild_id, user_id, reason, warned_by, utc_now().isoformat()),
        )
        return int(cursor.lastrowid)


def list_warnings(guild_id: int, user_id: Optional[int] = None) -> list[sqlite3.Row]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        if user_id is None:
            return conn.execute(
                """
                SELECT user_id, COUNT(*) AS count FROM warnings
                WHERE guild_id = ?
                GROUP BY user_id
                ORDER BY count DESC
                LIMIT 20
                """,
                (guild_id,),
            ).fetchall()
        return conn.execute(
            """
            SELECT * FROM warnings
            WHERE guild_id = ? AND user_id = ?
            ORDER BY created_at DESC
            LIMIT 20
            """,
            (guild_id, user_id),
        ).fetchall()


def delete_warning(guild_id: int, warning_id: int) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            "DELETE FROM warnings WHERE guild_id = ? AND id = ?",
            (guild_id, warning_id),
        )
        return cursor.rowcount > 0


def clear_member_warnings(guild_id: int, user_id: int) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            "DELETE FROM warnings WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        return cursor.rowcount


def add_deleted_message(message: discord.Message) -> None:
    if message.guild is None or message.author.bot:
        return
    content = message.content.strip()
    if not content:
        content = "[내용 없음 또는 임베드/첨부파일]"
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO deleted_messages (guild_id, channel_id, author_id, author_name, content, deleted_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                message.guild.id,
                message.channel.id,
                message.author.id,
                str(message.author),
                content[:1500],
                utc_now().isoformat(),
            ),
        )


def list_deleted_messages(guild_id: int, limit: int) -> list[sqlite3.Row]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            """
            SELECT * FROM deleted_messages
            WHERE guild_id = ?
            ORDER BY deleted_at DESC
            LIMIT ?
            """,
            (guild_id, limit),
        ).fetchall()


def db_table_names() -> list[str]:
    return sorted(DB_ADMIN_TABLES)


def db_fetch_rows(table: str, limit: int) -> list[sqlite3.Row]:
    if table not in DB_ADMIN_TABLES:
        raise ValueError("허용되지 않은 테이블입니다.")
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(f"SELECT rowid AS row_id, * FROM {table} ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()


def db_delete_row(table: str, row_id: int) -> int:
    if table not in DB_ADMIN_TABLES:
        raise ValueError("허용되지 않은 테이블입니다.")
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(f"DELETE FROM {table} WHERE rowid = ?", (row_id,))
        return cursor.rowcount


def db_clear_table(table: str) -> int:
    if table not in DB_ADMIN_TABLES:
        raise ValueError("허용되지 않은 테이블입니다.")
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(f"DELETE FROM {table}")
        return cursor.rowcount


def format_db_rows(rows: list[sqlite3.Row]) -> str:
    if not rows:
        return "데이터가 없습니다."
    chunks = []
    for row in rows:
        items = []
        for key in row.keys():
            value = str(row[key]).replace("`", "'")
            if len(value) > 80:
                value = value[:77] + "..."
            items.append(f"{key}={value}")
        chunks.append(", ".join(items))
    text = "\n".join(chunks)
    return text[:3800]


def split_options(options: str) -> list[str]:
    return [option.strip() for option in re.split(r"[,/|]", options) if option.strip()]


def is_ai_channel(channel_id: Optional[int]) -> bool:
    if not AI_ALLOWED_CHANNEL_IDS:
        return True
    return channel_id in AI_ALLOWED_CHANNEL_IDS


def extract_gemini_text(data: dict) -> str:
    candidates = data.get("candidates") or []
    for candidate in candidates:
        content = candidate.get("content") or {}
        parts = content.get("parts") or []
        texts = [part.get("text", "") for part in parts if part.get("text")]
        if texts:
            return "\n".join(texts).strip()
    return ""


def ask_gemini(prompt: str, context: Optional[str] = None, replied_message: Optional[str] = None) -> str:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY가 설정되지 않았습니다.")

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{CURRENT_GEMINI_MODEL}:generateContent"
    prompt_text = (
        "당신은 디스코드 서버 봇 Agent입니다. 답변은 한국어로, 자연스럽고 너무 길지 않게 작성하세요. "
        "이전 대화 맥락이 있으면 이어서 답하고, 확신이 약하면 단정하지 마세요. "
        "사용자가 특정 메시지에 답글을 단 경우 그 메시지를 우선 해석 대상으로 삼으세요.\n"
    )
    if replied_message:
        prompt_text += f"사용자가 직접 답글로 가리킨 메시지:\n{replied_message}\n\n"
    if context:
        prompt_text += f"최근 대화 맥락:\n{context}\n\n"
    prompt_text += f"사용자 질문:\n{prompt}"
    payload = {
        "contents": [
            {
                "parts": [
                    {
                        "text": prompt_text
                    }
                ]
            }
        ]
    }
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": GEMINI_API_KEY,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Gemini API 오류: HTTP {exc.code} {detail[:200]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Gemini 연결 오류: {exc.reason}") from exc

    text = extract_gemini_text(data)
    if not text:
        raise RuntimeError("Gemini 응답에서 텍스트를 찾지 못했습니다.")
    return text[:1800]


async def build_recent_context(message: discord.Message, limit: int = 8) -> str:
    if not isinstance(message.channel, discord.TextChannel):
        return ""

    history: list[discord.Message] = []
    async for item in message.channel.history(limit=limit, before=message.created_at, oldest_first=False):
        if item.author.bot and (message.guild is None or item.author.id != message.guild.me.id):
            continue
        history.append(item)

    history.reverse()
    lines = []
    for item in history:
        author_name = item.author.display_name
        content = item.content.strip()
        if not content:
            continue
        lines.append(f"{author_name}: {content}")

    if message.reference and isinstance(message.reference.resolved, discord.Message):
        ref = message.reference.resolved
        ref_content = ref.content.strip()
        if ref_content:
            lines.append(f"reply_to {ref.author.display_name}: {ref_content}")

    return "\n".join(lines)[-3000:]


def extract_replied_message(message: discord.Message) -> Optional[str]:
    if not message.reference or not isinstance(message.reference.resolved, discord.Message):
        return None
    ref = message.reference.resolved
    content = ref.content.strip()
    if not content:
        return None
    return f"{ref.author.display_name}: {content}"


def make_schedule_embed(schedule: Schedule, title_prefix: str = "일정") -> discord.Embed:
    label = f"{title_prefix}: {schedule.title}"
    if schedule.category:
        label = f"{title_prefix}: [{schedule.category}] {schedule.title}"

    embed = discord.Embed(title=label, color=discord.Color.blue())
    embed.add_field(name="ID", value=str(schedule.id), inline=True)
    embed.add_field(name="시작", value=format_dt(schedule.start_at, schedule.all_day), inline=True)
    embed.add_field(name="종료", value=format_dt(schedule.end_at, schedule.all_day), inline=True)
    embed.add_field(name="종일", value="예" if schedule.all_day else "아니오", inline=True)
    embed.add_field(name="미리알림", value=f"{schedule.reminder_minutes}분 전", inline=True)
    if schedule.description:
        embed.add_field(name="설명", value=schedule.description[:1024], inline=False)
    return embed


class ScheduleBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.guild_commands_synced = False

    async def setup_hook(self) -> None:
        init_db()
        self.reminder_loop.start()
        logger.info("Bot setup complete.")

    async def on_ready(self) -> None:
        logger.info("Logged in as %s", self.user)
        if not self.guild_commands_synced:
            for guild in self.guilds:
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                logger.info("Synced %s commands to guild %s.", len(synced), guild.id)
            self.tree.clear_commands(guild=None)
            cleared = await self.tree.sync()
            logger.info("Cleared global commands. Remaining global commands: %s.", len(cleared))
            self.guild_commands_synced = True

    async def on_member_join(self, member: discord.Member) -> None:
        channel = get_guild_notice_channel(self, member.guild)
        if channel is None:
            return
        await channel.send(
            f"{member.mention}님 환영합니다!",
            allowed_mentions=discord.AllowedMentions(users=True),
        )

    async def on_member_remove(self, member: discord.Member) -> None:
        deleted_count = clear_member_warnings(member.guild.id, member.id)
        if deleted_count:
            logger.info(
                "Cleared %s warnings for user %s in guild %s after member left.",
                deleted_count,
                member.id,
                member.guild.id,
            )

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        game = ACTIVE_TYPING_GAMES.get(message.channel.id)
        if game is None:
            pass
        else:
            phrase, started_at = game
            if message.content.strip() == phrase:
                elapsed = (utc_now() - started_at).total_seconds()
                ACTIVE_TYPING_GAMES.pop(message.channel.id, None)
                await message.channel.send(f"{message.author.mention} 정답! {elapsed:.2f}초")
                return

        if self.user is None:
            return
        mentioned_bot = self.user in message.mentions
        replying_to_bot = bool(
            message.reference
            and isinstance(message.reference.resolved, discord.Message)
            and message.reference.resolved.author.id == self.user.id
        )
        if not mentioned_bot and not replying_to_bot:
            return
        if not is_ai_channel(message.channel.id):
            return

        prompt = message.content.replace(self.user.mention, "").strip()
        if not prompt and replying_to_bot:
            prompt = message.content.strip()
        if not prompt:
            await message.reply("질문을 같이 적어주세요. 예: `@Agent 오늘 일정 요약해줘`", mention_author=False)
            return

        context = await build_recent_context(message)
        replied_message = extract_replied_message(message)
        async with message.channel.typing():
            try:
                answer = await asyncio.to_thread(ask_gemini, prompt, context, replied_message)
            except RuntimeError as exc:
                await message.reply(str(exc), mention_author=False)
                return

        await message.reply(answer, mention_author=False)

    async def on_message_delete(self, message: discord.Message) -> None:
        add_deleted_message(message)

    @tasks.loop(seconds=30)
    async def reminder_loop(self) -> None:
        for schedule in due_reminders():
            channel = self.get_channel(schedule.channel_id or 0)
            if channel is None and DEFAULT_NOTICE_CHANNEL_ID:
                channel = self.get_channel(int(DEFAULT_NOTICE_CHANNEL_ID))
            if isinstance(channel, discord.abc.Messageable):
                await channel.send(
                    content=f"⏰ 일정 미리알림: **{schedule.title}** 이(가) {schedule.reminder_minutes}분 후 시작합니다.",
                    embed=make_schedule_embed(schedule, "미리알림"),
                    allowed_mentions=discord.AllowedMentions(everyone=False),
                )
            mark_reminder_sent(schedule.id)

    @reminder_loop.before_loop
    async def before_reminder_loop(self) -> None:
        await self.wait_until_ready()


bot = ScheduleBot()


class NoticeModal(discord.ui.Modal, title="공지 작성"):
    content = discord.ui.TextInput(
        label="공지 내용",
        style=discord.TextStyle.paragraph,
        placeholder="여러 줄 공지를 입력하세요.",
        max_length=1800,
        required=True,
    )

    def __init__(self, channel: discord.TextChannel) -> None:
        super().__init__()
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.channel.send(
            content=f"@everyone\n{as_blockquote(str(self.content.value))}",
            allowed_mentions=discord.AllowedMentions(everyone=True),
        )
        await interaction.response.send_message(f"{self.channel.mention}에 공지를 보냈습니다.", ephemeral=True)


def guild_only(interaction: discord.Interaction) -> bool:
    return interaction.guild is not None and interaction.guild_id is not None


def make_basic_embed(title: str, description: str, color: discord.Color = discord.Color.blurple()) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=color)


def get_guild_notice_channel(client: discord.Client, guild: discord.Guild) -> Optional[discord.TextChannel]:
    if DEFAULT_NOTICE_CHANNEL_ID:
        channel = client.get_channel(int(DEFAULT_NOTICE_CHANNEL_ID))
        if isinstance(channel, discord.TextChannel) and channel.guild.id == guild.id:
            return channel

    if isinstance(guild.system_channel, discord.TextChannel):
        return guild.system_channel

    for channel in guild.text_channels:
        permissions = channel.permissions_for(guild.me)
        if permissions.send_messages:
            return channel

    return None


def as_blockquote(content: str) -> str:
    lines = content.strip().splitlines() or [content.strip()]
    return "\n".join(f"> {line}" if line else ">" for line in lines)


def is_bot_owner(interaction: discord.Interaction) -> bool:
    return interaction.user.id in BOT_OWNER_IDS


def is_bot_manager(interaction: discord.Interaction) -> bool:
    return is_bot_owner(interaction) or is_stored_bot_admin(interaction.guild_id, interaction.user.id)


def bot_owner_required():
    async def predicate(interaction: discord.Interaction) -> bool:
        return is_bot_owner(interaction)

    return app_commands.check(predicate)


def bot_manager_required():
    async def predicate(interaction: discord.Interaction) -> bool:
        return is_bot_manager(interaction)

    return app_commands.check(predicate)


def owner_or_permissions(**permissions: bool):
    async def predicate(interaction: discord.Interaction) -> bool:
        if is_bot_owner(interaction):
            return True

        guild_permissions = getattr(interaction.user, "guild_permissions", None)
        if guild_permissions is None:
            return False

        return all(getattr(guild_permissions, name, None) == value for name, value in permissions.items())

    return app_commands.check(predicate)


@bot.tree.command(name="일정추가", description="일정을 추가합니다.")
@app_commands.describe(
    제목="일정 제목",
    시작="예: 6월8일 14시, 2026-06-08 14:00",
    종료="예: 6월8일 16시, 2026-06-08 16:00",
    종일="종일 일정 여부",
    미리알림분="시작 몇 분 전에 알림을 보낼지",
    설명="선택 설명",
)
async def schedule_add(
    interaction: discord.Interaction,
    제목: str,
    시작: str,
    종료: str,
    종일: bool = False,
    미리알림분: int = 30,
    설명: Optional[str] = None,
) -> None:
    if interaction.guild_id is None:
        await interaction.response.send_message("서버 안에서만 사용할 수 있습니다.", ephemeral=True)
        return

    try:
        start_at = parse_datetime(시작, 종일)
        end_at = parse_datetime(종료, 종일)
        schedule_id = add_schedule(
            interaction.guild_id,
            interaction.channel_id,
            제목,
            start_at,
            end_at,
            종일,
            미리알림분,
            interaction.user.id,
            설명,
        )
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return

    created = get_schedule(interaction.guild_id, schedule_id)
    if created is None:
        await interaction.response.send_message("일정은 저장됐지만 다시 불러오지 못했습니다.", ephemeral=True)
        return
    await interaction.response.send_message(embed=make_schedule_embed(created, "일정 추가 완료"))


@bot.tree.command(name="일정삭제", description="일정 ID로 일정을 삭제합니다.")
@app_commands.describe(일정id="삭제할 일정 ID")
async def schedule_delete(interaction: discord.Interaction, 일정id: int) -> None:
    if interaction.guild_id is None:
        await interaction.response.send_message("서버 안에서만 사용할 수 있습니다.", ephemeral=True)
        return

    if delete_schedule(interaction.guild_id, 일정id):
        await interaction.response.send_message(f"일정 #{일정id} 삭제 완료")
    else:
        await interaction.response.send_message(f"일정 #{일정id}을 찾지 못했습니다.", ephemeral=True)


@bot.tree.command(name="일정전체보기", description="등록된 전체 일정을 확인합니다.")
async def schedule_list(interaction: discord.Interaction) -> None:
    if interaction.guild_id is None:
        await interaction.response.send_message("서버 안에서만 사용할 수 있습니다.", ephemeral=True)
        return

    schedules = list_schedules(interaction.guild_id)
    if not schedules:
        await interaction.response.send_message("등록된 일정이 없습니다.", ephemeral=True)
        return

    lines = []
    for schedule in schedules:
        category = f"[{schedule.category}] " if schedule.category else ""
        lines.append(
            f"#{schedule.id} {category}{schedule.title}\n"
            f"시작: {format_dt(schedule.start_at, schedule.all_day)} / "
            f"종료: {format_dt(schedule.end_at, schedule.all_day)} / "
            f"알림: {schedule.reminder_minutes}분 전"
        )

    embed = discord.Embed(
        title="일정 전체보기",
        description="\n\n".join(lines),
        color=discord.Color.green(),
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="일정템플릿", description="일정 추가 입력 템플릿을 확인합니다.")
async def schedule_template(interaction: discord.Interaction) -> None:
    template = (
        "일정 추가 템플릿\n"
        "제목: 대회명 또는 일정명\n"
        "시작: 6월8일 14시\n"
        "종료: 6월8일 16시\n"
        "종일: 아니오\n"
        "미리알림분: 30\n"
        "설명: 필요한 내용"
    )
    await interaction.response.send_message(template, ephemeral=True)


@bot.tree.command(name="명령어", description="봇 명령어 목록을 확인합니다.")
async def command_help(interaction: discord.Interaction) -> None:
    embed = discord.Embed(
        title="명령어 목록",
        description="━━━━━━━━━━━━━━━━━━━━",
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="놀이",
        value=(
            "`/업다운` 숫자 맞히기\n"
            "`/타자게임` 빠른 타자 게임\n"
            "`/별명추천` 이상한 별명 추천\n"
            "`/명언랜덤` 서버 명언 뽑기\n"
            "`/오늘의운세` 랜덤 운세\n"
            "`/놀리기` 가벼운 장난 멘트\n"
            "`/투표` 반응 투표 생성"
        ),
        inline=False,
    )
    embed.add_field(
        name="━━━━━━━━━━━━━━━━━━━━",
        value="\u200b",
        inline=False,
    )
    embed.add_field(
        name="일반사용자",
        value=(
            "`/명령어` 명령어 목록\n"
            "`/내아이디` 내 Discord ID 확인\n"
            "`/서버정보` 서버 정보 확인\n"
            "`/일정추가` 일정 추가\n"
            "`/일정삭제` 일정 삭제\n"
            "`/일정전체보기` 일정 목록\n"
            "`/일정템플릿` 입력 예시\n"
            "`/대회등록` 대회 등록 및 일정 연동"
        ),
        inline=False,
    )
    embed.add_field(
        name="━━━━━━━━━━━━━━━━━━━━",
        value="\u200b",
        inline=False,
    )
    embed.add_field(
        name="관리자",
        value=(
            "`/권한목록` 봇관리자 목록\n"
            "`/공지` 한 줄 @everyone 공지\n"
            "`/공지작성` 여러 줄 @everyone 공지\n"
            "`/채팅도배` 같은 문자를 여러 번 전송\n"
            "`/역할만들기` 역할 생성\n"
            "`/역할지급` 멤버에게 역할 지급\n"
            "`/역할회수` 멤버에게서 역할 회수\n"
            "`/멤버추방` 멤버 추방\n"
            "`/멤버차단` 멤버 차단\n"
            "`/채팅금지` 멤버 타임아웃\n"
            "`/채팅허용` 멤버 타임아웃 해제\n"
            "`/명언등록` 서버 명언 저장\n"
            "`/경고` 유저 경고\n"
            "`/경고목록` 경고 확인\n"
            "`/경고삭제` 경고 삭제\n"
            "`/삭제로그` 삭제된 채팅 확인\n"
            "`/채널만들기` 텍스트 채널 생성\n"
            "`/채널삭제` 텍스트 채널 삭제\n"
            "`/메시지삭제` 최근 메시지 삭제\n"
            "`/메시지고정` 메시지 ID로 고정\n"
            "`/반응추가` 메시지에 이모지 반응 추가\n"
            "`/스레드만들기` 공개 스레드 생성\n"
            "`/스레드잠금` 스레드 잠금/해제\n"
            "`/이벤트만들기` 서버 이벤트 생성"
        ),
        inline=False,
    )
    embed.add_field(
        name="━━━━━━━━━━━━━━━━━━━━",
        value="\u200b",
        inline=False,
    )
    embed.add_field(
        name="Owner",
        value=(
            "`/권한주기` 봇관리자 권한 지급\n"
            "`/권한회수` 봇관리자 권한 회수\n"
            "`/ai모델` 현재 AI 모델 변경\n"
            "`/ai모델목록` 선택 가능한 AI 모델\n"
            "`/db테이블` DB 테이블 목록\n"
            "`/db조회` DB 테이블 조회\n"
            "`/db삭제` DB row_id 삭제\n"
            "`/db초기화` DB 테이블 비우기"
        ),
        inline=False,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="내아이디", description="내 Discord 사용자 ID를 확인합니다.")
async def my_id(interaction: discord.Interaction) -> None:
    owner_state = "등록됨" if is_bot_owner(interaction) else "등록 안 됨"
    manager_state = "등록됨" if is_bot_manager(interaction) else "등록 안 됨"
    await interaction.response.send_message(
        f"내 Discord ID: `{interaction.user.id}`\nBOT_OWNER_IDS 상태: {owner_state}\n봇관리자 상태: {manager_state}",
        ephemeral=True,
    )


@bot.tree.command(name="db테이블", description="DB 관리 가능 테이블 목록을 봅니다.")
@bot_owner_required()
async def db_tables(interaction: discord.Interaction) -> None:
    await interaction.response.send_message("관리 가능 테이블:\n" + "\n".join(f"- `{name}`" for name in db_table_names()), ephemeral=True)


@bot.tree.command(name="ai모델목록", description="선택 가능한 Gemini 모델 목록을 봅니다.")
@bot_owner_required()
async def ai_model_list(interaction: discord.Interaction) -> None:
    lines = [f"- `{model}`" for model in AVAILABLE_GEMINI_MODELS]
    lines.append(f"\n현재 모델: `{CURRENT_GEMINI_MODEL}`")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.command(name="ai모델", description="현재 Gemini 모델을 변경합니다.")
@bot_owner_required()
@app_commands.describe(모델명="예: gemini-3.5-flash")
async def ai_model_set(interaction: discord.Interaction, 모델명: str) -> None:
    global CURRENT_GEMINI_MODEL
    if 모델명 not in AVAILABLE_GEMINI_MODELS:
        await interaction.response.send_message(
            "허용되지 않은 모델입니다.\n" + "\n".join(f"- `{model}`" for model in AVAILABLE_GEMINI_MODELS),
            ephemeral=True,
        )
        return
    CURRENT_GEMINI_MODEL = 모델명
    await interaction.response.send_message(f"현재 AI 모델을 `{CURRENT_GEMINI_MODEL}`로 변경했습니다.", ephemeral=True)


@bot.tree.command(name="db조회", description="DB 테이블 데이터를 조회합니다.")
@bot_owner_required()
@app_commands.describe(테이블="조회할 테이블명", 개수="조회 개수. 1~20")
async def db_view(interaction: discord.Interaction, 테이블: str, 개수: app_commands.Range[int, 1, 20] = 10) -> None:
    try:
        rows = db_fetch_rows(테이블, 개수)
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return
    await interaction.response.send_message(f"`{테이블}` 조회 결과\n```text\n{format_db_rows(rows)}\n```", ephemeral=True)


@bot.tree.command(name="db삭제", description="DB row_id로 한 줄을 삭제합니다.")
@bot_owner_required()
@app_commands.describe(테이블="삭제할 테이블명", row_id="db조회에 표시되는 row_id")
async def db_delete(interaction: discord.Interaction, 테이블: str, row_id: int) -> None:
    try:
        count = db_delete_row(테이블, row_id)
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return
    await interaction.response.send_message(f"`{테이블}` row_id `{row_id}` 삭제 완료: {count}개", ephemeral=True)


@bot.tree.command(name="db초기화", description="DB 테이블 전체를 비웁니다.")
@bot_owner_required()
@app_commands.describe(테이블="초기화할 테이블명", 확인="초기화 라고 입력해야 실행됩니다.")
async def db_clear(interaction: discord.Interaction, 테이블: str, 확인: str) -> None:
    if 확인 != "초기화":
        await interaction.response.send_message("실행하려면 확인 값에 `초기화`를 입력하세요.", ephemeral=True)
        return
    try:
        count = db_clear_table(테이블)
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return
    await interaction.response.send_message(f"`{테이블}` 테이블 초기화 완료: {count}개 삭제", ephemeral=True)


@bot.tree.command(name="업다운", description="1~100 숫자 맞히기 게임을 시작하거나 추측합니다.")
@app_commands.describe(추측="추측할 숫자. 비워두면 새 게임을 시작합니다.")
async def updown(interaction: discord.Interaction, 추측: Optional[app_commands.Range[int, 1, 100]] = None) -> None:
    key = interaction.channel_id or 0
    if 추측 is None:
        ACTIVE_UPDOWN_GAMES[key] = random.randint(1, 100)
        await interaction.response.send_message("업다운 시작! 이 채널에서 모두 같이 1~100 사이 숫자를 맞혀보세요. `/업다운 추측:숫자`")
        return

    answer = ACTIVE_UPDOWN_GAMES.get(key)
    if answer is None:
        ACTIVE_UPDOWN_GAMES[key] = random.randint(1, 100)
        await interaction.response.send_message("진행 중인 게임이 없어서 새 게임을 시작했습니다. 다시 추측해보세요.")
        return

    if 추측 == answer:
        ACTIVE_UPDOWN_GAMES.pop(key, None)
        await interaction.response.send_message(f"{interaction.user.mention} 정답! 숫자는 {answer}였습니다.")
    elif 추측 < answer:
        await interaction.response.send_message("UP")
    else:
        await interaction.response.send_message("DOWN")


@bot.tree.command(name="타자게임", description="문장을 가장 빨리 정확히 치는 사람이 승리합니다.")
@app_commands.describe(문장="따라 칠 문장")
async def typing_game(interaction: discord.Interaction, 문장: app_commands.Range[str, 1, 300]) -> None:
    if interaction.channel_id is None:
        await interaction.response.send_message("채널에서만 사용할 수 있습니다.", ephemeral=True)
        return
    ACTIVE_TYPING_GAMES[interaction.channel_id] = (str(문장).strip(), utc_now())
    await interaction.response.send_message(f"타자게임 시작!\n정확히 입력하세요:\n```text\n{문장}\n```")


@bot.tree.command(name="별명추천", description="유저에게 이상한 별명을 추천합니다.")
@app_commands.describe(유저="별명을 추천할 유저")
async def nickname_suggest(interaction: discord.Interaction, 유저: discord.Member) -> None:
    prefixes = ["반짝이는", "수상한", "전설의", "잠 덜 깬", "고장난", "비밀스러운", "급발진", "우당탕"]
    nouns = ["감자", "마법사", "대장", "챔피언", "버튼", "토스트", "천재", "보스"]
    nickname = f"{random.choice(prefixes)} {random.choice(nouns)}"
    await interaction.response.send_message(f"{유저.mention}님의 추천 별명: **{nickname}**")


@bot.tree.command(name="오늘의운세", description="오늘의 랜덤 운세와 점수를 봅니다.")
async def fortune(interaction: discord.Interaction) -> None:
    fortunes = [
        "기회가 굴러옵니다. 발로 차지만 마세요.",
        "작은 실수가 큰 웃음으로 바뀝니다.",
        "오늘은 한 번쯤 과감해도 됩니다.",
        "기다리던 답이 조금 늦게 옵니다.",
        "팀플에서 빛나는 날입니다.",
        "집중력이 생각보다 오래 갑니다.",
    ]
    score = random.randint(1, 100)
    await interaction.response.send_message(f"오늘의 운세: {random.choice(fortunes)}\n운세 점수: **{score}점**")


@bot.tree.command(name="놀리기", description="조금 더 독한 랜덤 장난 멘트를 보냅니다.")
@app_commands.describe(유저="놀릴 유저")
async def tease(interaction: discord.Interaction, 유저: discord.Member) -> None:
    lines = [
        "오늘도 자신감은 풀충전인데 결과는 업데이트가 필요합니다.",
        "진지하게 말하면 잘하는데, 시작 전에 늘 한 번 흔들립니다.",
        "폼은 프로인데 마무리는 늘 예능으로 갑니다.",
        "가만히 있어도 존재감이 큰데, 실수하면 전설이 됩니다.",
        "오늘의 평가: 잠재력은 높은데 행동이 늘 스포일러입니다.",
        "열심히는 하는데 이상하게 주변 사람 심장을 같이 시험합니다.",
        "한 줄 요약: 실력은 있는데 운영체제가 가끔 멋대로입니다.",
        "분위기 메이커인지 사고 메이커인지는 아직 판별 중입니다.",
    ]
    await interaction.response.send_message(f"{유저.mention} {random.choice(lines)}")


@bot.tree.command(name="투표", description="반응 투표를 만듭니다.")
@app_commands.describe(제목="투표 제목", 선택지들="쉼표로 구분. 예: 참가,불참,미정")
async def poll(interaction: discord.Interaction, 제목: str, 선택지들: str) -> None:
    options = split_options(선택지들)
    if not 2 <= len(options) <= 10:
        await interaction.response.send_message("선택지는 2~10개로 입력하세요. 예: 참가,불참,미정", ephemeral=True)
        return
    description = "\n".join(f"{POLL_EMOJIS[index]} {option}" for index, option in enumerate(options))
    embed = discord.Embed(title=제목, description=description, color=discord.Color.green())
    await interaction.response.send_message(embed=embed)
    message = await interaction.original_response()
    for emoji in POLL_EMOJIS[: len(options)]:
        await message.add_reaction(emoji)


@bot.tree.command(name="권한주기", description="유저에게 봇관리자 권한을 줍니다.")
@bot_owner_required()
@app_commands.describe(유저="봇관리자 권한을 줄 유저")
async def bot_admin_grant(interaction: discord.Interaction, 유저: discord.Member) -> None:
    if interaction.guild_id is None:
        await interaction.response.send_message("서버 안에서만 사용할 수 있습니다.", ephemeral=True)
        return

    already_admin = bot_admin_exists(interaction.guild_id, 유저.id)
    grant_bot_admin(interaction.guild_id, 유저.id, interaction.user.id)
    dm_status = ""
    try:
        if already_admin:
            await 유저.send(
                f"{interaction.guild.name} 서버의 봇관리자 권한이 갱신되었습니다.\n"
                "권한은 계속 유지됩니다."
            )
        else:
            await 유저.send(
                f"{interaction.guild.name} 서버에서 봇관리자 권한을 받았습니다.\n"
                "이제 `/권한목록`, `/공지`, `/공지작성`, `/역할만들기`, `/역할지급`, `/역할회수`, `/멤버추방`, `/멤버차단`을 사용할 수 있습니다."
            )
    except discord.Forbidden:
        dm_status = "\nDM을 보낼 수 없어 개인 알림은 전달하지 못했습니다."

    action = "봇관리자 권한을 갱신했습니다" if already_admin else "봇관리자 권한을 받았습니다"
    await interaction.response.send_message(
        f"{유저.mention}이(가) {action}.{dm_status}",
        ephemeral=True,
    )


@bot.tree.command(name="권한회수", description="유저의 봇관리자 권한을 회수합니다.")
@bot_owner_required()
@app_commands.describe(유저="봇관리자 권한을 회수할 유저")
async def bot_admin_revoke(interaction: discord.Interaction, 유저: discord.Member) -> None:
    if interaction.guild_id is None:
        await interaction.response.send_message("서버 안에서만 사용할 수 있습니다.", ephemeral=True)
        return

    if revoke_bot_admin(interaction.guild_id, 유저.id):
        dm_status = ""
        try:
            await 유저.send(f"{interaction.guild.name} 서버의 봇관리자 권한이 회수되었습니다.")
        except discord.Forbidden:
            dm_status = "\nDM을 보낼 수 없어 개인 알림은 전달하지 못했습니다."
        await interaction.response.send_message(f"{유저.mention}의 봇관리자 권한을 회수했습니다.{dm_status}", ephemeral=True)
    else:
        await interaction.response.send_message(f"{유저.mention}은 봇관리자로 등록되어 있지 않습니다.", ephemeral=True)


@bot.tree.command(name="권한목록", description="봇관리자 권한을 가진 사람 목록을 봅니다.")
@bot_manager_required()
async def bot_admin_list(interaction: discord.Interaction) -> None:
    if interaction.guild_id is None:
        await interaction.response.send_message("서버 안에서만 사용할 수 있습니다.", ephemeral=True)
        return

    owner_mentions = [f"<@{owner_id}> `owner`" for owner_id in sorted(BOT_OWNER_IDS)]
    admin_mentions = [f"<@{user_id}> `bot_admin`" for user_id in list_bot_admin_ids(interaction.guild_id)]
    lines = owner_mentions + admin_mentions
    if not lines:
        lines = ["등록된 권한자가 없습니다. `.env`의 BOT_OWNER_IDS를 먼저 설정하세요."]

    embed = discord.Embed(
        title="봇 권한 목록",
        description="\n".join(lines),
        color=discord.Color.blurple(),
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="공지", description="@everyone 공지를 지정 채널에 보냅니다.")
@bot_manager_required()
@app_commands.describe(채널="공지할 채널", 내용="공지 내용")
async def notice(interaction: discord.Interaction, 채널: discord.TextChannel, 내용: str) -> None:
    await 채널.send(
        content=f"@everyone\n{as_blockquote(내용)}",
        allowed_mentions=discord.AllowedMentions(everyone=True),
    )
    await interaction.response.send_message(f"{채널.mention}에 공지를 보냈습니다.", ephemeral=True)


@bot.tree.command(name="공지작성", description="여러 줄 공지를 작성 창으로 입력해서 보냅니다.")
@bot_manager_required()
@app_commands.describe(채널="공지할 채널")
async def notice_modal(interaction: discord.Interaction, 채널: discord.TextChannel) -> None:
    await interaction.response.send_modal(NoticeModal(채널))


@bot.tree.command(name="채팅도배", description="현재 채널에 같은 문자를 여러 번 보냅니다.")
@bot_manager_required()
@app_commands.describe(횟수="전송 횟수. 최대 100회", 문자="전송할 문자")
async def chat_repeat(
    interaction: discord.Interaction,
    횟수: app_commands.Range[int, 1, 100],
    문자: app_commands.Range[str, 1, 500],
) -> None:
    if not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("텍스트 채널에서만 사용할 수 있습니다.", ephemeral=True)
        return

    await interaction.response.send_message(f"{횟수}회 전송을 시작합니다.", ephemeral=True)
    for _ in range(횟수):
        await interaction.channel.send(문자, allowed_mentions=discord.AllowedMentions.none())
        await asyncio.sleep(0.005)


@bot.tree.command(name="명언등록", description="서버 명언을 등록합니다.")
@bot_manager_required()
@app_commands.describe(유저="명언을 남긴 유저", 멘트="저장할 명언")
async def quote_add(interaction: discord.Interaction, 유저: discord.Member, 멘트: app_commands.Range[str, 1, 500]) -> None:
    if interaction.guild_id is None:
        await interaction.response.send_message("서버 안에서만 사용할 수 있습니다.", ephemeral=True)
        return
    quote_id = add_quote(interaction.guild_id, 유저.id, str(멘트), interaction.user.id)
    await interaction.response.send_message(f"명언 #{quote_id} 등록 완료: {유저.mention} - “{멘트}”")


@bot.tree.command(name="명언랜덤", description="등록된 서버 명언을 랜덤으로 뽑습니다.")
async def quote_random(interaction: discord.Interaction) -> None:
    if interaction.guild_id is None:
        await interaction.response.send_message("서버 안에서만 사용할 수 있습니다.", ephemeral=True)
        return
    quote = random_quote(interaction.guild_id)
    if quote is None:
        await interaction.response.send_message("등록된 명언이 없습니다.")
        return
    await interaction.response.send_message(f"명언 #{quote['id']}\n<@{quote['user_id']}>: “{quote['quote']}”")


@bot.tree.command(name="경고", description="유저에게 경고를 부여합니다.")
@bot_manager_required()
@app_commands.describe(유저="경고할 유저", 사유="경고 사유")
async def warn_user(interaction: discord.Interaction, 유저: discord.Member, 사유: app_commands.Range[str, 1, 500]) -> None:
    if interaction.guild_id is None:
        await interaction.response.send_message("서버 안에서만 사용할 수 있습니다.", ephemeral=True)
        return
    warning_id = add_warning(interaction.guild_id, 유저.id, str(사유), interaction.user.id)
    count = len(list_warnings(interaction.guild_id, 유저.id))
    if count >= 3 and 유저.id not in BOT_OWNER_IDS:
        try:
            await 유저.kick(reason=f"경고 3회 누적: {사유}")
        except discord.Forbidden:
            await interaction.response.send_message(
                f"{유저.mention} 경고 #{warning_id} 등록 완료. 누적 {count}회\n"
                "경고 3회라 자동 강퇴하려 했지만 봇 권한 또는 역할 순서 때문에 실패했습니다.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"{유저} 경고 #{warning_id} 등록 완료. 누적 {count}회로 자동 강퇴했습니다.\n사유: {사유}"
        )
        if isinstance(interaction.channel, discord.abc.Messageable):
            await interaction.channel.send(f"{유저.mention}님 경고 3회 누적으로 잘가요~")
        return

    if count >= 3 and 유저.id in BOT_OWNER_IDS:
        await interaction.response.send_message(
            f"{유저.mention} 경고 #{warning_id} 등록 완료. 누적 {count}회\n"
            "단, owner는 자동 강퇴하지 않습니다.\n"
            f"사유: {사유}"
        )
        return

    await interaction.response.send_message(f"{유저.mention} 경고 #{warning_id} 등록 완료. 누적 {count}회\n사유: {사유}")


@bot.tree.command(name="경고목록", description="유저별 경고 목록을 봅니다.")
@bot_manager_required()
@app_commands.describe(유저="특정 유저만 확인하려면 선택")
async def warning_list(interaction: discord.Interaction, 유저: Optional[discord.Member] = None) -> None:
    if interaction.guild_id is None:
        await interaction.response.send_message("서버 안에서만 사용할 수 있습니다.", ephemeral=True)
        return

    if 유저 is None:
        rows = list_warnings(interaction.guild_id)
        if not rows:
            await interaction.response.send_message("등록된 경고가 없습니다.", ephemeral=True)
            return
        description = "\n".join(f"<@{row['user_id']}>: {row['count']}회" for row in rows)
    else:
        rows = list_warnings(interaction.guild_id, 유저.id)
        if not rows:
            await interaction.response.send_message(f"{유저.mention}의 경고가 없습니다.", ephemeral=True)
            return
        description = "\n".join(f"#{row['id']} {row['reason']} - <@{row['warned_by']}>" for row in rows)

    embed = discord.Embed(title="경고 목록", description=description, color=discord.Color.orange())
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="경고삭제", description="경고 ID로 경고를 삭제합니다.")
@bot_manager_required()
@app_commands.describe(경고id="삭제할 경고 ID")
async def warning_delete(interaction: discord.Interaction, 경고id: int) -> None:
    if interaction.guild_id is None:
        await interaction.response.send_message("서버 안에서만 사용할 수 있습니다.", ephemeral=True)
        return
    if delete_warning(interaction.guild_id, 경고id):
        await interaction.response.send_message(f"경고 #{경고id} 삭제 완료", ephemeral=True)
    else:
        await interaction.response.send_message(f"경고 #{경고id}를 찾지 못했습니다.", ephemeral=True)


@bot.tree.command(name="삭제로그", description="최근 삭제된 채팅을 봅니다.")
@bot_manager_required()
@app_commands.describe(개수="확인할 개수. 1~10")
async def deleted_log(interaction: discord.Interaction, 개수: app_commands.Range[int, 1, 10] = 5) -> None:
    if interaction.guild_id is None:
        await interaction.response.send_message("서버 안에서만 사용할 수 있습니다.", ephemeral=True)
        return
    rows = list_deleted_messages(interaction.guild_id, 개수)
    if not rows:
        await interaction.response.send_message("기록된 삭제 채팅이 없습니다.")
        return
    lines = []
    for row in rows:
        deleted_at = from_utc_iso(row["deleted_at"]).astimezone(local_tz()).strftime("%m-%d %H:%M")
        lines.append(
            f"#{row['id']} [{deleted_at}] <#{row['channel_id']}> {row['author_name']}:\n{row['content'][:300]}"
        )
    embed = discord.Embed(title="삭제된 채팅 로그", description="\n\n".join(lines), color=discord.Color.red())
    await interaction.response.send_message(embed=embed)


@notice.error
async def notice_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message("공지 명령어는 봇관리자 권한이 필요합니다.", ephemeral=True)
        return
    raise error


@bot.tree.command(name="서버정보", description="서버 정보를 확인합니다.")
async def server_info(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("서버 안에서만 사용할 수 있습니다.", ephemeral=True)
        return

    guild = interaction.guild
    owner = guild.owner.mention if guild.owner else "알 수 없음"
    embed = discord.Embed(title=guild.name, color=discord.Color.blurple())
    embed.add_field(name="서버 ID", value=str(guild.id), inline=True)
    embed.add_field(name="소유자", value=owner, inline=True)
    embed.add_field(name="멤버 수", value=str(guild.member_count or len(guild.members)), inline=True)
    embed.add_field(name="채널 수", value=str(len(guild.channels)), inline=True)
    embed.add_field(name="역할 수", value=str(len(guild.roles)), inline=True)
    embed.add_field(name="생성일", value=guild.created_at.strftime("%Y-%m-%d"), inline=True)
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="채널만들기", description="텍스트 채널을 만듭니다.")
@owner_or_permissions(manage_channels=True)
@app_commands.describe(이름="새 채널 이름", 카테고리="선택 카테고리")
async def channel_create(
    interaction: discord.Interaction,
    이름: str,
    카테고리: Optional[discord.CategoryChannel] = None,
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("서버 안에서만 사용할 수 있습니다.", ephemeral=True)
        return

    channel = await interaction.guild.create_text_channel(
        name=이름,
        category=카테고리,
        reason=f"{interaction.user} used /채널만들기",
    )
    await interaction.response.send_message(f"채널 생성 완료: {channel.mention}", ephemeral=True)


@bot.tree.command(name="채널삭제", description="채널을 삭제합니다.")
@owner_or_permissions(manage_channels=True)
@app_commands.describe(채널="삭제할 채널")
async def channel_delete(interaction: discord.Interaction, 채널: discord.TextChannel) -> None:
    channel_name = 채널.name
    await 채널.delete(reason=f"{interaction.user} used /채널삭제")
    await interaction.response.send_message(f"채널 삭제 완료: #{channel_name}", ephemeral=True)


@bot.tree.command(name="역할만들기", description="역할을 만듭니다.")
@bot_manager_required()
@app_commands.describe(이름="새 역할 이름", 색상="HEX 색상. 예: #5865F2")
async def role_create(interaction: discord.Interaction, 이름: str, 색상: Optional[str] = None) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("서버 안에서만 사용할 수 있습니다.", ephemeral=True)
        return

    color = discord.Color.default()
    if 색상:
        hex_color = 색상.strip().lstrip("#")
        if not re.fullmatch(r"[0-9a-fA-F]{6}", hex_color):
            await interaction.response.send_message("색상은 #5865F2 같은 HEX 형식으로 입력하세요.", ephemeral=True)
            return
        color = discord.Color(int(hex_color, 16))

    role = await interaction.guild.create_role(
        name=이름,
        color=color,
        reason=f"{interaction.user} used /역할만들기",
    )
    await interaction.response.send_message(f"역할 생성 완료: {role.mention}", ephemeral=True)


@bot.tree.command(name="역할지급", description="멤버에게 역할을 지급합니다.")
@bot_manager_required()
@app_commands.describe(멤버="역할을 받을 멤버", 역할="지급할 역할")
async def role_add(interaction: discord.Interaction, 멤버: discord.Member, 역할: discord.Role) -> None:
    await 멤버.add_roles(역할, reason=f"{interaction.user} used /역할지급")
    await interaction.response.send_message(f"{멤버.mention}에게 {역할.mention} 역할을 지급했습니다.", ephemeral=True)


@bot.tree.command(name="역할회수", description="멤버에게서 역할을 회수합니다.")
@bot_manager_required()
@app_commands.describe(멤버="역할을 회수할 멤버", 역할="회수할 역할")
async def role_remove(interaction: discord.Interaction, 멤버: discord.Member, 역할: discord.Role) -> None:
    await 멤버.remove_roles(역할, reason=f"{interaction.user} used /역할회수")
    await interaction.response.send_message(f"{멤버.mention}에게서 {역할.mention} 역할을 회수했습니다.", ephemeral=True)


@bot.tree.command(name="멤버추방", description="멤버를 서버에서 추방합니다.")
@bot_manager_required()
@app_commands.describe(멤버="추방할 멤버", 사유="선택 사유")
async def member_kick(interaction: discord.Interaction, 멤버: discord.Member, 사유: Optional[str] = None) -> None:
    if 멤버.id in BOT_OWNER_IDS:
        await interaction.response.send_message("봇 관리자(owner)는 추방할 수 없습니다.", ephemeral=True)
        return
    farewell = f"{멤버.mention}님 잘가요~"
    await 멤버.kick(reason=사유 or f"{interaction.user} used /멤버추방")
    await interaction.response.send_message(f"{멤버} 추방 완료", ephemeral=True)
    if isinstance(interaction.channel, discord.abc.Messageable):
        await interaction.channel.send(
            farewell,
            allowed_mentions=discord.AllowedMentions(users=True),
        )


@bot.tree.command(name="멤버차단", description="멤버를 서버에서 차단합니다.")
@bot_manager_required()
@app_commands.describe(멤버="차단할 멤버", 사유="선택 사유")
async def member_ban(interaction: discord.Interaction, 멤버: discord.Member, 사유: Optional[str] = None) -> None:
    if 멤버.id in BOT_OWNER_IDS:
        await interaction.response.send_message("봇 관리자(owner)는 차단할 수 없습니다.", ephemeral=True)
        return
    await 멤버.ban(reason=사유 or f"{interaction.user} used /멤버차단")
    await interaction.response.send_message(f"{멤버} 차단 완료", ephemeral=True)
    if isinstance(interaction.channel, discord.abc.Messageable):
        msg = f"{interaction.user.mention}님이 {멤버.mention}님을 **차단**했습니다."
        if 사유:
            msg += f"\n사유: {사유}"
        await interaction.channel.send(msg, allowed_mentions=discord.AllowedMentions(users=True))


@bot.tree.command(name="채팅금지", description="멤버를 지정한 시간 동안 채팅금지합니다.")
@bot_manager_required()
@app_commands.describe(
    멤버="채팅금지할 멤버",
    시간분="채팅금지 시간. 1~10080분",
    사유="선택 사유",
)
async def member_timeout(
    interaction: discord.Interaction,
    멤버: discord.Member,
    시간분: app_commands.Range[int, 1, 10080],
    사유: Optional[str] = None,
) -> None:
    if 멤버.id in BOT_OWNER_IDS:
        await interaction.response.send_message("봇 관리자(owner)는 채팅금지할 수 없습니다.", ephemeral=True)
        return
    until = discord.utils.utcnow() + timedelta(minutes=시간분)
    await 멤버.timeout(until, reason=사유 or f"{interaction.user} used /채팅금지")
    await interaction.response.send_message(f"{멤버.mention} 채팅금지 완료: {시간분}분", ephemeral=True)
    if isinstance(interaction.channel, discord.abc.Messageable):
        msg = f"{interaction.user.mention}님이 {멤버.mention}님을 **{시간분}분 채팅금지**했습니다."
        if 사유:
            msg += f"\n사유: {사유}"
        await interaction.channel.send(msg, allowed_mentions=discord.AllowedMentions(users=True))


@bot.tree.command(name="채팅허용", description="멤버의 채팅금지를 해제합니다.")
@bot_manager_required()
@app_commands.describe(멤버="채팅금지를 해제할 멤버", 사유="선택 사유")
async def member_timeout_clear(
    interaction: discord.Interaction,
    멤버: discord.Member,
    사유: Optional[str] = None,
) -> None:
    await 멤버.timeout(None, reason=사유 or f"{interaction.user} used /채팅허용")
    await interaction.response.send_message(f"{멤버.mention} 채팅금지 해제 완료", ephemeral=True)
    if isinstance(interaction.channel, discord.abc.Messageable):
        msg = f"{interaction.user.mention}님이 {멤버.mention}님의 **채팅금지를 해제**했습니다."
        if 사유:
            msg += f"\n사유: {사유}"
        await interaction.channel.send(msg, allowed_mentions=discord.AllowedMentions(users=True))


@bot.tree.command(name="메시지삭제", description="최근 메시지를 여러 개 삭제합니다.")
@owner_or_permissions(manage_messages=True)
@app_commands.describe(개수="삭제할 최근 메시지 수. 1~100")
async def message_purge(interaction: discord.Interaction, 개수: app_commands.Range[int, 1, 100]) -> None:
    if not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("텍스트 채널에서만 사용할 수 있습니다.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=개수, reason=f"{interaction.user} used /메시지삭제")
    await interaction.followup.send(f"메시지 {len(deleted)}개 삭제 완료", ephemeral=True)


@bot.tree.command(name="메시지고정", description="메시지 ID로 메시지를 고정합니다.")
@owner_or_permissions(manage_messages=True)
@app_commands.describe(채널="메시지가 있는 채널", 메시지id="고정할 메시지 ID")
async def message_pin(interaction: discord.Interaction, 채널: discord.TextChannel, 메시지id: str) -> None:
    try:
        message = await 채널.fetch_message(int(메시지id))
    except (ValueError, discord.NotFound):
        await interaction.response.send_message("메시지를 찾지 못했습니다.", ephemeral=True)
        return

    await message.pin(reason=f"{interaction.user} used /메시지고정")
    await interaction.response.send_message("메시지 고정 완료", ephemeral=True)


@bot.tree.command(name="반응추가", description="메시지에 반응을 추가합니다.")
@owner_or_permissions(add_reactions=True)
@app_commands.describe(채널="메시지가 있는 채널", 메시지id="반응을 추가할 메시지 ID", 이모지="예: ✅")
async def reaction_add(interaction: discord.Interaction, 채널: discord.TextChannel, 메시지id: str, 이모지: str) -> None:
    try:
        message = await 채널.fetch_message(int(메시지id))
    except (ValueError, discord.NotFound):
        await interaction.response.send_message("메시지를 찾지 못했습니다.", ephemeral=True)
        return

    await message.add_reaction(이모지)
    await interaction.response.send_message("반응 추가 완료", ephemeral=True)


@bot.tree.command(name="스레드만들기", description="현재 채널에 공개 스레드를 만듭니다.")
@owner_or_permissions(create_public_threads=True)
@app_commands.describe(이름="스레드 이름")
async def thread_create(interaction: discord.Interaction, 이름: str) -> None:
    if not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("텍스트 채널에서만 사용할 수 있습니다.", ephemeral=True)
        return

    thread = await interaction.channel.create_thread(
        name=이름,
        type=discord.ChannelType.public_thread,
        reason=f"{interaction.user} used /스레드만들기",
    )
    await interaction.response.send_message(f"스레드 생성 완료: {thread.mention}", ephemeral=True)


@bot.tree.command(name="스레드잠금", description="스레드를 잠그거나 해제합니다.")
@owner_or_permissions(manage_threads=True)
@app_commands.describe(스레드="대상 스레드", 잠금="true면 잠금, false면 해제")
async def thread_lock(interaction: discord.Interaction, 스레드: discord.Thread, 잠금: bool = True) -> None:
    await 스레드.edit(locked=잠금, reason=f"{interaction.user} used /스레드잠금")
    state = "잠금" if 잠금 else "잠금 해제"
    await interaction.response.send_message(f"{스레드.mention} {state} 완료", ephemeral=True)


@bot.tree.command(name="이벤트만들기", description="디스코드 서버 이벤트를 만듭니다.")
@owner_or_permissions(manage_events=True)
@app_commands.describe(
    이름="이벤트 이름",
    시작="예: 6월8일 14시, 2026-06-08 14:00",
    종료="예: 6월8일 16시, 2026-06-08 16:00",
    장소="외부 장소 또는 접속 링크",
    설명="선택 설명",
)
async def guild_event_create(
    interaction: discord.Interaction,
    이름: str,
    시작: str,
    종료: str,
    장소: str,
    설명: Optional[str] = None,
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("서버 안에서만 사용할 수 있습니다.", ephemeral=True)
        return

    try:
        start_at = parse_datetime(시작).astimezone(timezone.utc)
        end_at = parse_datetime(종료).astimezone(timezone.utc)
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return

    if end_at <= start_at:
        await interaction.response.send_message("종료 시간은 시작 시간보다 뒤여야 합니다.", ephemeral=True)
        return

    event = await interaction.guild.create_scheduled_event(
        name=이름,
        start_time=start_at,
        end_time=end_at,
        description=설명,
        entity_type=discord.EntityType.external,
        privacy_level=discord.PrivacyLevel.guild_only,
        location=장소,
        reason=f"{interaction.user} used /이벤트만들기",
    )
    await interaction.response.send_message(f"서버 이벤트 생성 완료: {event.name}", ephemeral=True)


@bot.tree.error
async def app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    logger.exception("Command failed: %s", interaction.command, exc_info=error)
    if isinstance(error, app_commands.MissingPermissions):
        message = "이 명령어를 사용할 권한이 없습니다."
    elif isinstance(error, app_commands.CheckFailure):
        message = "이 명령어를 사용할 권한이 없습니다. BOT_OWNER_IDS에 등록된 owner이거나 /권한주기로 등록된 봇관리자여야 합니다."
    elif isinstance(error, app_commands.BotMissingPermissions):
        message = "봇에게 필요한 권한이 없습니다. 초대 권한과 서버 내 역할 순서를 확인하세요."
    elif isinstance(error, app_commands.CommandInvokeError) and isinstance(error.original, discord.Forbidden):
        message = "디스코드가 작업을 거부했습니다. 봇 권한 또는 역할 순서를 확인하세요."
    elif isinstance(error, discord.Forbidden):
        message = "디스코드가 작업을 거부했습니다. 봇 권한 또는 역할 순서를 확인하세요."
    else:
        message = f"명령어 실행 중 오류가 발생했습니다: {error}"

    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


@bot.tree.command(name="대회등록", description="대회를 등록하고 일정에 연동합니다.")
@app_commands.describe(
    분야="예: 게임, 코딩, 디자인",
    제목="대회명",
    시작="예: 6월8일 14시, 2026-06-08 14:00",
    종료="예: 6월8일 16시, 2026-06-08 16:00",
    종일="종일 대회 여부",
    미리알림분="시작 몇 분 전에 알림을 보낼지",
    세부설명="대회 세부 설명",
)
async def tournament_add(
    interaction: discord.Interaction,
    분야: str,
    제목: str,
    시작: str,
    종료: str,
    종일: bool = False,
    미리알림분: int = 60,
    세부설명: Optional[str] = None,
) -> None:
    if interaction.guild_id is None:
        await interaction.response.send_message("서버 안에서만 사용할 수 있습니다.", ephemeral=True)
        return

    try:
        start_at = parse_datetime(시작, 종일)
        end_at = parse_datetime(종료, 종일)
        schedule_id = add_schedule(
            interaction.guild_id,
            interaction.channel_id,
            제목,
            start_at,
            end_at,
            종일,
            미리알림분,
            interaction.user.id,
            세부설명,
            분야,
        )
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return

    created = get_schedule(interaction.guild_id, schedule_id)
    if created is None:
        await interaction.response.send_message("대회는 저장됐지만 다시 불러오지 못했습니다.", ephemeral=True)
        return
    await interaction.response.send_message(embed=make_schedule_embed(created, "대회 등록 완료"))


async def main() -> None:
    if not TOKEN:
        raise RuntimeError(".env 파일에 DISCORD_TOKEN을 설정하세요.")
    await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
