# -*- coding: utf-8 -*-
"""
Telegram-бот: принимает tdata / session-файл, входит в аккаунт и
пересылает сообщение с кодом входа от Telegram (777000) 1в1 в чат с ботом.

Зависимости: см. requirements.txt
Запуск: python bot.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import shutil
import sqlite3
import string
import time
import zipfile
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient, events, Button, functions
from telethon.errors import PhoneNumberFloodError, AuthRestartError, RPCError
from telethon.sessions import StringSession

try:
    from opentele2.td import TDesktop
    from opentele2.api import UseCurrentSession
    from opentele2.tl import TelegramClient as OTLClient
    HAS_OPENTELE = True
except Exception:
    HAS_OPENTELE = False

load_dotenv()

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ALLOWED_IDS = {
    int(x) for x in os.getenv("ALLOWED_IDS", "").replace(";", ",").split(",") if x.strip()
}
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
TELEGRAM_SERVICE_ID = 777000
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
STARTUP_SESSION = os.getenv("STARTUP_SESSION", "").strip()
STARTUP_TDATA = os.getenv("STARTUP_TDATA", "").strip()

# ---------------------------------------------------------------- база данных
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "bot.db"
db = sqlite3.connect(str(DB_PATH), check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS users(
    user_id INTEGER PRIMARY KEY,
    username TEXT DEFAULT '',
    used INTEGER DEFAULT 0,
    max_limit INTEGER DEFAULT 10,
    ban_until INTEGER DEFAULT 0,
    ban_reason TEXT DEFAULT '',
    sup_ban_until INTEGER DEFAULT 0,
    sup_ban_reason TEXT DEFAULT '',
    joined INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS promos(
    code TEXT PRIMARY KEY,
    uses_left INTEGER,
    limit_add INTEGER,
    created INTEGER
);
CREATE TABLE IF NOT EXISTS promo_used(
    code TEXT, user_id INTEGER,
    PRIMARY KEY(code, user_id)
);
CREATE TABLE IF NOT EXISTS tickets(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER, username TEXT, status TEXT DEFAULT 'open'
);
CREATE TABLE IF NOT EXISTS settings(
    key TEXT PRIMARY KEY, value TEXT
);
CREATE TABLE IF NOT EXISTS proxies(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    line TEXT UNIQUE,
    ptype TEXT,
    host TEXT,
    port INTEGER,
    username TEXT,
    password TEXT,
    owner_id INTEGER DEFAULT 0,
    fails INTEGER DEFAULT 0,
    active INTEGER DEFAULT 1
);
""")
db.commit()
try:
    db.execute("ALTER TABLE proxies ADD COLUMN owner_id INTEGER DEFAULT 0")
    db.commit()
except Exception:
    pass


def get_setting(key: str, default=None):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value):
    db.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    db.commit()


def get_user(user_id: int, username: str = ""):
    row = db.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    if row:
        if username and username != row["username"]:
            db.execute("UPDATE users SET username=? WHERE user_id=?", (username, user_id))
            db.commit()
        return row
    db.execute(
        "INSERT INTO users(user_id, username, max_limit, joined) VALUES(?,?,?,?)",
        (user_id, username, int(get_setting("default_limit", "10")), int(time.time())),
    )
    db.commit()
    return db.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()


def ban_status(user_id: int):
    """Возвращает строку юзера, если он забанен (с учётом срока), иначе None."""
    u = get_user(user_id)
    now = int(time.time())
    if u["ban_until"] and (u["ban_until"] == -1 or u["ban_until"] > now):
        return u
    if u["ban_until"] not in (0, -1) and u["ban_until"] <= now:
        db.execute("UPDATE users SET ban_until=0 WHERE user_id=?", (user_id,))
        db.commit()
    return None


def parse_duration(s: str):
    """'12h' / '3d' / 'perm' -> секунды или -1 (навсегда)."""
    s = s.strip().lower()
    if s in ("perm", "навсегда"):
        return -1
    m = re.fullmatch(r"(\d+)([hd])", s)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    return n * 3600 if unit == "h" else n * 86400


def fmt_ban_until(ts: int) -> str:
    if ts == -1:
        return "навсегда"
    from datetime import datetime
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("codebot")
logging.getLogger("telethon").setLevel(logging.WARNING)

if not (API_ID and API_HASH and BOT_TOKEN):
    raise SystemExit("Заполни .env: API_ID, API_HASH, BOT_TOKEN (см. .env.example)")

DATA_DIR.mkdir(parents=True, exist_ok=True)

CTXS: dict = {}  # user_id -> Ctx

bot = TelegramClient(str(DATA_DIR / "bot_session"), API_ID, API_HASH)


class Ctx:
    """Аккаунт, загруженный пользователем бота."""

    def __init__(self, user_id: int):
        self.user_id = user_id
        self.client = None
        self.phone = None
        self.workdir = None
        self.waiting_chat = None  # чат, куда пересылать сообщения от 777000
        self.keepalive_task = None


def is_allowed(user_id: int) -> bool:
    return not ALLOWED_IDS or user_id in ALLOWED_IDS


def rand_tag() -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=6))


CONVERT_MODE: set = set()  # user_id — ждём архив для конвертации в tdata
PROXY_MODE: set = set()  # user_id — ждём прокси (.txt или текстом)


def parse_proxy_line(line: str):
    """Разбирает строку прокси (socks4/socks5/http, IPv4/IPv6). dict или None."""
    line = line.strip()
    if not line or line.startswith("#") or line.startswith("//"):
        return None
    scheme, rest = "socks5", line
    if "://" in line:
        scheme, rest = line.split("://", 1)
        scheme = scheme.lower()
    scheme = {"socks": "socks5", "https": "http", "ssl": "http"}.get(scheme, scheme)
    if scheme not in ("socks5", "socks4", "http"):
        return None
    username = password = ""
    if "@" in rest:
        auth, rest = rest.rsplit("@", 1)
        username, _, password = auth.partition(":")
    host, port = None, None
    if rest.startswith("["):  # IPv6 в скобках: [2a01::1]:1080
        if "]" not in rest:
            return None
        host, tail = rest[1:].split("]", 1)
        port = tail[1:] if tail.startswith(":") else ""
    else:
        parts = rest.split(":")
        if len(parts) == 2:
            host, port = parts
        elif len(parts) == 4:  # host:port:user:pass
            host, port, username, password = parts
        elif len(parts) == 3:  # host:port:user
            host, port, username = parts
        else:
            host = None
    if not host or not (port or "").isdigit() or not (1 <= int(port) <= 65535):
        return None
    return {
        "line": line,
        "ptype": scheme,
        "host": host,
        "port": int(port),
        "username": username or None,
        "password": password or None,
    }


