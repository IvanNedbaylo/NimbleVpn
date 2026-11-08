# bot.py
from __future__ import annotations

import asyncio
import logging
import csv
import html
import io
import random
import secrets
import string
import time
from datetime import datetime, timedelta
from urllib.parse import urlparse, unquote

import aiohttp
import aiosqlite

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# ==================== НАСТРОЙКИ ====================
BOT_TOKEN = "9029529148:AAGaqz8QU9P89sV32MZMPqIpxNrd4oufynY"
ADMIN_IDS = [8583735877, 832696725]

BRAND = "NimbleVPN"
BRAND_TITLE = "Nimble VPN"

# Канал для проверки подписки
CHANNEL_ID = "@FreeNimbleVpn"
DB_PATH = "bot.db"

# --- ССЫЛКИ ---
CHANNEL_LINK = f"https://t.me/{CHANNEL_ID.lstrip('@')}"
# Ссылка на вашу поддержку
SUPPORT_LINK = "https://t.me/NimbleVpnSupport"
CHAT_LINK = "https://t.me/+O9-ePIXJYO05ZWVi"

# --- ПЕРСОНАЛЬНЫЕ ПОДПИСКИ ---
SITE_URL = "https://ivannedbaylo.github.io/NimbleVpn/"
WORKER_BASE = "https://nimblevpn-sub.i-a-nedbaylo.workers.dev"
WORKER_API_KEY = "nvpn-6ac6e0142bb5247da0e46117ba4f48fc"
DEVICE_LIMIT = 3

# --- ССЫЛКИ НА ПРИЛОЖЕНИЯ ---
HAPP_ANDROID_URL = "https://play.google.com/store/apps/details?id=com.happproxy"
INCY_IOS_URL = "https://apps.apple.com/ru/app/incy/id6756943388"
HAPP_WINDOWS_URL = "https://happ.su"
HAPP_LINUX_URL = "https://happ.su"

# Документы для банка (по шаблонам от платежной системы)
DOC_PRIVACY_URL = "https://telegra.ph/POLITIKA-KONFIDENCIALNOSTI-08-12-99"
DOC_TERMS_URL = "https://telegra.ph/PUBLICHNAYA-OFERTA-08-12-15"

# Глобальные переменные
MAINTENANCE_MODE = False
ANNOUNCEMENT_TEXT = ""

USERS_PER_PAGE = 20
KEY_COOLDOWN_SEC = 60
KEY_DAILY_LIMIT = 20
KEYS_PER_PAGE = 6

MONITOR_ENABLED = True
MONITOR_INTERVAL_SEC = 300
MONITOR_FIRST_DELAY_SEC = 20

REF_TARGET = 3
REF_SHARE_TEXT = f"Бесплатный VPN {BRAND} — подключайся"
BOT_USERNAME = ""
# ===================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger(__name__)
router = Router()

# ==================== БАЗА ДАННЫХ ====================