def add_proxy_line(line: str, owner_id: int) -> str:
    """'added' / 'dup' / 'bad'. Пул у каждого юзера свой."""
    pr = parse_proxy_line(line)
    if not pr:
        return "bad"
    exists = db.execute(
        "SELECT 1 FROM proxies WHERE owner_id=? AND ptype=? AND host=? AND port=? "
        "AND IFNULL(username,'')=?",
        (owner_id, pr["ptype"], pr["host"], pr["port"], pr["username"] or ""),
    ).fetchone()
    if exists:
        return "dup"
    db.execute(
        "INSERT INTO proxies(line, ptype, host, port, username, password, owner_id) "
        "VALUES(?,?,?,?,?,?,?)",
        (pr["line"], pr["ptype"], pr["host"], pr["port"], pr["username"], pr["password"], owner_id),
    )
    db.commit()
    return "added"


def pick_proxy(owner_id: int):
    """(row, dict-прокси-для-telethon) из пула юзера или (None, None)."""
    row = db.execute(
        "SELECT * FROM proxies WHERE active=1 AND owner_id=? ORDER BY fails ASC, id ASC LIMIT 1",
        (owner_id,),
    ).fetchone()
    if not row:
        return None, None
    return row, {
        "proxy_type": row["ptype"],
        "addr": row["host"],
        "port": int(row["port"]),
        "username": row["username"] or None,
        "password": row["password"] or None,
    }


def mark_proxy_result(proxy_id, ok: bool):
    if not proxy_id:
        return
    if ok:
        db.execute("UPDATE proxies SET fails=0 WHERE id=?", (proxy_id,))
    else:
        db.execute(
            "UPDATE proxies SET fails=fails+1, "
            "active=CASE WHEN fails+1>=3 THEN 0 ELSE active END WHERE id=?",
            (proxy_id,),
        )
    db.commit()


# ---------------------------------------------------------------- файлы/архивы

ARCHIVE_EXT = {".zip", ".7z", ".rar", ".gz", ".tgz", ".tar"}


def looks_like_archive(path: Path) -> bool:
    if path.is_dir():
        return False
    if path.suffix.lower() in ARCHIVE_EXT:
        return True
    with open(path, "rb") as f:
        magic = f.read(6)
    return (
        magic[:2] == b"PK"            # zip
        or magic[:4] == b"7z\xbc\xaf"  # 7z
        or magic[:3] == b"Rar"         # rar
        or magic[:2] == b"\x1f\x8b"    # gzip
    )


def extract_archive(path: Path, dest: Path) -> str:
    """Распаковывает zip / 7z / rar / tar.gz. Возвращает имя движка."""
    with open(path, "rb") as f:
        magic = f.read(6)
    suffix = path.suffix.lower()

    if magic[:4] == b"7z\xbc\xaf" or suffix == ".7z":
        try:
            import py7zr
        except ImportError:
            raise RuntimeError("архив 7z, но не установлена библиотека py7zr (pip install py7zr)")
        with py7zr.SevenZipFile(str(path)) as z:
            z.extractall(str(dest))
        return "7z"

    if magic[:3] == b"Rar" or suffix == ".rar":
        try:
            import rarfile
        except ImportError:
            raise RuntimeError("архив rar, но не установлена библиотека rarfile (pip install rarfile)")
        rf = rarfile.RarFile(str(path))
        rf.extractall(str(dest))
        return "rar"

    try:
        with zipfile.ZipFile(str(path)) as z:
            z.extractall(str(dest))
        return "zip"
    except zipfile.BadZipFile:
        pass

    import tarfile
    if tarfile.is_tarfile(str(path)):
        with tarfile.open(str(path)) as tf:
            tf.extractall(str(dest), filter="data")
        return "tar"

    raise RuntimeError("не удалось распаковать архив (поддержка: zip, 7z, rar, tar.gz)")


def find_tdata_dirs(root: Path) -> list:
    """Ищет каталоги, похожие на tdata (сама папка tdata или её содержимое)."""
    found: list = []
    for p in root.rglob("*"):
        if p.is_dir() and p.name.lower() == "tdata":
            found.append(p)
    if not found:
        # архив может содержать содержимое tdata без самой папки
        for name in ("key_ds", "usertag"):
            for p in root.rglob(name):
                if p.is_file() and p.parent not in found:
                    found.append(p.parent)
    return sorted(found, key=lambda x: str(x))


def find_session_files(root: Path) -> list:
    return [p for p in sorted(root.rglob("*")) if p.is_file() and p.suffix.lower() == ".session"]


def find_string_session(root: Path):
    """Ищет строку Telethon-сессии в json/txt файлах."""
    keys = ("session", "string_session", "session_string", "telethon_session", "session_str")
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in {".json", ".txt"}:
            continue
        try:
            raw = p.read_text(encoding="utf-8", errors="ignore").strip()
            data = json.loads(raw)
        except Exception:
            data = raw if len(raw) > 40 else None
        if isinstance(data, dict):
            for k in keys:
                v = data.get(k)
                if isinstance(v, str) and len(v) > 40:
                    return v
            for v in data.values():
                if isinstance(v, str) and len(v) > 40:
                    return v
        elif isinstance(data, str) and len(data) > 40:
            return data
    return None


def read_session_string(src: str) -> str:
    """Строка сессии из .env: либо сама строка, либо путь к json/txt-файлу."""
    p = Path(src)
    if not p.exists():
        return src
    data = p.read_text(encoding="utf-8", errors="ignore").strip()
    try:
        j = json.loads(data)
    except Exception:
        return data
    if isinstance(j, dict):
        for k in ("session", "string_session", "session_string", "telethon_session"):
            if isinstance(j.get(k), str) and len(j[k]) > 40:
                return j[k]
    return data


def iter_string_sessions(root: Path):
    """Все строки сессий из json/txt-файлов каталога: (имя_файла, строка)."""
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in {".json", ".txt"}:
            continue
        try:
            raw = p.read_text(encoding="utf-8", errors="ignore").strip()
            data = json.loads(raw)
        except Exception:
            if len(raw) > 40:
                yield p.name, raw
            continue
        if isinstance(data, dict):
            for k in ("session", "string_session", "session_string", "telethon_session"):
                if isinstance(data.get(k), str) and len(data[k]) > 40:
                    yield p.name, data[k]
                    break
        elif isinstance(data, str) and len(data) > 40:
            yield p.name, data


async def cleanup_ctx(ctx: Ctx, notify_chat: int | None = None):
    """Отключает клиент и удаляет рабочую папку пользователя."""
    if ctx.keepalive_task:
        ctx.keepalive_task.cancel()
    if ctx.client:
        try:
            await ctx.client.disconnect()
        except Exception:
            pass
    if ctx.workdir and ctx.workdir.exists():
        shutil.rmtree(ctx.workdir, ignore_errors=True)
    CTXS.pop(ctx.user_id, None)
    if notify_chat:
        try:
            await bot.send_message(notify_chat, "👋 Сессия отключена. Пришли новый файл, чтобы войти снова.")
        except Exception:
            pass


# ---------------------------------------------------------------- вход по файлу

async def build_client_from_tdata(ctx: Ctx, tdata_dir: Path, proxy=None) -> TelegramClient:
    if not HAS_OPENTELE:
        raise RuntimeError("opentele2 не установлена — конвертация tdata невозможна (pip install opentele2)")
    tdesk = TDesktop(str(tdata_dir))
    if not tdesk.isLoaded():
        raise RuntimeError("tdata не читается")
    session_path = ctx.workdir / f"converted_{rand_tag()}"
    client = await tdesk.ToTelethon(session=str(session_path), flag=UseCurrentSession, proxy=proxy)
    return client


async def try_client_login(client: TelegramClient):
    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError("сессия не авторизована / ключ невалиден")
    return await client.get_me()


async def report_login_success(chat_id: int, me):
    name = (me.first_name or "").strip()
    if me.last_name:
        name += f" {me.last_name}"
    username = f"@{me.username}" if me.username else "—"
    await bot.send_message(
        chat_id,
        "✅ Вход успешный\n\n"
        f"📱 Номер: `{me.phone}`\n"
        f"👤 Имя: {name}\n"
        f"🔗 Username: {username}",
        buttons=[[Button.inline("📨 Запросить код", b"req_code")]],
    )


async def handle_upload(event) -> None:
    user_id = event.sender_id
    sender = await event.get_sender()
    u = get_user(user_id, (sender.username if sender else "") or "")

    b = ban_status(user_id)
    if b and user_id != ADMIN_ID:
        await event.reply(
            "🚫 Вы заблокированы в боте."
            + (f"\nПричина: {b['ban_reason']}" if b["ban_reason"] else "")
        )
        return
    if user_id != ADMIN_ID and u["used"] >= u["max_limit"]:
        await event.reply(get_setting("limit_message", "❌ У вас закончился лимит."))
        return

    workdir = DATA_DIR / str(user_id) / rand_tag()
    workdir.mkdir(parents=True, exist_ok=True)

    status = await event.reply("💱 Файл получен, обрабатываю…")
    downloaded = await event.download_media(file=str(workdir))
    if not downloaded:
        await status.edit("🚫 Не вижу файла. Пришли файл (tdata.zip / *.session / session.json).")
        return
    src = Path(downloaded)
    log.info("user %s прислал %s", user_id, src.name)

    old = CTXS.get(user_id)
    if old:
        await cleanup_ctx(old)

    ctx = Ctx(user_id)
    ctx.workdir = workdir
    CTXS[user_id] = ctx

    proxy_row, proxy = pick_proxy(user_id)
    try:
        if looks_like_archive(src):
            engine = extract_archive(src, workdir / "extracted")
            log.info("распаковано движком %s", engine)
            extracted = workdir / "extracted"
            tdata_dirs = find_tdata_dirs(extracted)
            session_files = find_session_files(extracted)
            string_session = find_string_session(extracted)

            if tdata_dirs:
                client = None
                for td in tdata_dirs:
                    try:
                        client = await build_client_from_tdata(ctx, td, proxy=proxy)
                        break
                    except Exception as e:
                        log.warning("tdata %s не загрузилась: %s", td, e)
                if client is None:
                    raise RuntimeError("tdata внутри архива невалидна")
            elif session_files:
                client = TelegramClient(
                    str(session_files[0].with_suffix("")), API_ID, API_HASH, proxy=proxy
                )
            elif string_session:
                client = TelegramClient(StringSession(string_session), API_ID, API_HASH, proxy=proxy)
            else:
                raise RuntimeError("в архиве не найдено ни tdata, ни session-файла, ни строки сессии")
        elif src.suffix.lower() == ".session":
            client = TelegramClient(str(src.with_suffix("")), API_ID, API_HASH, proxy=proxy)
        elif src.suffix.lower() in {".json", ".txt"}:
            s = find_string_session(workdir)
            if not s:
                raise RuntimeError("в файле не найдена строка сессии")
            client = TelegramClient(StringSession(s), API_ID, API_HASH, proxy=proxy)
        else:
            raise RuntimeError(
                "пришли файл: tdata (zip/7z/rar), *.session или session.json со строкой сессии"
            )

        ctx.client = client
        me = await try_client_login(ctx.client)
        ctx.phone = me.phone
        register_service_listener(ctx)

        await status.delete()
        await report_login_success(event.chat_id, me)
        mark_proxy_result(proxy_row["id"], True)
        db.execute("UPDATE users SET used=used+1 WHERE user_id=?", (user_id,))
        db.commit()
    except Exception as e:
        if proxy_row and isinstance(e, (ConnectionError, TimeoutError, OSError)):
            mark_proxy_result(proxy_row["id"], False)
        log.exception("login failed")
        await status.edit(f"🚫 Вход не успешный\n\nПричина: {e}")
        await cleanup_ctx(ctx)
    finally:
        try:
            src.unlink(missing_ok=True)
        except Exception:
            pass


# ---------------------------------------------------------------- ожидание кода