async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=5000")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
                created_at TEXT NOT NULL, is_banned INTEGER DEFAULT 0,
                keys_issued INTEGER DEFAULT 0
            )
        """)
        cursor = await db.execute("PRAGMA table_info(users)")
        cols = [col[1] for col in await cursor.fetchall()]
        if "keys_issued" not in cols:
            await db.execute("ALTER TABLE users ADD COLUMN keys_issued INTEGER DEFAULT 0")
        if "referred_by" not in cols:
            await db.execute("ALTER TABLE users ADD COLUMN referred_by INTEGER")
        if "sub_token" not in cols:
            await db.execute("ALTER TABLE users ADD COLUMN sub_token TEXT")
            cursor = await db.execute(
                "SELECT user_id FROM users WHERE sub_token IS NULL OR sub_token = ''"
            )
            old_users = await cursor.fetchall()
            for (uid,) in old_users:
                await db.execute(
                    "UPDATE users SET sub_token = ? WHERE user_id = ?",
                    (secrets.token_hex(16), uid),
                )
            if old_users:
                logger.info("Выданы персональные токены: %s пользователям", len(old_users))
        
        await db.execute("""
            CREATE TABLE IF NOT EXISTS key_issues (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                keys_count INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                ts REAL NOT NULL
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_key_issues_user_ts ON key_issues(user_id, ts)"
        )
        await db.execute("CREATE INDEX IF NOT EXISTS idx_users_ref ON users(referred_by)")
        
        await db.execute("""
            CREATE TABLE IF NOT EXISTS promo_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                max_uses INTEGER NOT NULL DEFAULT 1,
                used_count INTEGER NOT NULL DEFAULT 0,
                expires_at TEXT,
                created_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS promo_redemptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                promo_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(promo_id, user_id)
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_redemptions_user ON promo_redemptions(user_id)"
        )
        
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'keys'"
        )
        keys_table_existed = await cursor.fetchone() is not None

        await db.execute("""
            CREATE TABLE IF NOT EXISTS keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                url TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_keys_kind ON keys(kind, enabled)")
        
        # Стартовый набор ключей (если таблица только что создана)
        cursor = await db.execute("SELECT COUNT(*) FROM keys")
        if not keys_table_existed and (await cursor.fetchone())[0] == 0:
            now = datetime.now().isoformat(timespec="seconds")
            default_key = "vless://f0aef4c9-f7d6-4345-bad2-58bcc1ad8294@rus.noooo.win:8443?flow=xtls-rprx-vision&encryption=none&security=tls&sni=id-6adce7d7.adhil.uk&alpn=h2&fp=edge&type=tcp&headerType=none#%D0%A0%D0%BE%D1%81%D1%81%D0%B8%D1%8F"
            await db.execute(
                "INSERT INTO keys (kind, url, enabled, created_at) VALUES (?, ?, 1, ?)",
                ("black", default_key, now),
            )
            logger.info("Добавлен стартовый ключ в БД.")
        await db.commit()

async def add_subscription(user_id: int, username: str | None, first_name: str | None) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
        existing = await cursor.fetchone()
        
        if existing:
            await db.execute("UPDATE users SET username=?, first_name=? WHERE user_id=?", (username, first_name, user_id))
            await db.commit()
            return False

        await db.execute("""
            INSERT INTO users (user_id, username, first_name, created_at, is_banned)
            VALUES (?, ?, ?, ?, 0)
        """, (user_id, username, first_name, datetime.now().isoformat(timespec="seconds")))
        await db.commit()
        return True

# --- Персональная подписка (токен + счётчик устройств) ---

async def get_user_token(user_id: int) -> str:
    """Возвращает токен подписки пользователя, создавая его при необходимости."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT sub_token FROM users WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        if row and row[0]:
            return row[0]

        token = secrets.token_hex(16)
        if row:
            await db.execute(
                "UPDATE users SET sub_token = ? WHERE user_id = ?", (token, user_id)
            )
        else:
            await db.execute(
                "INSERT INTO users (user_id, created_at, sub_token) VALUES (?, ?, ?)",
                (user_id, datetime.now().isoformat(timespec="seconds"), token),
            )
        await db.commit()
        return token

def sub_url(token: str) -> str:
    return f"{SITE_URL.rstrip('/')}/?t={token}"

async def fetch_stats(token: str) -> dict | None:
    """Сколько устройств занято в подписке (данные из воркера)."""
    url = f"{WORKER_BASE}/api/stat/{token}"
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params={"key": WORKER_API_KEY}) as resp:
                if resp.status != 200:
                    logger.warning("stat: воркер вернул %s", resp.status)
                    return None
                return await resp.json()
    except Exception:
        logger.warning("stat: не удалось получить данные", exc_info=True)
        return None

def devices_line(stats: dict | None) -> str:
    if not stats or not stats.get("ok"):
        return "📱 Устройства: временно недоступны, откройте ссылку на сайте"
    used = int(stats.get("used") or 0)
    limit = int(stats.get("limit") or DEVICE_LIMIT)
    free = max(0, limit - used)
    return f"📱 Устройства: <b>{used} из {limit}</b> занято · свободно <b>{free}</b>"

# --- Реферальная система ---

def ref_link(user_id: int) -> str:
    return f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"

async def set_referrer(user_id: int, referrer_id: int) -> bool:
    if user_id == referrer_id:
        return False
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT referred_by FROM users WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        if not row or row[0] is not None:
            return False
        cursor = await db.execute("SELECT 1 FROM users WHERE user_id = ?", (referrer_id,))
        if not await cursor.fetchone():
            return False
        updated = await db.execute(
            "UPDATE users SET referred_by = ? WHERE user_id = ? AND referred_by IS NULL",
            (referrer_id, user_id),
        )
        await db.commit()
        return updated.rowcount > 0

async def get_invites(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM users WHERE referred_by = ?", (user_id,))
        row = await cursor.fetchone()
        return (row[0] if row else 0) or 0

async def count_referrals() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM users WHERE referred_by IS NOT NULL")
        row = await cursor.fetchone()
        return (row[0] if row else 0) or 0

async def find_user(text: str) -> int | None:
    text = text.strip()
    async with aiosqlite.connect(DB_PATH) as db:
        if text.lstrip("-").isdigit():
            cursor = await db.execute("SELECT user_id FROM users WHERE user_id = ?", (int(text),))
        else:
            cursor = await db.execute(
                "SELECT user_id FROM users WHERE LOWER(username) = ?", (text.lstrip("@").lower(),)
            )
        row = await cursor.fetchone()
        return row[0] if row else None

# --- Промокоды ---

def normalize_code(code: str) -> str:
    return code.strip().upper().replace(" ", "")

async def create_promo(code: str, max_uses: int, days: int) -> tuple[bool, str]:
    code = normalize_code(code)
    if not code or len(code) > 32:
        return False, "Код должен быть от 1 до 32 символов."
    if not code.isalnum():
        return False, "В коде допустимы только буквы и цифры."

    expires_at = None
    if days > 0:
        expires_at = (datetime.now() + timedelta(days=days)).isoformat(timespec="seconds")

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT id FROM promo_codes WHERE code = ?", (code,))
        if await cursor.fetchone():
            return False, "Такой промокод уже существует."
        await db.execute(
            "INSERT INTO promo_codes (code, max_uses, used_count, expires_at, created_at) "
            "VALUES (?, ?, 0, ?, ?)",
            (code, max_uses, expires_at, datetime.now().isoformat(timespec="seconds")),
        )
        await db.commit()
    return True, "Промокод создан."

async def list_promos(limit: int = 15, offset: int = 0) -> list[tuple]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id, code, max_uses, used_count, expires_at FROM promo_codes "
            "ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        return await cursor.fetchall()

async def count_promos() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM promo_codes")
        return (await cursor.fetchone())[0] or 0

async def delete_promo(promo_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("DELETE FROM promo_codes WHERE id = ?", (promo_id,))
        await db.commit()
        return cursor.rowcount > 0

async def increment_keys_issued(user_id: int, amount: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET keys_issued = keys_issued + ? WHERE user_id = ?", (amount, user_id))
        await db.commit()

async def count_users() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM users")
        row = await cursor.fetchone()
        return row[0] if row else 0

async def get_total_keys_today() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT COALESCE(SUM(keys_count), 0) FROM key_issues WHERE ts >= ?",
            (day_start_ts(),),
        )
        row = await cursor.fetchone()
        return row[0] if row and row[0] else 0

# --- Антиспам ---

def day_start_ts() -> float:
    now = datetime.now()
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

async def try_register_issue(
    user_id: int, kind: str, keys_count: int, check_cooldown: bool = True
) -> tuple[bool, str, int]:
    db = await aiosqlite.connect(DB_PATH, isolation_level=None)
    try:
        await db.execute("PRAGMA busy_timeout=5000")
        await db.execute("BEGIN IMMEDIATE")

        now_ts = time.time()

        if check_cooldown:
            cursor = await db.execute("SELECT MAX(ts) FROM key_issues WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            last_ts = row[0] if row and row[0] is not None else None
            if last_ts is not None:
                elapsed = now_ts - last_ts
                if elapsed < KEY_COOLDOWN_SEC:
                    await db.execute("ROLLBACK")
                    return False, "cooldown", max(1, int(KEY_COOLDOWN_SEC - elapsed + 0.999))

        cursor = await db.execute(
            "SELECT COUNT(*) FROM key_issues WHERE user_id = ? AND ts >= ?",
            (user_id, day_start_ts()),
        )
        used = (await cursor.fetchone())[0] or 0
        if used >= KEY_DAILY_LIMIT:
            await db.execute("ROLLBACK")
            return False, "daily_limit", 0

        await db.execute(
            "INSERT INTO key_issues (user_id, kind, keys_count, created_at, ts) VALUES (?, ?, ?, ?, ?)",
            (user_id, kind, keys_count, datetime.now().isoformat(timespec="seconds"), now_ts),
        )
        await db.execute("COMMIT")
        return True, "", 0
    except Exception:
        logger.exception("Не удалось проверить лимиты для %s", user_id)
        try:
            await db.execute("ROLLBACK")
        except Exception:
            pass
        return True, "", 0
    finally:
        await db.close()

def limit_message(reason: str, retry_after: int) -> str:
    if reason == "cooldown":
        if retry_after >= 60:
            wait = f"{retry_after // 60} мин {retry_after % 60} сек"
        else:
            wait = f"{retry_after} сек"
        return (
            "⏳ <b>Слишком часто</b>\n\n"
            f"Ключи можно получать не чаще одного раза в {KEY_COOLDOWN_SEC} сек.\n"
            f"Повторите через <b>{wait}</b>."
        )
    return (
        "🚫 <b>Дневной лимит исчерпан</b>\n\n"
        f"Вы получали ключи сегодня уже {KEY_DAILY_LIMIT} раз(а).\n"
        "Лимит обновится в 00:00.\n\n"
        "Если доступ нужен срочно — напишите в поддержку."
    )

async def is_banned(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT is_banned FROM users WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        return bool(row and row[0] == 1)

async def ban_user(username: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT user_id FROM users WHERE LOWER(username) = ?", (username.lstrip("@").lower(),))
        row = await cursor.fetchone()
        if not row: return False
        await db.execute("UPDATE users SET is_banned = 1 WHERE user_id = ?", (row[0],))
        await db.commit()
        return True

async def unban_user(username: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT user_id FROM users WHERE LOWER(username) = ?", (username.lstrip("@").lower(),))
        row = await cursor.fetchone()
        if not row: return False
        await db.execute("UPDATE users SET is_banned = 0 WHERE user_id = ?", (row[0],))
        await db.commit()
        return True

# --- Список всех пользователей ---

async def get_users_page(page: int) -> tuple[list[tuple], int]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM users")
        total = (await cursor.fetchone())[0]
        cursor = await db.execute(
            "SELECT user_id, username, first_name, created_at, is_banned, keys_issued "
            "FROM users ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (USERS_PER_PAGE, page * USERS_PER_PAGE),
        )
        rows = await cursor.fetchall()
    return rows, total

async def get_all_users() -> list[tuple]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT user_id, username, first_name, created_at, is_banned, keys_issued "
            "FROM users ORDER BY created_at ASC"
        )
        return await cursor.fetchall()

# --- Серверы (ключи) в БД ---

async def get_keys(kind: str, only_enabled: bool = True) -> list[tuple[int, str]]:
    sql = "SELECT id, url FROM keys WHERE kind = ?"
    if only_enabled:
        sql += " AND enabled = 1"
    sql += " ORDER BY id"
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(sql, (kind,))
        return await cursor.fetchall()

async def get_all_keys() -> list[tuple[int, str, str, int]]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT id, kind, url, enabled FROM keys ORDER BY kind, id")
        return await cursor.fetchall()

async def get_key(key_id: int) -> tuple[int, str, str, int] | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id, kind, url, enabled FROM keys WHERE id = ?", (key_id,)
        )
        return await cursor.fetchone()

async def add_key(kind: str, url: str) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT id FROM keys WHERE kind = ? AND url = ?", (kind, url))
        if await cursor.fetchone():
            return "duplicate"
        await db.execute(
            "INSERT INTO keys (kind, url, enabled, created_at) VALUES (?, ?, 1, ?)",
            (kind, url, datetime.now().isoformat(timespec="seconds")),
        )
        await db.commit()
    return "ok"

async def set_key_enabled(key_id: int, enabled: bool) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE keys SET enabled = ? WHERE id = ?", (1 if enabled else 0, key_id)
        )
        await db.commit()
        return cursor.rowcount > 0

async def delete_key(key_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("DELETE FROM keys WHERE id = ?", (key_id,))
        await db.commit()
        return cursor.rowcount > 0

async def count_keys() -> tuple[int, int]:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT kind, COUNT(*) FROM keys GROUP BY kind")
        data = {kind: n for kind, n in await cursor.fetchall()}
    return data.get("black", 0), data.get("white", 0)

# ==================== ПРОВЕРКА СЕРВЕРОВ ====================

async def check_server(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        fut = asyncio.open_connection(host, port)
        reader, writer = await asyncio.wait_for(fut, timeout=timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except Exception:
        return False

def extract_host_port(key: str) -> tuple[str | None, int | None]:
    try:
        parsed = urlparse(key)
        host = parsed.hostname
        port = parsed.port or 443
        return host, port
    except Exception:
        return None, None

def get_key_label(key: str) -> str:
    try:
        frag = key.split("#", 1)[1] if "#" in key else ""
        return unquote(frag) or "Без названия"
    except Exception:
        return "Сервер"

async def _false() -> bool:
    return False

async def collect_server_status() -> list[dict]:
    rows = await get_all_keys()
    servers = []
    for key_id, kind, url, enabled in rows:
        host, port = extract_host_port(url)
        servers.append({
            "id": key_id,
            "kind": kind,
            "url": url,
            "enabled": bool(enabled),
            "host": host,
            "port": port,
            "label": get_key_label(url),
        })

    results = await asyncio.gather(*[
        check_server(s["host"], s["port"]) if (s["enabled"] and s["host"]) else _false()
        for s in servers
    ])
    for server, ok in zip(servers, results):
        server["online"] = bool(ok)
    return servers

def server_line(server: dict) -> str:
    kind = "⚫" if server["kind"] == "black" else "⚪"
    label = server["label"]
    if len(label) > 40:
        label = label[:37] + "..."
    host = f"{server['host']}:{server['port']}" if server["host"] else "некорректный ключ"
    return f"{kind} <b>{html.escape(label)}</b>\n    <code>{host}</code>"

async def notify_admins(bot: Bot, text: str) -> None:
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            logger.warning("Не смог отправить уведомление админу %s", admin_id)

_server_state: dict[int, bool] = {}

async def monitor_servers(bot: Bot) -> None:
    await asyncio.sleep(MONITOR_FIRST_DELAY_SEC)
    first_run = True

    while True:
        try:
            servers = await collect_server_status()
            down, up = [], []
            for server in servers:
                if not server["enabled"]:
                    continue
                was = _server_state.get(server["id"])
                _server_state[server["id"]] = server["online"]
                if was is None:
                    if not server["online"]:
                        down.append(server)
                elif was and not server["online"]:
                    down.append(server)
                elif not was and server["online"]:
                    up.append(server)

            if first_run:
                first_run = False
                if down:
                    await notify_admins(
                        bot,
                        "⚠️ <b>Проверка при запуске</b>\n\n"
                        + "\n\n".join(server_line(s) for s in down)
                        + "\n\n<i>Остальные серверы отвечают.</i>",
                    )
            else:
                if down:
                    await notify_admins(
                        bot,
                        f"🔴 <b>Сервер недоступен ({len(down)})</b>\n\n"
                        + "\n\n".join(server_line(s) for s in down)
                        + "\n\nАдминка → 🌐 Статус серверов.",
                    )
                if up:
                    await notify_admins(
                        bot,
                        f"🟢 <b>Сервер снова онлайн ({len(up)})</b>\n\n"
                        + "\n\n".join(server_line(s) for s in up),
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Ошибка в мониторинге серверов")

        await asyncio.sleep(MONITOR_INTERVAL_SEC)

# ==================== ПОДПИСКА ====================

async def check_subscription(bot: Bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        return member.status not in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED)
    except Exception as exc:
        logger.warning("Не удалось проверить подписку %s: %s", user_id, exc)
        return False

async def require_subscription(bot: Bot, event: Message | CallbackQuery) -> bool:
    global MAINTENANCE_MODE
    user = event.from_user
    if not user: return False
    
    if await is_banned(user.id):
        msg = "⛔ Вы заблокированы."
        if isinstance(event, CallbackQuery): await event.answer(msg, show_alert=True)
        else: await event.answer(msg)
        return False
    
    if MAINTENANCE_MODE and user.id not in ADMIN_IDS:
        text = (
            "🛠 <b>Технические работы</b>\n\n"
            "Приносим извинения за неудобства. Наши серверы обновляются.\n\n"
            "Следите за новостями в нашем канале: " + CHANNEL_LINK
        )
        if isinstance(event, CallbackQuery):
            try: await event.message.edit_text(text)
            except TelegramBadRequest: await event.message.answer(text)
            await event.answer()
        else: await event.answer(text)
        return False

    if await check_subscription(bot, user.id): return True

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Подписаться", url=CHANNEL_LINK)],
        [InlineKeyboardButton(text="✅ Я подписался", callback_data="check_sub")],
    ])
    text = f"👋 Подпишитесь на канал {CHANNEL_ID}."
    if isinstance(event, CallbackQuery):
        try: await event.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest: await event.message.answer(text, reply_markup=kb)
        await event.answer()
    else: await event.answer(text, reply_markup=kb)
    return False

# ==================== МЕНЮ ====================

def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔌 Подключиться", callback_data="connect")],
        [InlineKeyboardButton(text="📱 Скачать приложение", callback_data="apps")],
        [
            InlineKeyboardButton(text="📢 Наш канал", url=CHANNEL_LINK),
            InlineKeyboardButton(text="💬 Чат", url=CHAT_LINK),
        ],
        [
            InlineKeyboardButton(text="🛠 Поддержка", url=SUPPORT_LINK),
            InlineKeyboardButton(text="📖 Инструкция", callback_data="instruction"),
        ],
        [InlineKeyboardButton(text="ℹ️ О сервисе и документы", callback_data="about_service")],
    ])

def connect_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚫ Чёрные списки", callback_data="get_black")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")],
    ])

def keys_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="back_to_menu")]
    ])

WELCOME_TEXT = f"👋 Привет, {{name}}!\n\nЭто бот <b>{BRAND_TITLE}</b>.\n\nВыберите действие 👇"

# --- ИНСТРУКЦИИ ПО ПЛАТФОРМАМ ---

INSTRUCTION_COMMON = (
    "📖 <b>Инструкция</b>\n\n"
    "Выберите вашу платформу 👇\n\n"
    "ℹ️ На всех устройствах используется приложение <b>Happ</b> или <b>Incy</b>."
)

INSTRUCTION_ANDROID = (
    "🤖 <b>Android</b>\n\n"
    "1️⃣ Скачайте <b>Happ</b> из Google Play.\n"
    f"🔗 <a href='{HAPP_ANDROID_URL}'>Скачать Happ (Google Play)</a>\n\n"
    "2️⃣ Получите ключ в боте («🔌 Подключиться»).\n\n"
    "3️⃣ Откройте Happ → «+» → «Импорт из буфера обмена».\n\n"
    "4️⃣ Нажмите «Подключить».\n\n"
    "✅ Готово!"
)

INSTRUCTION_IOS = (
    "🍏 <b>iOS (iPhone / iPad)</b>\n\n"
    "1️⃣ Скачайте <b>Incy</b> из App Store.\n"
    f"🔗 <a href='{INCY_IOS_URL}'>Скачать Incy (App Store)</a>\n\n"
    "2️⃣ Получите ключ в боте («🔌 Подключиться»).\n\n"
    "3️⃣ Откройте Incy → «+» → «Импорт из буфера обмена».\n\n"
    "4️⃣ Нажмите «Подключить».\n\n"
    "✅ Готово!"
)

INSTRUCTION_WINDOWS = (
    "🪟 <b>Windows</b>\n\n"
    "1️⃣ Скачайте <b>Happ</b> для Windows.\n"
    f"🔗 <a href='{HAPP_WINDOWS_URL}'>Скачать Happ (happ.su)</a>\n\n"
    "2️⃣ Получите ключ в боте («🔌 Подключиться»).\n\n"
    "3️⃣ Откройте Happ → «Добавить профиль» → "
    "«Импорт из буфера обмена» (Ctrl+V).\n\n"
    "4️⃣ Выберите профиль и нажмите «Подключить».\n\n"
    "💡 Если не работает — запустите Happ от имени администратора.\n\n"
    "✅ Готово!"
)

INSTRUCTION_LINUX = (
    "🐧 <b>Linux</b>\n\n"
    "1️⃣ Скачайте <b>Happ</b> для Linux.\n"
    f"🔗 <a href='{HAPP_LINUX_URL}'>Скачать Happ (happ.su)</a>\n\n"
    "2️⃣ Получите ключ в боте («🔌 Подключиться»).\n\n"
    "3️⃣ Откройте Happ → «Добавить профиль» → "
    "«Импорт из буфера обмена».\n\n"
    "4️⃣ Выберите профиль → «Подключить».\n\n"
    "💡 Для AppImage: <code>chmod +x Happ*.AppImage</code>, затем запустите файл.\n\n"
    "✅ Готово!"
)

INSTRUCTION_PLATFORM_KB = InlineKeyboardMarkup(inline_keyboard=[
    [
        InlineKeyboardButton(text="🤖 Android", callback_data="instr_android"),
        InlineKeyboardButton(text="🍏 iOS", callback_data="instr_ios"),
    ],
    [
        InlineKeyboardButton(text="🪟 Windows", callback_data="instr_windows"),
        InlineKeyboardButton(text="🐧 Linux", callback_data="instr_linux"),
    ],
    [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")],
])

APPS_TEXT = (
    "📱 <b>Приложения для подключения:</b>\n\n"
    "🤖 <b>Для Android:</b>\n"
    "🔹 Happ\n"
    f"🔗 <a href='{HAPP_ANDROID_URL}'>Скачать в Google Play</a>\n\n"
    "🍏 <b>Для iOS (iPhone/iPad):</b>\n"
    "🔹 Incy\n"
    f"🔗 <a href='{INCY_IOS_URL}'>Скачать в App Store</a>\n\n"
    "🪟 <b>Для Windows:</b>\n"
    "🔹 Happ\n"
    f"🔗 <a href='{HAPP_WINDOWS_URL}'>Скачать Happ (happ.su)</a>\n\n"
    "🐧 <b>Для Linux:</b>\n"
    "🔹 Happ\n"
    f"🔗 <a href='{HAPP_LINUX_URL}'>Скачать Happ (happ.su)</a>\n\n"
    "<i>Установите приложение, соответствующее вашей системе.</i>"
)

# ==================== FSM ====================

class Broadcast(StatesGroup):
    waiting_message = State()

class UserBan(StatesGroup):
    waiting_username = State()

class SetAnnouncement(StatesGroup):
    waiting_text = State()

class AddKey(StatesGroup):
    waiting_url = State()

class AdminPromo(StatesGroup):
    waiting_params = State()

class AdminGrant(StatesGroup):
    waiting_user = State()

# ==================== ОТПРАВКА КЛЮЧЕЙ ====================

async def send_keys(message: Message, user_id: int, k_type: str = "black") -> None:
    if k_type != "black":
        return

    if user_id not in ADMIN_IDS:
        allowed, reason, retry_after = await try_register_issue(
            user_id, k_type, 1, check_cooldown=False
        )
        if not allowed:
            logger.info("Лимит сработал: user=%s reason=%s", user_id, reason)
            await message.answer(limit_message(reason, retry_after))
            return

    await increment_keys_issued(user_id, 1)

    token = await get_user_token(user_id)
    stats = await fetch_stats(token)
    link = sub_url(token)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Открыть сайт", url=link)],
        [InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="back_to_menu")],
    ])

    await message.answer(
        "⚫ <b>Чёрные списки</b>\n\n"
        "Ваша персональная ссылка на подключение:\n\n"
        f"🔗 {link}\n\n"
        f"{devices_line(stats)}\n\n"
        "Ссылка одна для всех ваших устройств: откройте её на телефоне и на компьютере.",
        disable_web_page_preview=True,
        reply_markup=kb,
    )

# ==================== ПОЛЬЗОВАТЕЛЬСКИЕ ХЭНДЛЕРЫ ====================

@router.message(CommandStart())
async def cmd_start(message: Message, bot: Bot, state: FSMContext) -> None:
    global ANNOUNCEMENT_TEXT
    user = message.from_user
    if not user: return
    await state.clear()

    is_new = await add_subscription(user.id, user.username, user.first_name)

    parts = (message.text or "").split(maxsplit=1)
    payload = parts[1].strip() if len(parts) > 1 else ""
    if is_new and payload.startswith("ref_"):
        try:
            referrer_id = int(payload[4:])
        except ValueError:
            referrer_id = 0
        if referrer_id and referrer_id != user.id and await set_referrer(user.id, referrer_id):
            await notify_referrer(bot, referrer_id, user)

    if not await require_subscription(bot, message): return
    
    text = WELCOME_TEXT.format(name=user.first_name or "друг")
    if ANNOUNCEMENT_TEXT:
        text = f"📢 <b>Анонс:</b> {ANNOUNCEMENT_TEXT}\n\n" + text

    await message.answer(text, reply_markup=main_menu_kb(), disable_web_page_preview=True)

@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    user = message.from_user
    if not user: return
    await message.answer(f"🆔 Твой ID: <code>{user.id}</code>")

@router.callback_query(F.data == "check_sub")
async def cb_check_sub(callback: CallbackQuery, bot: Bot) -> None:
    user = callback.from_user
    await add_subscription(user.id, user.username, user.first_name)
    if await check_subscription(bot, user.id):
        try: await callback.message.delete()
        except TelegramBadRequest: pass
        
        text = WELCOME_TEXT.format(name=user.first_name or "друг")
        if ANNOUNCEMENT_TEXT:
            text = f"📢 <b>Анонс:</b> {ANNOUNCEMENT_TEXT}\n\n" + text

        await callback.message.answer(text, reply_markup=main_menu_kb(), disable_web_page_preview=True)
        await callback.answer("Спасибо! ✅")
    else:
        await callback.answer("Вы не подписались ❌", show_alert=True)

@router.callback_query(F.data == "connect")
async def cb_connect(callback: CallbackQuery, bot: Bot) -> None:
    if not await require_subscription(bot, callback): return
    token = await get_user_token(callback.from_user.id)
    stats = await fetch_stats(token)
    try: await callback.message.delete()
    except TelegramBadRequest: pass
    await callback.message.answer(
        "🔌 <b>Подключение</b>\n\n"
        f"{devices_line(stats)}\n\n"
        "⚫ <b>Чёрные списки</b> — персональная ссылка на сайт и подписку.\n\n"
        "ℹ️ Доступ без ограничений по времени.",
        reply_markup=connect_menu_kb(),
    )
    await callback.answer()

@router.callback_query(F.data == "get_black")
async def cb_get_keys(callback: CallbackQuery, bot: Bot) -> None:
    if not await require_subscription(bot, callback): return
    await callback.answer()
    await send_keys(callback.message, callback.from_user.id, "black")

@router.callback_query(F.data == "apps")
async def cb_apps(callback: CallbackQuery, bot: Bot) -> None:
    if not await require_subscription(bot, callback): return
    try: await callback.message.delete()
    except TelegramBadRequest: pass
    await callback.message.answer(
        APPS_TEXT,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")]]),
        disable_web_page_preview=True,
    )
    await callback.answer()

@router.callback_query(F.data == "instruction")
async def cb_instruction(callback: CallbackQuery, bot: Bot) -> None:
    if not await require_subscription(bot, callback): return
    try: await callback.message.delete()
    except TelegramBadRequest: pass
    await callback.message.answer(
        INSTRUCTION_COMMON,
        reply_markup=INSTRUCTION_PLATFORM_KB,
        disable_web_page_preview=True,
    )
    await callback.answer()

@router.callback_query(F.data.in_(["instr_android", "instr_ios", "instr_windows", "instr_linux"]))
async def cb_instruction_platform(callback: CallbackQuery, bot: Bot) -> None:
    if not await require_subscription(bot, callback): return

    mapping = {
        "instr_android": INSTRUCTION_ANDROID,
        "instr_ios": INSTRUCTION_IOS,
        "instr_windows": INSTRUCTION_WINDOWS,
        "instr_linux": INSTRUCTION_LINUX,
    }
    text = mapping.get(callback.data, INSTRUCTION_COMMON)

    back_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ К выбору платформы", callback_data="instruction")],
        [InlineKeyboardButton(text="🏠 В меню", callback_data="back_to_menu")],
    ])

    try:
        await callback.message.edit_text(text, reply_markup=back_kb, disable_web_page_preview=True)
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=back_kb, disable_web_page_preview=True)
    await callback.answer()

@router.callback_query(F.data == "back_to_menu")
async def cb_back(callback: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    global ANNOUNCEMENT_TEXT
    await state.clear()
    if not await require_subscription(bot, callback): return
    user = callback.from_user
    try: await callback.message.delete()
    except TelegramBadRequest: pass

    text = WELCOME_TEXT.format(name=user.first_name or "друг")
    if ANNOUNCEMENT_TEXT:
        text = f"📢 <b>Анонс:</b> {ANNOUNCEMENT_TEXT}\n\n" + text

    await callback.message.answer(text, reply_markup=main_menu_kb(), disable_web_page_preview=True)
    await callback.answer()

# --- ИНФОРМАЦИЯ И ДОКУМЕНТЫ ---

@router.callback_query(F.data == "about_service")
async def cb_about_service(callback: CallbackQuery, bot: Bot) -> None:
    if not await require_subscription(bot, callback): return
    
    text = (
        f"ℹ️ <b>О сервисе {BRAND_TITLE}</b>\n\n"
        "Мы предоставляем безопасный доступ к сети Интернет через современные протоколы шифрования. "
        "Сервис не требует регистрации и не хранит историю ваших подключений.\n\n"
        "👇 Ознакомьтесь с документацией и правилами сервиса:"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📄 Пользовательское соглашение (Оферта)", url=DOC_TERMS_URL)],
        [InlineKeyboardButton(text="🔒 Политика конфиденциальности", url=DOC_PRIVACY_URL)],
        [InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="back_to_menu")],
    ])
    
    try: 
        await callback.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except TelegramBadRequest: 
        await callback.message.answer(text, reply_markup=kb, disable_web_page_preview=True)
        
    await callback.answer()

# ==================== АДМИН-ПАНЕЛЬ ====================

def admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="adm_stats")],
        [InlineKeyboardButton(text="🔑 Ключи", callback_data="adm_keys")],
        [InlineKeyboardButton(text="🎟 Промокоды", callback_data="adm_promo")],
        [InlineKeyboardButton(text="🌐 Статус серверов", callback_data="adm_servers")],
        [InlineKeyboardButton(text="👥 Все пользователи", callback_data="adm_users:0")],
        [InlineKeyboardButton(text="📅 Анонс", callback_data="adm_set_announcement")],
        [InlineKeyboardButton(text="📨 Рассылка", callback_data="adm_broadcast")],
        [InlineKeyboardButton(text="🚫 Забанить", callback_data="adm_ban_user")],
        [InlineKeyboardButton(text="🛠 Тех. работы", callback_data="adm_toggle_maintenance")],
    ])

@router.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ Нет доступа.")
        return
    await message.answer("🛠 <b>Админ-панель</b>", reply_markup=admin_kb())

@router.callback_query(F.data == "adm_stats")
async def cb_stats(callback: CallbackQuery) -> None:
    if callback.from_user.id not in ADMIN_IDS: return
    
    total = await count_users()
    keys_today = await get_total_keys_today()
    black_n, white_n = await count_keys()
    referred = await count_referrals()
    promos = await count_promos()

    await callback.message.edit_text(
        f"📊 <b>Статистика</b>\n\n"
        f"👥 Всего пользователей: <b>{total}</b>\n"
        f"🤝 Пришли по рефке: <b>{referred}</b>\n"
        f"🎟 Промокодов: <b>{promos}</b>\n"
        f"🔑 Ключей выдано сегодня: <b>{keys_today}</b>\n"
        f"🌐 Серверов в базе: ⚫ <b>{black_n}</b> / ⚪ <b>{white_n}</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👥 Все пользователи", callback_data="adm_users:0")],
            [InlineKeyboardButton(text="🔑 Ключи", callback_data="adm_keys")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_back")],
        ])
    )
    await callback.answer()

@router.callback_query(F.data == "adm_servers")
async def cb_servers(callback: CallbackQuery) -> None:
    if callback.from_user.id not in ADMIN_IDS:
        return
    await callback.answer("⏳ Проверяю серверы...")
    servers = await collect_server_status()
    lines = ["🌐 <b>Статус серверов</b>\n"]
    online = 0
    enabled_total = 0
    for server in servers:
        if not server["enabled"]:
            icon = "⏸"
        else:
            enabled_total += 1
            if server["online"]:
                online += 1
                icon = "🟢"
            else:
                icon = "🔴"
        lines.append(f"{icon} {server_line(server)}")

    if not servers:
        lines.append("В базе нет ни одного ключа — добавьте через «🔑 Ключи».")
    else:
        lines.append(f"\n📊 Онлайн: <b>{online}/{enabled_total}</b> (⏸ отключено: {len(servers) - enabled_total})")

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="adm_servers")],
            [InlineKeyboardButton(text="🔑 Ключи", callback_data="adm_keys")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_back")],
        ]),
    )

def keys_admin_text(black_n: int, white_n: int) -> str:
    return (
        "🔑 <b>Управление ключами</b>\n\n"
        f"⚫ Чёрные списки: <b>{black_n}</b>\n"
        f"⚪ Белые списки: <b>{white_n}</b>\n\n"
        "Выберите список, чтобы включить/выключить или удалить серверы, "
        "либо добавьте новый ключ.\n\n"
        "<i>Изменения применяются сразу — перезапуск не нужен.</i>"
    )

def keys_admin_kb(black_n: int, white_n: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"⚫ Чёрные ({black_n})", callback_data="adm_keys_list:black:0")],
        [InlineKeyboardButton(text=f"⚪ Белые ({white_n})", callback_data="adm_keys_list:white:0")],
        [InlineKeyboardButton(text="➕ Добавить ключ", callback_data="adm_key_add")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_back")],
    ])

@router.callback_query(F.data == "adm_keys")
async def cb_keys(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id not in ADMIN_IDS: return
    await state.clear()
    black_n, white_n = await count_keys()
    try:
        await callback.message.edit_text(
            keys_admin_text(black_n, white_n),
            reply_markup=keys_admin_kb(black_n, white_n),
        )
    except TelegramBadRequest:
        pass
    await callback.answer()

async def render_keys_list(callback: CallbackQuery, kind: str, page: int) -> None:
    rows = [row for row in await get_all_keys() if row[1] == kind]
    pages = max(1, (len(rows) + KEYS_PER_PAGE - 1) // KEYS_PER_PAGE)
    page = min(max(0, page), pages - 1)
    chunk = rows[page * KEYS_PER_PAGE:(page + 1) * KEYS_PER_PAGE]

    title = "⚫ <b>Чёрные списки</b>" if kind == "black" else "⚪ <b>Белый список</b>"
    lines = [f"{title} — {len(rows)} шт.", f"Страница {page + 1}/{pages}\n"]

    keyboard = []
    for num, (key_id, _kind, url, enabled) in enumerate(chunk, start=page * KEYS_PER_PAGE + 1):
        label = get_key_label(url)
        if len(label) > 22:
            label = label[:20] + "…"
        host, port = extract_host_port(url)
        icon = "🟢" if enabled else "🔴"
        lines.append(f"{num}. {icon} <b>{html.escape(label)}</b>\n    <code>{host}:{port}</code>")
        keyboard.append([
            InlineKeyboardButton(
                text=f"{icon} #{key_id} {label}",
                callback_data=f"adm_key_toggle:{key_id}:{kind}:{page}",
            ),
            InlineKeyboardButton(
                text="🗑",
                callback_data=f"adm_key_del:{key_id}:{kind}:{page}",
            ),
        ])

    if not rows:
        lines.append("Пока пусто — добавьте ключ.")

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"adm_keys_list:{kind}:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"adm_keys_list:{kind}:{page + 1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton(text="➕ Добавить", callback_data=f"adm_key_add:{kind}")])
    keyboard.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_keys")])

    try:
        await callback.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    except TelegramBadRequest:
        pass

@router.callback_query(F.data.startswith("adm_keys_list:"))
async def cb_keys_list(callback: CallbackQuery) -> None:
    if callback.from_user.id not in ADMIN_IDS: return
    try:
        _, kind, page_raw = callback.data.split(":")
        page = int(page_raw)
    except ValueError:
        await callback.answer("Некорректная страница", show_alert=True)
        return
    if kind not in ("black", "white"):
        await callback.answer()
        return
    await render_keys_list(callback, kind, page)
    await callback.answer()

@router.callback_query(F.data.startswith("adm_key_toggle:"))
async def cb_key_toggle(callback: CallbackQuery) -> None:
    if callback.from_user.id not in ADMIN_IDS: return
    try:
        _, key_raw, kind, page_raw = callback.data.split(":")
        key_id, page = int(key_raw), int(page_raw)
    except ValueError:
        await callback.answer()
        return

    key = await get_key(key_id)
    if not key:
        await callback.answer("Ключ не найден", show_alert=True)
        return

    now_enabled = not bool(key[3])
    await set_key_enabled(key_id, now_enabled)
    await callback.answer("✅ Включён" if now_enabled else "⏸ Отключён")
    await render_keys_list(callback, kind, page)

@router.callback_query(F.data.startswith("adm_key_del:"))
async def cb_key_delete(callback: CallbackQuery) -> None:
    if callback.from_user.id not in ADMIN_IDS: return
    try:
        _, key_raw, kind, page_raw = callback.data.split(":")
        key_id, page = int(key_raw), int(page_raw)
    except ValueError:
        await callback.answer()
        return

    deleted = await delete_key(key_id)
    await callback.answer("🗑 Удалён" if deleted else "Ключ не найден", show_alert=not deleted)
    await render_keys_list(callback, kind, page)

@router.callback_query(F.data.startswith("adm_key_add"))
async def cb_key_add(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id not in ADMIN_IDS: return

    parts = callback.data.split(":")
    kind = parts[1] if len(parts) > 1 and parts[1] in ("black", "white") else ""

    if not kind:
        await callback.message.edit_text(
            "➕ <b>Куда добавить ключ?</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⚫ Чёрные списки", callback_data="adm_key_add:black")],
                [InlineKeyboardButton(text="⚪ Белые списки", callback_data="adm_key_add:white")],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_keys")],
            ]),
        )
        await callback.answer()
        return

    await state.set_state(AddKey.waiting_url)
    await state.update_data(kind=kind)
    where = "чёрные" if kind == "black" else "белые"
    await callback.message.edit_text(
        f"➕ <b>Добавление в {where} списки</b>\n\n"
        "Отправьте ключ (<code>vless://...</code>).\n"
        "Можно несколько — по одному на строку.\n\n"
        "/cancel — отмена.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Отмена", callback_data="adm_keys")],
        ]),
    )
    await callback.answer()

@router.message(AddKey.waiting_url, Command("cancel"))
async def cancel_add_key(message: Message, state: FSMContext) -> None:
    if not message.from_user or message.from_user.id not in ADMIN_IDS: return
    await state.clear()
    black_n, white_n = await count_keys()
    await message.answer("❌ Отменено.", reply_markup=keys_admin_kb(black_n, white_n))

@router.message(AddKey.waiting_url)
async def process_add_key(message: Message, state: FSMContext) -> None:
    if not message.from_user or message.from_user.id not in ADMIN_IDS: return

    data = await state.get_data()
    kind = data.get("kind", "black")
    lines = [line.strip() for line in (message.text or "").splitlines() if line.strip()]

    added = duplicates = invalid = 0
    for line in lines:
        if "://" not in line or len(line) > 4000:
            invalid += 1
            continue
        result = await add_key(kind, line)
        if result == "ok":
            added += 1
        elif result == "duplicate":
            duplicates += 1
        else:
            invalid += 1

    await state.clear()
    black_n, white_n = await count_keys()

    if not lines:
        report = "⚠️ Пустое сообщение — ничего не добавлено."
    else:
        parts = [f"✅ Добавлено: <b>{added}</b>"]
        if duplicates:
            parts.append(f"♻️ Уже были: <b>{duplicates}</b>")
        if invalid:
            parts.append(f"⚠️ Некорректных строк: <b>{invalid}</b>")
        report = "\n".join(parts)

    await message.answer(report, reply_markup=keys_admin_kb(black_n, white_n))

@router.callback_query(F.data.startswith("adm_users:"))
async def cb_users(callback: CallbackQuery) -> None:
    if callback.from_user.id not in ADMIN_IDS: return
    try:
        page = max(0, int(callback.data.split(":", 1)[1]))
    except ValueError:
        page = 0

    rows, total = await get_users_page(page)
    pages = max(1, (total + USERS_PER_PAGE - 1) // USERS_PER_PAGE)
    if page >= pages:
        page = pages - 1
        rows, total = await get_users_page(page)

    lines = [f"👥 <b>Все пользователи бота:</b> {total}", f"Страница {page + 1}/{pages} (новые сверху)\n"]
    start_n = page * USERS_PER_PAGE
    for i, (uid, username, first_name, created_at, banned, keys_issued) in enumerate(rows, start=start_n + 1):
        uname = f"@{html.escape(username)}" if username else "—"
        name = html.escape(first_name or "—")
        date = (created_at or "")[:16].replace("T", " ")
        mark = " 🚫" if banned else ""
        lines.append(f"{i}. <code>{uid}</code> {uname} | {name} | {date} | 🔑{keys_issued or 0}{mark}")
    if not rows:
        lines.append("Пока никого нет.")

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"adm_users:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"adm_users:{page + 1}"))

    keyboard = []
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton(text="📄 Скачать список (CSV)", callback_data="adm_export")])
    keyboard.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_back")])

    try:
        await callback.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    except TelegramBadRequest:
        pass
    await callback.answer()

@router.callback_query(F.data == "adm_export")
async def cb_export(callback: CallbackQuery) -> None:
    if callback.from_user.id not in ADMIN_IDS: return

    users = await get_all_users()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["user_id", "username", "first_name", "first_seen", "banned", "keys_issued"])
    for row in users:
        writer.writerow(row)

    data = buf.getvalue().encode("utf-8-sig")
    filename = f"users_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    await callback.message.answer_document(
        BufferedInputFile(data, filename=filename),
        caption=f"👥 Всего пользователей: {len(users)}",
    )
    await callback.answer()

@router.callback_query(F.data == "adm_set_announcement")
async def cb_set_announcement(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id not in ADMIN_IDS: return
    await state.set_state(SetAnnouncement.waiting_text)
    await callback.message.edit_text("📅 Отправьте текст анонса.\n\nЧтобы убрать — отправьте «-».\n/cancel — отмена.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="adm_back")]]))
    await callback.answer()

@router.message(SetAnnouncement.waiting_text, Command("cancel"))
async def cancel_announcement(message: Message, state: FSMContext) -> None:
    if not message.from_user or message.from_user.id not in ADMIN_IDS: return
    await state.clear()
    await message.answer("❌ Отменено.", reply_markup=admin_kb())

@router.message(SetAnnouncement.waiting_text)
async def process_announcement(message: Message, state: FSMContext) -> None:
    global ANNOUNCEMENT_TEXT
    if not message.from_user or message.from_user.id not in ADMIN_IDS: return
    
    text = message.text.strip()
    if text == "-":
        ANNOUNCEMENT_TEXT = ""
        await message.answer("✅ Анонс убран.", reply_markup=admin_kb())
    else:
        ANNOUNCEMENT_TEXT = text
        await message.answer(f"✅ Анонс установлен:\n\n{text}", reply_markup=admin_kb())
    
    await state.clear()

@router.callback_query(F.data == "adm_toggle_maintenance")
async def cb_toggle(callback: CallbackQuery) -> None:
    global MAINTENANCE_MODE
    if callback.from_user.id not in ADMIN_IDS: return
    MAINTENANCE_MODE = not MAINTENANCE_MODE
    status = "ВКЛ 🛑" if MAINTENANCE_MODE else "ВЫКЛ 🟢"
    await callback.message.edit_text(f"🛠 Тех. работы: {status}", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_back")]]))
    await callback.answer(status)

@router.callback_query(F.data == "adm_ban_user")
async def cb_ban(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id not in ADMIN_IDS: return
    await state.set_state(UserBan.waiting_username)
    await callback.message.edit_text("🚫 Отправьте @username или ID.\nРазбан: <code>unban @username</code>\n/cancel — отмена.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="adm_back")]]))
    await callback.answer()

@router.message(UserBan.waiting_username, Command("cancel"))
async def cancel_ban(message: Message, state: FSMContext) -> None:
    if not message.from_user or message.from_user.id not in ADMIN_IDS: return
    await state.clear()
    await message.answer("❌ Отменено.", reply_markup=admin_kb())

@router.message(UserBan.waiting_username)
async def process_ban(message: Message, state: FSMContext) -> None:
    if not message.from_user or message.from_user.id not in ADMIN_IDS: return
    text = message.text.strip()
    
    if text.lower().startswith("unban"):
        username = text[5:].strip()
        ok = await unban_user(username)
        await message.answer(f"{'✅ Разбанен' if ok else '⚠️ Не найден'} {username}.", reply_markup=admin_kb())
    else:
        ok = await ban_user(text)
        await message.answer(f"{'✅ Забанен' if ok else '⚠️ Не найден'} {text}.", reply_markup=admin_kb())
    await state.clear()

@router.callback_query(F.data == "adm_broadcast")
async def cb_broadcast(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id not in ADMIN_IDS: return
    await state.set_state(Broadcast.waiting_message)
    await callback.message.edit_text("📨 Отправьте сообщение для рассылки.\n/cancel — отмена.")
    await callback.answer()

@router.message(Broadcast.waiting_message, Command("cancel"))
async def cancel_broadcast(message: Message, state: FSMContext) -> None:
    if not message.from_user or message.from_user.id not in ADMIN_IDS: return
    await state.clear()
    await message.answer("❌ Отменено.", reply_markup=admin_kb())

@router.message(Broadcast.waiting_message)
async def broadcast_input(message: Message, state: FSMContext, bot: Bot) -> None:
    if not message.from_user or message.from_user.id not in ADMIN_IDS: return
    await state.clear()
    
    await message.answer("🚀 Рассылка запущена...")
    
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT user_id FROM users WHERE is_banned = 0")
        user_ids = [r[0] for r in await cursor.fetchall()]
    
    sent = 0
    for uid in user_ids:
        try:
            await bot.copy_message(chat_id=uid, from_chat_id=message.chat.id, message_id=message.message_id)
            sent += 1
        except: pass
        await asyncio.sleep(0.05)
    
    await message.answer(f"✅ Доставлено: {sent}/{len(user_ids)}", reply_markup=admin_kb())

@router.callback_query(F.data == "adm_back")
async def cb_adm_back(callback: CallbackQuery) -> None:
    if callback.from_user.id not in ADMIN_IDS: return
    await callback.message.edit_text("🛠 <b>Админ-панель</b>", reply_markup=admin_kb())
    await callback.answer()

# ==================== ЗАПУСК ====================

async def main() -> None:
    global BOT_USERNAME
    await init_db()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    me = await bot.get_me()
    BOT_USERNAME = me.username or ""
    if not BOT_USERNAME:
        logger.warning("У бота нет юзернейма — реф-ссылки работать не будут")

    monitor_task = None
    if MONITOR_ENABLED:
        monitor_task = asyncio.create_task(monitor_servers(bot))
        logger.info("Мониторинг серверов включён, интервал %s сек", MONITOR_INTERVAL_SEC)

    logger.info("Бот @%s запущен", BOT_USERNAME or "—")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        if monitor_task is not None:
            monitor_task.cancel()
            try:
                await monitor_task
            except asyncio.CancelledError:
                pass
        await bot.session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен")