def register_service_listener(ctx: Ctx) -> None:
    """Пересылает ВСЕ сообщения от Telegram (777000) в чат пользователя."""

    @ctx.client.on(events.NewMessage(from_users=TELEGRAM_SERVICE_ID))
    async def on_service(m):
        if not ctx.waiting_chat and ADMIN_ID:
            ctx.waiting_chat = ADMIN_ID
        if not ctx.waiting_chat:
            return
        text = (m.raw_text or "").strip()
        if not text:
            return
        try:
            await bot.send_message(ctx.waiting_chat, text)
        except Exception as e:
            log.warning("не удалось переслать сообщение от 777000: %s", e)

    # держим соединение аккаунта живым, чтобы ничего не пропустить
    async def keepalive():
        while True:
            await asyncio.sleep(60)
            try:
                if not ctx.client.is_connected():
                    await ctx.client.connect()
                    log.info("клиент аккаунта %s переподключён", ctx.phone)
            except Exception as e:
                log.warning("keepalive аккаунта %s: %s", ctx.phone, e)

    ctx.keepalive_task = asyncio.create_task(keepalive())


async def request_code_from_session(ctx: Ctx) -> str:
    """Запрашивает код через сессию, не роняя клиент. Возвращает текст-статус."""
    client = ctx.client
    try:
        if not client.is_connected():
            await client.connect()
        if not await client.get_me():
            return "🚫 Сессия больше не жива — пришли файл заново."
    except Exception as e:
        log.warning("клиент аккаунта %s не отвечает: %s", ctx.phone, e)
        return "🚫 Сессия не отвечает — пришли файл заново."

    phone_digits = re.sub(r"\D", "", ctx.phone)
    for attempt in (1, 2):
        try:
            await client(
                functions.auth.SendCodeRequest(
                    phone_number=phone_digits, api_id=API_ID, api_hash=API_HASH, settings=None
                )
            )
            return "✅ Код запрошен на аккаунте — сейчас придёт сообщение от Telegram."
        except AuthRestartError:
            log.warning("AuthRestartError (попытка %d), повтор через 3 с", attempt)
            await asyncio.sleep(3)
        except (PhoneNumberFloodError, RPCError) as e:
            log.warning("SendCodeRequest: %s", e)
            break
        except Exception as e:
            log.warning("SendCodeRequest упал: %s", e)
            break

    try:
        if not client.is_connected():
            await client.connect()
    except Exception as e:
        log.warning("реконнект после запроса кода: %s", e)
    return (
        "ℹ️ Запросить код с сессии не вышло — но пересылка от Telegram уже работает. "
        "Войди по номеру со своего устройства: код придёт в аккаунт, и я его перешлю."
    )


async def on_req_code(event):
    user_id = event.sender_id
    ctx = CTXS.get(user_id) or CTXS.get(0)  # CTXS[0] — сессия автовхода
    if not ctx or not ctx.client or not ctx.phone:
        await event.answer("Сессия не активна — пришли файл заново", alert=True)
        return
    if ctx.user_id and ctx.user_id != user_id and user_id != ADMIN_ID:
        await event.answer("Это не твоя сессия 🙂", alert=True)
        return

    # пересылаем ВСЁ от 777000 в этот чат, пока сессия активна
    ctx.waiting_chat = event.chat_id
    note = await request_code_from_session(ctx)
    await event.answer()
    await event.reply(f"🔔 Пересылка сообщений от Telegram (777000) включена.\n{note}")


# ---------------------------------------------------------------- хэндлеры бота

START_TEXT = (
    "👋 Привет!\n\n"
    "1️⃣ Пришли файл: tdata (zip/7z/rar), *.session или session.json — я войду в аккаунт.\n"
    "2️⃣ Я пришлю «✅ Вход успешный» и номер аккаунта.\n"
    "3️⃣ Жми «📨 Запросить код» — я запрошу код входа и перешлю сообщение Telegram с кодом 1в1:\n"
    "«Код для входа в Telegram: 12345…»\n\n"
    "ℹ️ Команды: /start, /stop (отключить сессию), /profile, /support, /converter, /proxy."
)


@bot.on(events.NewMessage(pattern="/start|/help"))
async def cmd_start(event):
    await event.reply(START_TEXT)


@bot.on(events.NewMessage(pattern="/stop"))
async def cmd_stop(event):
    ctx = CTXS.get(event.sender_id) or CTXS.get(0)  # CTXS[0] — сессия автовхода
    if not ctx:
        await event.reply("Нет активной сессии.")
        return
    await cleanup_ctx(ctx, notify_chat=event.chat_id)


async def admin_only(event) -> bool:
    if event.sender_id != ADMIN_ID:
        await event.reply("⛔ У вас нет доступа к этой команде.")
        return False
    return True


def display_name(sender) -> str:
    return f"@{sender.username}" if sender and sender.username else "—"


@bot.on(events.NewMessage(pattern=r"^/profile$"))
async def cmd_profile(event):
    user_id = event.sender_id
    sender = await event.get_sender()
    u = get_user(user_id, (sender.username or "") if sender else "")
    b = ban_status(user_id)
    lines = [f"👤 {display_name(sender)}", f"ID: {user_id}"]
    if b:
        lines.append("🚫 Заблокирован" + (f" — {b['ban_reason']}" if b["ban_reason"] else ""))
    lines.append(f"📉 Лимит: {u['used']}/{u['max_limit']}")
    lines.append("ℹ Лимит можно поднять через /support")
    await event.reply("\n".join(lines))


@bot.on(events.NewMessage(pattern=r"^/promo\s+(\S+)$"))
async def cmd_promo(event):
    user_id = event.sender_id
    sender = await event.get_sender()
    get_user(user_id, (sender.username or "") if sender else "")
    b = ban_status(user_id)
    if b and user_id != ADMIN_ID:
        await event.reply(
            "🚫 Вы заблокированы в боте."
            + (f"\nПричина: {b['ban_reason']}" if b["ban_reason"] else "")
        )
        return
    code = event.pattern_match.group(1).upper()
    p = db.execute("SELECT * FROM promos WHERE code=?", (code,)).fetchone()
    if not p:
        await event.reply("🚫 Промокод не найден.")
        return
    if db.execute("SELECT 1 FROM promo_used WHERE code=? AND user_id=?", (code, user_id)).fetchone():
        await event.reply("🚫 Ты уже активировал этот промокод.")
        return
    if p["uses_left"] <= 0:
        await event.reply("🚫 Промокод закончился.")
        return
    db.execute("UPDATE users SET max_limit=max_limit+? WHERE user_id=?", (p["limit_add"], user_id))
    db.execute("UPDATE promos SET uses_left=uses_left-1 WHERE code=?", (code,))
    db.execute("INSERT INTO promo_used VALUES(?,?)", (code, user_id))
    db.commit()
    u = get_user(user_id)
    await event.reply(
        f"✅ Промокод активирован: +{p['limit_add']} к лимиту.\n📉 Лимит: {u['used']}/{u['max_limit']}"
    )


@bot.on(events.NewMessage(pattern=r"^/support$"))
async def cmd_support(event):
    user_id = event.sender_id
    sender = await event.get_sender()
    u = get_user(user_id, (sender.username or "") if sender else "")
    now = int(time.time())
    if u["sup_ban_until"] and (u["sup_ban_until"] == -1 or u["sup_ban_until"] > now):
        await event.reply(
            "⛔ Вам запрещен доступ к этой команде."
            + (f"\nПричина: {u['sup_ban_reason']}" if u["sup_ban_reason"] else "")
        )
        return
    if not ADMIN_ID:
        await event.reply("⛔ Поддержка сейчас недоступна.")
        return
    row = db.execute(
        "SELECT id FROM tickets WHERE user_id=? AND status='open'", (user_id,)
    ).fetchone()
    if row:
        await event.reply(f"🎫 У тебя уже открыт тикет #{row['id']} — просто напиши сообщение.")
        return
    cur = db.execute(
        "INSERT INTO tickets(user_id, username, status) VALUES(?,?,'open')",
        (user_id, (sender.username or "") if sender else ""),
    )
    db.commit()
    await event.reply(
        f"🎫 Тикет #{cur.lastrowid} создан. Напиши вопрос одним сообщением — передам админу."
    )


@bot.on(events.NewMessage(pattern=r"^/converter$"))
async def cmd_converter(event):
    user_id = event.sender_id
    sender = await event.get_sender()
    get_user(user_id, (sender.username or "") if sender else "")
    b = ban_status(user_id)
    if b and user_id != ADMIN_ID:
        await event.reply("🚫 Вы заблокированы в боте.")
        return
    if not HAS_OPENTELE:
        await event.reply("🚫 Конвертация недоступна: не установлена opentele2 (pip install opentele2).")
        return
    CONVERT_MODE.add(user_id)
    await event.reply(
        "🔁 Режим конвертации включён: пришли архив (zip/7z/rar) с .session или session.json — "
        "верну tdata-архив. (/cancel — отмена)"
    )


async def handle_convert(event):
    user_id = event.sender_id
    CONVERT_MODE.discard(user_id)
    if not HAS_OPENTELE:
        await event.reply("🚫 Конвертация недоступна: не установлена opentele2.")
        return
    workdir = DATA_DIR / str(user_id) / rand_tag()
    workdir.mkdir(parents=True, exist_ok=True)
    status = await event.reply("🔁 Конвертирую в tdata…")
    downloaded = await event.download_media(file=str(workdir))
    if not downloaded:
        await status.edit("🚫 Не вижу файла.")
        return
    src = Path(downloaded)
    try:
        if looks_like_archive(src):
            extract_archive(src, workdir / "extracted")
            root = workdir / "extracted"
        else:
            root = workdir

        jobs = []
        for sf in find_session_files(root):
            jobs.append((sf.stem, "session", str(sf.with_suffix(""))))
        for fname, s in iter_string_sessions(root):
            jobs.append((Path(fname).stem, "string", s))

        if not jobs:
            await status.edit("🚫 В архиве не найдено .session или session.json.")
            return

        converted, errors = [], []
        for i, (name, kind, val) in enumerate(jobs, 1):
            tdata_dir = workdir / f"tdata_{i}"
            try:
                if kind == "session":
                    client = OTLClient(val, API_ID, API_HASH)
                else:
                    client = OTLClient(StringSession(val), API_ID, API_HASH)
                await client.connect()
                try:
                    tdesk = await client.ToTDesktop(flag=UseCurrentSession)
                    tdesk.SaveTData(str(tdata_dir))
                finally:
                    await client.disconnect()
                converted.append((name, tdata_dir))
            except Exception as e:
                errors.append(f"{name}: {e}")
                log.warning("конвертация %s не удалась: %s", name, e)

        if not converted:
            await status.edit(
                "🚫 Ни одна сессия не сконвертирована:\n" + "\n".join(errors[:5])
            )
            return

        zip_path = workdir / "tdata.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for idx, (name, tdir) in enumerate(converted, 1):
                folder = "tdata" if len(converted) == 1 else f"tdata_{idx}"
                for f in tdir.rglob("*"):
                    if f.is_file():
                        z.write(f, Path(folder) / f.relative_to(tdir))

        cap = f"🔁 Готово: tdata из {len(converted)} сессий."
        if errors:
            cap += f"\nНе удалось: {len(errors)}."
        await status.delete()
        await bot.send_file(event.chat_id, str(zip_path), caption=cap)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@bot.on(events.NewMessage(pattern=r"^/proxy(?:\s+(list|clear|del)\s*(\S+)?)?$"))
async def cmd_proxy(event):
    user_id = event.sender_id
    sender = await event.get_sender()
    get_user(user_id, (sender.username or "") if sender else "")
    b = ban_status(user_id)
    if b and user_id != ADMIN_ID:
        await event.reply("🚫 Вы заблокированы в боте.")
        return
    sub, arg = event.pattern_match.group(1), event.pattern_match.group(2)
    total = db.execute(
        "SELECT COUNT(*) c FROM proxies WHERE active=1 AND owner_id=?", (user_id,)
    ).fetchone()["c"]

    if sub == "clear":
        db.execute("DELETE FROM proxies WHERE owner_id=?", (user_id,))
        db.commit()
        await event.reply("🗑 Твой прокси-пул очищен.")
        return

    if sub == "del":
        if not arg or not arg.isdigit():
            await event.reply("🚫 Формат: /proxy del ID — ID смотри в /proxy list")
            return
        t_id = int(arg)
        row = db.execute(
            "SELECT * FROM proxies WHERE id=? AND owner_id=?", (t_id, user_id)
        ).fetchone()
        if not row:
            await event.reply("🚫 Прокси с таким ID не найден в твоём пуле.")
            return
        db.execute("DELETE FROM proxies WHERE id=?", (t_id,))
        db.commit()
        await event.reply(f"🗑 Прокси #{t_id} удалён из твоего пула.")
        return

    if sub == "list":
        rows = db.execute(
            "SELECT * FROM proxies WHERE owner_id=? ORDER BY id LIMIT 15", (user_id,)
        ).fetchall()
        if not rows:
            await event.reply("📭 В твоём пуле прокси нет. Добавь: /proxy")
            return
        lines = [
            f"{r['id']}. [{r['ptype']}] {r['host']}:{r['port']} — "
            f"{'OK' if r['fails'] == 0 else 'сбоев: ' + str(r['fails'])}"
            + ("" if r["active"] else " (выкл)")
            for r in rows
        ]
        await event.reply("🗃 Твой прокси-пул:\n" + "\n".join(lines) + "\n\n🗑 Удалить: /proxy del ID")
        return

    PROXY_MODE.add(user_id)
    await event.reply(
        f"🌐 В твоём пуле (активных): {total}\n\n"
        "📥 Пришли .txt (по прокси в строке) или просто текстом в чат.\n"
        "Форматы: socks5://user:pass@host:port, http://host:port, "
        "host:port:user:pass, IPv6 — [адрес]:порт.\n"
        "Входы в твои аккаунты пойдут через них; если пул пуст — без прокси.\n"
        "📋 /proxy list — твой пул, /proxy del ID — удалить, /proxy clear — очистить пул.\n"
        "/cancel — отмена."
    )


async def handle_proxy_file(event):
    user_id = event.sender_id
    PROXY_MODE.discard(user_id)
    workdir = DATA_DIR / str(user_id) / rand_tag()
    workdir.mkdir(parents=True, exist_ok=True)
    downloaded = await event.download_media(file=str(workdir))
    if not downloaded:
        await event.reply("🚫 Не вижу файла.")
        return
    text = Path(downloaded).read_text(encoding="utf-8", errors="ignore")
    shutil.rmtree(workdir, ignore_errors=True)
    added = dup = bad = 0
    for line in text.splitlines():
        res = add_proxy_line(line, user_id)
        if res == "added":
            added += 1
        elif res == "dup":
            dup += 1
        else:
            bad += 1
    total = db.execute(
        "SELECT COUNT(*) c FROM proxies WHERE active=1 AND owner_id=?", (user_id,)
    ).fetchone()["c"]
    await event.reply(
        f"🌐 Прокси добавлено: {added} (дублей: {dup}, битых: {bad}). "
        f"Активных в твоём пуле: {total}."
    )


# ---------------------------------------------------------------- админка

ADMIN_STATE: dict = {}
ADMIN_TICKET_MAP: dict = {}  # msg_id в чате админа -> (ticket_id, user_id)


def admin_menu():
    return [
        [Button.inline("📢 Рассылка", b"adm:broadcast"), Button.inline("📊 Статистика", b"adm:stats")],
        [Button.inline("🎟 Создать промокод", b"adm:promo")],
        [Button.inline("⚙ Настройки", b"adm:settings")],
        [Button.inline("✖ Закрыть панель", b"adm:close")],
    ]


@bot.on(events.NewMessage(pattern=r"^/tryadmin$"))
async def cmd_tryadmin(event):
    if not await admin_only(event):
        return
    ADMIN_STATE[event.sender_id] = None
    await event.reply("👑 Админ-панель", buttons=admin_menu())


@bot.on(events.CallbackQuery(pattern=rb"^adm:"))
async def cb_admin(event):
    if event.sender_id != ADMIN_ID:
        await event.answer("⛔ У вас нет доступа", alert=True)
        return
    data = event.data.decode()
    if data == "adm:menu":
        await event.edit("👑 Админ-панель", buttons=admin_menu())
    elif data == "adm:broadcast":
        ADMIN_STATE[event.sender_id] = "broadcast"
        await event.edit("📢 Пришли текст рассылки следующим сообщением (/cancel — отмена).")
    elif data == "adm:promo":
        ADMIN_STATE[event.sender_id] = "promo_create"
        await event.edit(
            "🎟 Пришли промокод в формате:\nКОД использований прибавка_к_лимиту\n"
            "Например: FUN10 100 1\n(/cancel — отмена)"
        )
    elif data == "adm:stats":
        users_total = db.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        banned = db.execute("SELECT COUNT(*) c FROM users WHERE ban_until != 0").fetchone()["c"]
        tickets_open = db.execute(
            "SELECT COUNT(*) c FROM tickets WHERE status='open'"
        ).fetchone()["c"]
        promos_active = db.execute(
            "SELECT COALESCE(SUM(uses_left),0) c FROM promos"
        ).fetchone()["c"]
        activations = db.execute("SELECT COUNT(*) c FROM promo_used").fetchone()["c"]
        logins = db.execute("SELECT COALESCE(SUM(used),0) c FROM users").fetchone()["c"]
        await event.edit(
            "📊 Статистика:\n\n"
            f"👥 Пользователей: {users_total}\n"
            f"🚫 Забанено: {banned}\n"
            f"🎫 Открытых тикетов: {tickets_open}\n"
            f"🎟 Осталось активаций промокодов: {promos_active}\n"
            f"♻ Активаций всего: {activations}\n"
            f"📁 Всего входов: {logins}"
        )
    elif data == "adm:settings":
        await event.edit(
            "⚙ Настройки:\n\n"
            f"Дефолтный лимит: {get_setting('default_limit', '10')}\n"
            f"Сообщение о лимите: {get_setting('limit_message', '—')}\n\n"
            "Команды:\n"
            "/setlimit N — лимит новым юзерам\n"
            "/setmsg текст — сообщение при исчерпании лимита\n"
            "/setuserlimit ID N — лимит конкретному юзеру"
        )
    elif data == "adm:close":
        ADMIN_STATE[event.sender_id] = None
        await event.edit("👑 Панель закрыта.", buttons=None)


async def handle_admin_state(event, text):
    state = ADMIN_STATE.get(event.sender_id)
    if state == "broadcast":
        ADMIN_STATE[event.sender_id] = None
        rows = db.execute("SELECT user_id FROM users").fetchall()
        ok = fail = 0
        for r in rows:
            try:
                await bot.send_message(r["user_id"], text)
                ok += 1
            except Exception:
                fail += 1
            await asyncio.sleep(0.05)
        await event.reply(f"📤 Рассылка завершена: доставлено {ok}, не доставлено {fail}.")
    elif state == "promo_create":
        parts = text.split()
        if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
            await event.reply("🚫 Формат: КОД использований прибавка — например: FUN10 100 1")
            return
        code, uses, add = parts[0].upper(), int(parts[1]), int(parts[2])
        db.execute(
            "INSERT OR REPLACE INTO promos VALUES(?,?,?,?)", (code, uses, add, int(time.time()))
        )
        db.commit()
        ADMIN_STATE[event.sender_id] = None
        await event.reply(f"🎟 Промокод {code} создан: {uses} использований, +{add} к лимиту.")


@bot.on(events.NewMessage(pattern=r"^/cancel$"))
async def cmd_cancel(event):
    ADMIN_STATE.pop(event.sender_id, None)
    CONVERT_MODE.discard(event.sender_id)
    PROXY_MODE.discard(event.sender_id)
    await event.reply("👌 Отменено.")


@bot.on(events.NewMessage(pattern=r"^/setlimit\s+(\d+)$"))
async def cmd_setlimit(event):
    if not await admin_only(event):
        return
    set_setting("default_limit", event.pattern_match.group(1))
    await event.reply(f"✅ Дефолтный лимит для новых юзеров: {event.pattern_match.group(1)}")


@bot.on(events.NewMessage(pattern=r"^/setmsg\s+([\s\S]+)$"))
async def cmd_setmsg(event):
    if not await admin_only(event):
        return
    set_setting("limit_message", event.pattern_match.group(1).strip())
    await event.reply("✅ Сообщение о лимите обновлено.")


@bot.on(events.NewMessage(pattern=r"^/setuserlimit\s+(\d+)\s+(\d+)$"))
async def cmd_setuserlimit(event):
    if not await admin_only(event):
        return
    uid, n = int(event.pattern_match.group(1)), int(event.pattern_match.group(2))
    get_user(uid)
    db.execute("UPDATE users SET max_limit=? WHERE user_id=?", (n, uid))
    db.commit()
    await event.reply(f"✅ Лимит юзера {uid}: {n}")


@bot.on(events.NewMessage(pattern=r"^/ban\s+(\d+)\s+(\S+)(?:\s+([\s\S]+))?$"))
async def cmd_ban(event):
    if not await admin_only(event):
        return
    target = int(event.pattern_match.group(1))
    dur = parse_duration(event.pattern_match.group(2))
    reason = (event.pattern_match.group(3) or "").strip()
    if dur is None:
        await event.reply("🚫 Срок: 12h / 3d / perm. Пример: /ban 123 3d читерство")
        return
    get_user(target)
    db.execute(
        "UPDATE users SET ban_until=?, ban_reason=? WHERE user_id=?", (dur, reason, target)
    )
    db.commit()
    try:
        await bot.send_message(
            target, "🚫 Вы заблокированы в боте." + (f"\nПричина: {reason}" if reason else "")
        )
    except Exception:
        pass
    await event.reply(f"✅ Пользователь {target} забанен до {fmt_ban_until(dur)}.")


@bot.on(events.NewMessage(pattern=r"^/bansupport\s+(\d+)\s+(\S+)(?:\s+([\s\S]+))?$"))
async def cmd_bansupport(event):
    if not await admin_only(event):
        return
    target = int(event.pattern_match.group(1))
    dur = parse_duration(event.pattern_match.group(2))
    reason = (event.pattern_match.group(3) or "").strip()
    if dur is None:
        await event.reply("🚫 Срок: 12h / 3d / perm. Пример: /bansupport 123 24h спам")
        return
    get_user(target)
    db.execute(
        "UPDATE users SET sup_ban_until=?, sup_ban_reason=? WHERE user_id=?",
        (dur, reason, target),
    )
    db.commit()
    await event.reply(f"✅ Юзеру {target} запрещён /support до {fmt_ban_until(dur)}.")


@bot.on(events.NewMessage(pattern=r"^/unban\s+(\d+)$"))
async def cmd_unban(event):
    if not await admin_only(event):
        return
    target = int(event.pattern_match.group(1))
    db.execute("UPDATE users SET ban_until=0, ban_reason='' WHERE user_id=?", (target,))
    db.commit()
    await event.reply(f"✅ Пользователь {target} разбанен.")


@bot.on(events.NewMessage(pattern=r"^/unbansupport\s+(\d+)$"))
async def cmd_unbansupport(event):
    if not await admin_only(event):
        return
    target = int(event.pattern_match.group(1))
    db.execute("UPDATE users SET sup_ban_until=0, sup_ban_reason='' WHERE user_id=?", (target,))
    db.commit()
    await event.reply(f"✅ Юзеру {target} вернули /support.")


@bot.on(events.CallbackQuery(pattern=rb"^tk:close:"))
async def cb_close_ticket(event):
    if event.sender_id != ADMIN_ID:
        await event.answer("⛔ У вас нет доступа", alert=True)
        return
    tid = int(event.data.decode().split(":")[2])
    row = db.execute("SELECT user_id FROM tickets WHERE id=?", (tid,)).fetchone()
    db.execute("UPDATE tickets SET status='closed' WHERE id=?", (tid,))
    db.commit()
    if event.message:
        ADMIN_TICKET_MAP.pop(event.message.id, None)
    await event.edit("🎫 Тикет закрыт.")
    if row:
        try:
            await bot.send_message(row["user_id"], f"🔒 Тикет #{tid} закрыт.")
        except Exception:
            pass


@bot.on(events.NewMessage(func=lambda e: e.chat_id is not None and e.chat_id > 0))
async def on_message(event):
    text = (event.raw_text or "").strip()
    if text and not event.media:
        if text.startswith("/"):
            return  # команды обрабатываются своими хэндлерами
        user_id = event.sender_id

        if user_id != ADMIN_ID:
            b = ban_status(user_id)
            if b:
                await event.reply(
                    "🚫 Вы заблокированы в боте."
                    + (f"\nПричина: {b['ban_reason']}" if b["ban_reason"] else "")
                )
                return

        # админ: ожидание ввода для админки
        if user_id == ADMIN_ID and ADMIN_STATE.get(user_id):
            await handle_admin_state(event, text)
            return

        # админ: ответ на пересланное сообщение тикета
        if user_id == ADMIN_ID and event.message.reply_to_msg_id in ADMIN_TICKET_MAP:
            tid, uid = ADMIN_TICKET_MAP[event.message.reply_to_msg_id]
            await bot.send_message(uid, f"💬 Ответ поддержки:\n\n{text}")
            await event.reply(f"📨 Отправлено в тикет #{tid}.")
            return

        # режим добавления прокси
        if user_id in PROXY_MODE:
            added = dup = bad = 0
            for line in text.splitlines():
                res = add_proxy_line(line, user_id)
                if res == "added":
                    added += 1
                elif res == "dup":
                    dup += 1
                else:
                    bad += 1
            PROXY_MODE.discard(user_id)
            total = db.execute(
                "SELECT COUNT(*) c FROM proxies WHERE active=1 AND owner_id=?", (user_id,)
            ).fetchone()["c"]
            await event.reply(
                f"🌐 Прокси добавлено: {added} (дублей: {dup}, битых: {bad}). "
                f"Активных в твоём пуле: {total}."
            )
            return

        # тикет: пересылаем сообщение админу
        t = db.execute(
            "SELECT id FROM tickets WHERE user_id=? AND status='open'", (user_id,)
        ).fetchone()
        if t:
            sender = await event.get_sender()
            uname = f"@{sender.username}" if sender and sender.username else f"id{user_id}"
            if not ADMIN_ID:
                await event.reply("⛔ Поддержка сейчас недоступна.")
                return
            msg = await bot.send_message(
                ADMIN_ID,
                f"🎫 Тикет #{t['id']}\n👤 {uname} | ID: {user_id}\n\n{text}",
                buttons=[[Button.inline("🔒 Закрыть тикет", f"tk:close:{t['id']}".encode())]],
            )
            ADMIN_TICKET_MAP[msg.id] = (t["id"], user_id)
            await event.reply("📨 Отправлено админу.")
            return

        await handle_text_session(event)
        return
    if event.media and is_allowed(event.sender_id):
        if event.sender_id in CONVERT_MODE:
            await handle_convert(event)
        elif event.sender_id in PROXY_MODE:
            await handle_proxy_file(event)
        else:
            await handle_upload(event)


async def handle_text_session(event):
    s = (event.raw_text or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_\-]{40,}", s):
        return
    old = CTXS.get(event.sender_id)
    if old:
        await cleanup_ctx(old)

    workdir = DATA_DIR / str(event.sender_id) / rand_tag()
    workdir.mkdir(parents=True, exist_ok=True)
    ctx = Ctx(event.sender_id)
    ctx.workdir = workdir
    CTXS[event.sender_id] = ctx
    proxy_row, proxy = pick_proxy(event.sender_id)
    try:
        ctx.client = TelegramClient(StringSession(s), API_ID, API_HASH, proxy=proxy)
        me = await try_client_login(ctx.client)
        ctx.phone = me.phone
        register_service_listener(ctx)
        await report_login_success(event.chat_id, me)
        mark_proxy_result(proxy_row["id"], True)
    except Exception as e:
        if proxy_row and isinstance(e, (ConnectionError, TimeoutError, OSError)):
            mark_proxy_result(proxy_row["id"], False)
        await event.reply(f"🚫 Вход не успешный\n\nПричина: {e}")
        await cleanup_ctx(ctx)


@bot.on(events.CallbackQuery(data=b"req_code"))
async def cb_req_code(event):
    await on_req_code(event)


async def startup_login():
    """Автовход по сессии/tdata из .env (STARTUP_SESSION / STARTUP_TDATA)."""
    uid = ADMIN_ID or 0
    old = CTXS.get(uid)
    if old:
        await cleanup_ctx(old)
    ctx = Ctx(uid)
    ctx.workdir = DATA_DIR / "startup" / rand_tag()
    ctx.workdir.mkdir(parents=True, exist_ok=True)
    CTXS[uid] = ctx
    proxy_row, proxy = pick_proxy(ADMIN_ID or 0)
    try:
        if STARTUP_SESSION:
            src = read_session_string(STARTUP_SESSION)
            sfile = Path(STARTUP_SESSION)
            if sfile.exists() and sfile.suffix.lower() == ".session":
                ctx.client = TelegramClient(str(sfile.with_suffix("")), API_ID, API_HASH, proxy=proxy)
            else:
                ctx.client = TelegramClient(StringSession(src), API_ID, API_HASH, proxy=proxy)
        elif STARTUP_TDATA:
            tpath = Path(STARTUP_TDATA)
            if looks_like_archive(tpath):
                extract_archive(tpath, ctx.workdir / "extracted")
                tds = find_tdata_dirs(ctx.workdir / "extracted")
                if not tds:
                    raise RuntimeError("в архиве не найдена папка tdata")
                ctx.client = await build_client_from_tdata(ctx, tds[0], proxy=proxy)
            elif tpath.is_dir() and (tpath.name.lower() == "tdata" or (tpath / "key_ds").exists()):
                ctx.client = await build_client_from_tdata(ctx, tpath, proxy=proxy)
            else:
                raise RuntimeError("STARTUP_TDATA: укажи папку tdata, архив с ней или *.session файл")
        else:
            return
        me_ = await try_client_login(ctx.client)
        ctx.phone = me_.phone
        register_service_listener(ctx)
        mark_proxy_result(proxy_row["id"], True)
        log.info("Автовход выполнен: %s", me_.phone)
        if ADMIN_ID:
            await report_login_success(ADMIN_ID, me_)
    except Exception as e:
        if proxy_row and isinstance(e, (ConnectionError, TimeoutError, OSError)):
            mark_proxy_result(proxy_row["id"], False)
        log.error("Автовход не удался: %s", e)
        await cleanup_ctx(ctx)


async def main():
    await bot.start(bot_token=BOT_TOKEN)
    me = await bot.get_me()
    log.info("Бот запущен: @%s", me.username)
    if STARTUP_SESSION or STARTUP_TDATA:
        await startup_login()
    await bot.run_until_disconnected()


if __name__ == "__main__":
    loop = getattr(bot, "loop", None) or asyncio.get_event_loop()
    loop.run_until_complete(main())
