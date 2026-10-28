# ============================================================
# AUTO FORWARDER TELEGRAM - VERSION CORRIGEE
# Nautiljon -> AniList (complement/fallback)
# ============================================================

import os
import re
import sys
import json
import asyncio
import logging
import hashlib
import io
import tempfile
import time
import unicodedata
from difflib import SequenceMatcher
from typing import Optional, List, Dict, Any, Tuple

import requests
from deep_translator import GoogleTranslator
from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.request import HTTPXRequest

# ============================================================
# CONFIGURATION
# ============================================================

def load_simple_dotenv(path: str = ".env"):
    """Charge les variables simples de .env sans dépendance externe."""
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as env_file:
            for raw_line in env_file:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key:
                    os.environ.setdefault(key, value)
    except Exception as e:
        print(f"⚠️ Impossible de lire .env : {e}")


load_simple_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CONFIG_FILE = "config_A.json"

DEFAULT_SOURCES = [-1001694110649, -1002197415221]
DEFAULT_DESTINATION = -1001569253891
DEFAULT_STICKER_DELAY = 3 * 60
UPDATE_INTERVAL = 7 * 24 * 60 * 60


def get_env_ids(name: str) -> List[int]:
    value = os.getenv(name, "5825526159")
    result = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            result.append(int(item))
        except ValueError:
            pass
    return list(dict.fromkeys(result))


INITIAL_ADMIN_IDS = get_env_ids("ADMIN_IDS")
GITHUB_REPO = os.getenv("GITHUB_REPO", "").strip()
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main").strip()
GITHUB_FILE = os.getenv("GITHUB_FILE", "bot.py").strip()

# ============================================================
# LOGGING / HTTP
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("AUTO_FORWARDER")

HTTP_SESSION = requests.Session()
HTTP_SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
})

CONFIG: Dict[str, Any] = {}
PROCESSED_MESSAGES = set()
PROCESSING_MESSAGES = set()
STICKER_TASKS = set()

# Choix temporaires en mémoire pour les nouvelles versions d'un épisode.
# Aucun système de file d'attente n'est utilisé.
PENDING_VERSION_CHOICES: Dict[Tuple[int, int, int], Dict[str, Any]] = {}
PENDING_EPISODE_FIXES: Dict[int, Dict[str, Any]] = {}
PROCESSED_MESSAGES_FILE = "processed_messages.json"
ANIME_FICHES_FILE = "anime_fiches_publiees.json"
ANIME_FICHES_PUBLISHED = set()
ANILIST_DETAILS_CACHE = {}
ANIME_POSTER_CACHE = {}

CONFIG_LOCK = asyncio.Lock()
UPDATE_LOCK = asyncio.Lock()

def load_processed_messages():
    global PROCESSED_MESSAGES
    if not os.path.exists(PROCESSED_MESSAGES_FILE):
        PROCESSED_MESSAGES = set()
        return
    try:
        with open(PROCESSED_MESSAGES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        result = set()
        if isinstance(data, list):
            for item in data:
                if isinstance(item, list) and len(item) == 2:
                    try:
                        result.add((int(item[0]), int(item[1])))
                    except Exception:
                        pass
                elif isinstance(item, str) and ":" in item:
                    try:
                        chat_id, message_id = item.split(":", 1)
                        result.add((int(chat_id), int(message_id)))
                    except Exception:
                        pass
        PROCESSED_MESSAGES = result
        logger.info("📚 Messages source déjà traités : %d", len(PROCESSED_MESSAGES))
    except Exception as e:
        logger.warning("Erreur lecture %s : %s", PROCESSED_MESSAGES_FILE, e)
        PROCESSED_MESSAGES = set()


def save_processed_messages():
    directory = os.path.dirname(os.path.abspath(PROCESSED_MESSAGES_FILE))
    temp_path = None
    try:
        fd, temp_path = tempfile.mkstemp(prefix=".processed_", suffix=".tmp", dir=directory)
        data = [[int(chat_id), int(message_id)] for chat_id, message_id in sorted(PROCESSED_MESSAGES)]
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, PROCESSED_MESSAGES_FILE)
    except Exception as e:
        logger.warning("Erreur sauvegarde %s : %s", PROCESSED_MESSAGES_FILE, e)
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def mark_message_processed(message_key: Tuple[int, int]):
    if message_key not in PROCESSED_MESSAGES:
        PROCESSED_MESSAGES.add(message_key)
        save_processed_messages()


# ============================================================
# LIENS DES ÉPISODES PRÉCÉDENTS
# ============================================================

EPISODES_FILE = "episode_links.json"
EPISODE_LINKS: Dict[str, Dict[str, Any]] = {}
EPISODE_LOCK = asyncio.Lock()
EPISODE_ROUTING_LOCK = asyncio.Lock()
PREVIOUS_EPISODE_MARKER = "ÉPISODE PRÉCÉDENT"


def load_episode_links():
    global EPISODE_LINKS
    if not os.path.exists(EPISODES_FILE):
        EPISODE_LINKS = {}
        return
    try:
        with open(EPISODES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        EPISODE_LINKS = data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning("Erreur lecture %s : %s", EPISODES_FILE, e)
        EPISODE_LINKS = {}


def save_episode_links():
    directory = os.path.dirname(os.path.abspath(EPISODES_FILE))
    temp_path = None
    try:
        fd, temp_path = tempfile.mkstemp(prefix=".episodes_", suffix=".tmp", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(EPISODE_LINKS, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, EPISODES_FILE)
    except Exception as e:
        logger.warning("Erreur sauvegarde %s : %s", EPISODES_FILE, e)
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def episode_key(anime_name: str, season: int, episode: int) -> str:
    return f"{normalize_text(anime_name)}|S{int(season):02d}|E{int(episode):04d}"


async def get_message_link(bot, chat_id: int, message_id: int) -> str:
    """Construit un lien Telegram public ou privé vers un message."""
    try:
        chat = await bot.get_chat(chat_id)
        username = getattr(chat, "username", None)
        if username:
            return f"https://t.me/{username}/{message_id}"
    except Exception as e:
        logger.warning("Impossible de récupérer le username du chat %s : %s", chat_id, e)

    # Les chaînes/supergroupes privés ont généralement un ID -100xxxxxxxxxx.
    # Leur lien de message interne est https://t.me/c/xxxxxxxxxx/message_id.
    chat_str = str(chat_id)
    if chat_str.startswith("-100"):
        internal_id = chat_str[4:]
        return f"https://t.me/c/{internal_id}/{message_id}"

    return ""


def get_previous_episode_record(anime_name: str, season: int, episode: int):
    if episode <= 1:
        return None
    record = EPISODE_LINKS.get(episode_key(anime_name, season, episode - 1))
    if isinstance(record, list):
        # Le dernier lien publié est utilisé comme épisode précédent.
        for item in reversed(record):
            if isinstance(item, dict) and item.get("link"):
                return item
        return None
    return record if isinstance(record, dict) else None


def build_caption(
    anime_name: str,
    season: int,
    episode: int,
    language: str,
    quality: Optional[str],
    previous_link: Optional[str],
) -> str:
    lines = [
        f"🎬 {anime_name}",
        f"📀 Saison {season:02d} — Épisode {episode:02d}",
    ]
    if language:
        lines.append(f"🌐 {language}")
    if quality:
        lines.append(f"🎞️ {quality}")
    if episode > 1 and previous_link:
        lines.append(f'🔗 <a href="{previous_link}">{PREVIOUS_EPISODE_MARKER}</a>')
    return "\n".join(lines)

TECHNICAL_WORDS = {
    "1080p", "720p", "2160p", "480p", "360p", "4k", "fhd", "hd",
    "web", "webrip", "webdl", "web-dl", "bluray", "bdrip", "hdtv",
    "x264", "x265", "hevc", "av1", "aac", "flac", "multi", "vostfr",
    "vost", "vf", "vo", "truefrench", "french", "japanese", "english",
    "episode", "episodes", "ep", "saison", "season", "batch",
}

# ============================================================
# NORMALISATION
# ============================================================

def normalize_text(text: str) -> str:
    """Normalise un nom de fichier/titre, y compris les noms URL-encodes."""
    if not text:
        return ""

    from urllib.parse import unquote

    text = str(text)

    # Certains noms de fichiers arrivent encodes plusieurs fois (%20, etc.).
    for _ in range(3):
        decoded = unquote(text)
        if decoded == text:
            break
        text = decoded

    # Nettoyage des encodages percent incomplets/corrompus, par ex. "%2.mp4".
    text = re.sub(r"%(?![0-9A-Fa-f]{2})", " ", text)

    try:
        from html import unescape
        text = unescape(text)
    except Exception:
        pass

    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()

    # Retire les release groups entre crochets : [Erai-raws], [SubsPlease], etc.
    text = re.sub(r"\[[^\]]*\]", " ", text)

    text = text.replace("&", " and ")
    text = re.sub(r"[_\-–—]+", " ", text)
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def clean_anime_words(text: str) -> str:
    words = []
    for word in normalize_text(text).split():
        if word in TECHNICAL_WORDS:
            continue
        if re.fullmatch(r"\d{3,4}p", word) or re.fullmatch(r"x26[45]", word):
            continue
        words.append(word)
    return " ".join(words)

# ============================================================
# CONFIG
# ============================================================

def default_config() -> Dict[str, Any]:
    return {
        "sources": DEFAULT_SOURCES.copy(),
        "destination": DEFAULT_DESTINATION,
        "sticker_delay": DEFAULT_STICKER_DELAY,
        "animes": [],
        "admins": INITIAL_ADMIN_IDS.copy(),
        "users": [],
        "banned_users": [],
        "settings": {
            "nautiljon_enabled": True,
            "anilist_enabled": True,
            "auto_alias": True,
            "weekly_update": True,
        },
    }


def normalize_anime_entry(entry: Any) -> Optional[Dict[str, Any]]:
    if isinstance(entry, dict):
        name = entry.get("name") or entry.get("title")
        if not name:
            return None
        aliases = entry.get("aliases", [])
        if isinstance(aliases, str):
            aliases = [aliases]
        if not isinstance(aliases, list):
            aliases = []
        clean_aliases = []
        for alias in aliases:
            if not isinstance(alias, str):
                continue
            alias = alias.strip()
            if not alias or normalize_text(alias) == normalize_text(str(name)):
                continue
            if not any(normalize_text(alias) == normalize_text(x) for x in clean_aliases):
                clean_aliases.append(alias)
        entry["name"] = str(name).strip()
        entry["aliases"] = clean_aliases
        return entry
    if isinstance(entry, str) and entry.strip():
        return {"name": entry.strip(), "aliases": []}
    return None


def normalize_config(config: Any) -> Dict[str, Any]:
    if not isinstance(config, dict):
        config = default_config()

    sources = config.get("sources", DEFAULT_SOURCES)
    if not isinstance(sources, list):
        sources = DEFAULT_SOURCES.copy()
    clean_sources = []
    for source in sources:
        try:
            value = int(source)
            if value not in clean_sources:
                clean_sources.append(value)
        except Exception:
            pass
    config["sources"] = clean_sources

    try:
        config["destination"] = int(config.get("destination", DEFAULT_DESTINATION))
    except Exception:
        config["destination"] = DEFAULT_DESTINATION

    try:
        config["sticker_delay"] = max(0, int(config.get("sticker_delay", DEFAULT_STICKER_DELAY)))
    except Exception:
        config["sticker_delay"] = DEFAULT_STICKER_DELAY

    raw_animes = config.get("animes", [])
    if not isinstance(raw_animes, list):
        raw_animes = []
    config["animes"] = [a for x in raw_animes if (a := normalize_anime_entry(x))]

    admins = config.get("admins", INITIAL_ADMIN_IDS.copy())
    if not isinstance(admins, list):
        admins = INITIAL_ADMIN_IDS.copy()
    clean_admins = []
    for user_id in admins + INITIAL_ADMIN_IDS:
        try:
            value = int(user_id)
            if value not in clean_admins:
                clean_admins.append(value)
        except Exception:
            pass
    config["admins"] = clean_admins

    for key in ("users", "banned_users"):
        values = config.get(key, [])
        if not isinstance(values, list):
            values = []
        clean = []
        for user_id in values:
            try:
                value = int(user_id)
                if value not in clean:
                    clean.append(value)
            except Exception:
                pass
        config[key] = clean

    settings = config.get("settings", {})
    if not isinstance(settings, dict):
        settings = {}
    settings.setdefault("nautiljon_enabled", True)
    settings.setdefault("anilist_enabled", True)
    settings.setdefault("auto_alias", True)
    settings.setdefault("weekly_update", True)
    config["settings"] = settings
    return config


def load_config() -> Dict[str, Any]:
    if not os.path.exists(CONFIG_FILE):
        config = default_config()
        save_config(config)
        return config
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return normalize_config(json.load(f))
    except Exception as e:
        logger.exception("Erreur lecture configuration : %s", e)
        return default_config()


def save_config(config: Optional[Dict[str, Any]] = None):
    global CONFIG
    if config is None:
        config = CONFIG
    config = normalize_config(config)
    directory = os.path.dirname(os.path.abspath(CONFIG_FILE))
    temp_path = None
    try:
        fd, temp_path = tempfile.mkstemp(prefix=".config_", suffix=".tmp", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=4)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, CONFIG_FILE)
    except Exception as e:
        logger.exception("Erreur sauvegarde : %s", e)
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass

# ============================================================
# SECURITY
# ============================================================

def get_user_id(update: Update) -> Optional[int]:
    return update.effective_user.id if update.effective_user else None


def is_banned(user_id: Optional[int]) -> bool:
    return user_id is None or user_id in CONFIG.get("banned_users", [])


def is_admin(user_id: Optional[int]) -> bool:
    return user_id is not None and user_id in CONFIG.get("admins", [])


def is_allowed_user(user_id: Optional[int]) -> bool:
    return user_id is not None and not is_banned(user_id) and (is_admin(user_id) or user_id in CONFIG.get("users", []))


async def security_check(update: Update, admin_only: bool = False) -> bool:
    user_id = get_user_id(update)
    message = update.effective_message
    if is_banned(user_id):
        if message:
            await message.reply_text("🚫 Accès refusé.")
        return False
    if admin_only and not is_admin(user_id):
        if message:
            await message.reply_text("🔐 Cette commande est réservée aux administrateurs.")
        return False
    if not admin_only and not is_allowed_user(user_id):
        if message:
            await message.reply_text("🔐 Tu n'es pas autorisé à utiliser ce bot.")
        return False
    return True

# ============================================================
# ALIAS / MATCH
# ============================================================

def get_anime_aliases(anime: Any) -> List[str]:
    anime = normalize_anime_entry(anime)
    if not anime:
        return []
    result = [str(anime["name"])]
    for alias in anime.get("aliases", []):
        if isinstance(alias, str) and alias.strip() and not any(normalize_text(alias) == normalize_text(x) for x in result):
            result.append(alias.strip())
    return result


def anime_match_score(message_text: str, anime_name: str) -> float:
    msg = clean_anime_words(message_text)
    anime = clean_anime_words(anime_name)
    if not msg or not anime:
        return 0.0
    if anime in msg:
        return 1.0
    return SequenceMatcher(None, msg, anime).ratio()


def find_configured_anime(text: str) -> Optional[Dict[str, Any]]:
    """Trouve un anime configure meme si le fichier contient un titre raccourci."""
    if not text:
        return None

    normalized_message = clean_anime_words(text)
    if not normalized_message:
        return None

    input_words = normalized_message.split()
    best_anime = None
    best_score = 0.0

    for raw in CONFIG.get("animes", []):
        anime = normalize_anime_entry(raw)
        if not anime:
            continue

        for alias in get_anime_aliases(anime):
            normalized_alias = clean_anime_words(alias)
            if not normalized_alias:
                continue

            alias_words = normalized_alias.split()

            # Correspondance exacte.
            if normalized_alias == normalized_message:
                return anime

            # Le titre configure est contenu dans le texte du fichier.
            if len(normalized_alias) >= 5 and normalized_alias in normalized_message:
                score = 0.99
                if score > best_score:
                    best_score = score
                    best_anime = anime
                continue

            # Cas tres frequent : le fichier contient le debut du titre anime
            # mais pas le titre complet. Exemple :
            # "Gaikotsu Kishi-sama Tadaima Isekai"
            # pour "Gaikotsu Kishi-sama Tadaima Isekai e Odekakechuu".
            if len(input_words) >= 3 and len(alias_words) > len(input_words):
                matching = sum(
                    1 for a, b in zip(input_words, alias_words) if a == b
                )
                prefix_ratio = matching / len(input_words)
                if prefix_ratio >= 0.85 and matching >= 3:
                    score = 0.96
                    if score > best_score:
                        best_score = score
                        best_anime = anime
                    continue

            # Recouvrement de mots : utile avec ponctuation, sous-titres,
            # extensions ou variantes legerement differentes.
            input_set = set(input_words)
            alias_set = set(alias_words)
            common = input_set & alias_set
            if common:
                word_ratio = len(common) / min(len(input_set), len(alias_set))
            else:
                word_ratio = 0.0

            sequence_score = SequenceMatcher(
                None, normalized_alias, normalized_message
            ).ratio()

            score = max(sequence_score, word_ratio * 0.90)

            if score > best_score:
                best_score = score
                best_anime = anime

    if best_anime and best_score >= 0.72:
        logger.info(
            "Anime detecte : '%s' -> '%s' (score %.3f)",
            text,
            best_anime.get("name"),
            best_score,
        )
        return best_anime

    logger.info(
        "Aucun anime correspondant : '%s' (meilleur score %.3f)",
        text,
        best_score,
    )
    return None


def get_anime_by_name(name: str) -> Optional[Dict[str, Any]]:
    wanted = normalize_text(name)
    for raw in CONFIG.get("animes", []):
        anime = normalize_anime_entry(raw)
        if anime and normalize_text(anime.get("name", "")) == wanted:
            return anime
    return None


def add_aliases_to_anime(anime: Dict[str, Any], aliases: List[str]) -> List[str]:
    anime = normalize_anime_entry(anime)
    if not anime:
        return []
    current = anime.setdefault("aliases", [])
    added = []
    anime_name = normalize_text(anime.get("name", ""))
    for alias in aliases:
        if not isinstance(alias, str):
            continue
        alias = alias.strip()
        if not alias or normalize_text(alias) == anime_name:
            continue
        if not any(normalize_text(alias) == normalize_text(x) for x in current):
            current.append(alias)
            added.append(alias)
    return added


def remove_alias_from_anime(anime: Dict[str, Any], alias: str) -> bool:
    wanted = normalize_text(alias)
    old = anime.get("aliases", [])
    new = []
    removed = False
    for item in old if isinstance(old, list) else [old]:
        if normalize_text(str(item)) == wanted:
            removed = True
        else:
            new.append(item)
    anime["aliases"] = new
    return removed

# ============================================================
# PARSING MESSAGE
# ============================================================

def extract_episode(text: str) -> Optional[int]:
    patterns = [
        r"[Ss]\d{1,2}[Ee](\d{1,4})",
        r"[Ss]\d{1,2}\s*[-_. ]?\s*[Ee](\d{1,4})",
        r"\b(?:episode|episodes|ep)\s*[-_. ]?\s*(\d{1,4})\b",
        r"\b[Ee]\s*[-_. ]?\s*(\d{1,4})\b",
        r"#\s*(\d{1,4})\b",
    ]
    for pattern in patterns:
        m = re.search(pattern, text or "", re.IGNORECASE)
        if m:
            try:
                return int(m.group(1))
            except Exception:
                pass
    return None


def extract_season(text: str) -> Optional[int]:
    patterns = [
        r"\b[Ss](\d{1,2})\s*[-_. ]?\s*[Ee]\d{1,4}\b",
        r"\b[Ss]aison\s*(\d{1,2})\b",
        r"\b[Ss]eason\s*(\d{1,2})\b",
        r"\b[Ss](\d{1,2})\b",
    ]
    for pattern in patterns:
        m = re.search(pattern, text or "", re.IGNORECASE)
        if m:
            try:
                return int(m.group(1))
            except (ValueError, IndexError):
                pass
    return None

def detect_language(text: str) -> str:
    normalized = normalize_text(text)
    if re.search(r"\bvf\b", normalized) or "truefrench" in normalized or "french" in normalized:
        return "VF"
    if "vostfr" in normalized or "vost" in normalized:
        return "VOSTFR"
    return "VOSTFR"


def detect_quality(text: str) -> Optional[str]:
    m = re.search(r"\b(2160p|1080p|720p|480p|360p)\b", text or "", re.IGNORECASE)
    if m:
        return m.group(1).lower()
    if re.search(r"\b4k\b", text or "", re.IGNORECASE):
        return "2160p"
    return None


def get_message_text(message) -> str:
    """
    Retourne uniquement le nom du fichier média.
    La légende et le texte du message source sont ignorés.
    """
    for field in ("video", "document", "animation", "audio"):
        media = getattr(message, field, None)
        filename = getattr(media, "file_name", None) if media else None
        if filename and filename.strip():
            logger.info("📄 NOM DU FICHIER SOURCE | %s", filename)
            return filename.strip()

    logger.warning(
        "⚠️ MESSAGE IGNORÉ : aucun nom de fichier exploitable | "
        "chat_id=%s | message_id=%s",
        getattr(message, "chat_id", None),
        getattr(message, "message_id", None),
    )
    return ""


def decode_html(text: str) -> str:
    try:
        from html import unescape
        return unescape(text)
    except Exception:
        return text


def search_nautiljon(anime_name: str) -> List[str]:
    logger.info("Recherche Nautiljon : %s", anime_name)
    encoded = requests.utils.quote(anime_name)
    urls = [
        f"https://www.nautiljon.com/animes/?q={encoded}",
        f"https://www.nautiljon.com/animes/?search={encoded}",
        f"https://www.nautiljon.com/animes/?s={encoded}",
    ]
    best = []
    for url in urls:
        try:
            response = HTTP_SESSION.get(url, timeout=15)
            if response.status_code == 403:
                logger.warning("Nautiljon HTTP 403")
                continue
            response.raise_for_status()
            html = decode_html(response.text)
            matches = re.findall(r'href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html, re.I | re.S)
            aliases = []
            for href, title in matches:
                clean = re.sub(r"<[^>]+>", " ", title)
                clean = re.sub(r"\s+", " ", clean).strip()
                if not clean or len(clean) > 200:
                    continue
                score = SequenceMatcher(None, normalize_text(anime_name), normalize_text(clean)).ratio()
                if score >= 0.55:
                    aliases.append(clean)
            for alias in aliases:
                if not any(normalize_text(alias) == normalize_text(x) for x in best):
                    best.append(alias)
            if best:
                return best[:30]
        except Exception as e:
            logger.warning("Nautiljon erreur : %s", e)
    return []


ANILIST_URL = "https://graphql.anilist.co"


def search_anilist(anime_name: str) -> List[str]:
    query = """
    query ($search: String) {
      Media(search: $search, type: ANIME) {
        title { romaji english native userPreferred }
        synonyms
      }
    }
    """
    try:
        response = HTTP_SESSION.post(
            ANILIST_URL,
            json={"query": query, "variables": {"search": anime_name}},
            headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "AnimeAutoForwarder/1.0"},
            timeout=15,
        )
        response.raise_for_status()
        media = response.json().get("data", {}).get("Media")
        if not media:
            return []
        result = []
        title = media.get("title") or {}
        for key in ("romaji", "english", "native", "userPreferred"):
            value = title.get(key)
            if value and value not in result:
                result.append(value)
        for synonym in media.get("synonyms") or []:
            if synonym and synonym not in result:
                result.append(synonym)
        return result[:30]
    except Exception as e:
        logger.warning("AniList erreur : %s", e)
        return []


def find_aliases(anime_name: str) -> Tuple[List[str], str]:
    settings = CONFIG.get("settings", {})
    combined = []
    sources = []

    if settings.get("nautiljon_enabled", True):
        aliases = search_nautiljon(anime_name)
        if aliases:
            sources.append("Nautiljon")
            for alias in aliases:
                if not any(normalize_text(alias) == normalize_text(x) for x in combined):
                    combined.append(alias)

    # AniList complète Nautiljon et sert de fallback si Nautiljon ne donne rien.
    if settings.get("anilist_enabled", True):
        aliases = search_anilist(anime_name)
        if aliases:
            sources.append("AniList")
            for alias in aliases:
                if not any(normalize_text(alias) == normalize_text(x) for x in combined):
                    combined.append(alias)

    return combined[:50], " + ".join(sources) if sources else "Aucune source"

# ============================================================
# FICHES ANIME (AniList)
# ============================================================

ANILIST_DETAILS_QUERY = """
query ($search: String) {
  Media(search: $search, type: ANIME) {
    id
    siteUrl
    title { romaji english native userPreferred }
    synonyms
    format
    countryOfOrigin
    episodes
    duration
    source
    startDate { year month day }
    endDate { year month day }
    season
    seasonYear
    genres
    tags { name }
    studios { edges { isMain node { name } } }
    coverImage { extraLarge large }
    description(asHtml: false)
    externalLinks { site url }
  }
}
"""


def anime_fiche_key(anime_name: str) -> str:
    return normalize_text(anime_name)


def load_anime_fiches():
    global ANIME_FICHES_PUBLISHED
    try:
        with open(ANIME_FICHES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        ANIME_FICHES_PUBLISHED = set(data if isinstance(data, list) else [])
    except FileNotFoundError:
        ANIME_FICHES_PUBLISHED = set()
    except Exception as e:
        logger.warning("Erreur lecture %s : %s", ANIME_FICHES_FILE, e)
        ANIME_FICHES_PUBLISHED = set()


def save_anime_fiches():
    temp_path = None
    try:
        directory = os.path.dirname(os.path.abspath(ANIME_FICHES_FILE))
        fd, temp_path = tempfile.mkstemp(prefix=".anime_fiches_", suffix=".tmp", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(sorted(ANIME_FICHES_PUBLISHED), f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, ANIME_FICHES_FILE)
    except Exception as e:
        logger.warning("Erreur sauvegarde %s : %s", ANIME_FICHES_FILE, e)
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def fetch_anilist_details(anime_name: str) -> Optional[Dict[str, Any]]:
    cache_key = anime_name.strip().casefold()
    cached = ANILIST_DETAILS_CACHE.get(cache_key)
    if cached is not None:
        logger.info("Cache AniList utilisé : %s", anime_name)
        return cached

    try:
        response = HTTP_SESSION.post(
            ANILIST_URL,
            json={"query": ANILIST_DETAILS_QUERY, "variables": {"search": anime_name}},
            headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "AnimeAutoForwarder/1.0"},
            timeout=20,
        )
        response.raise_for_status()
        media = response.json().get("data", {}).get("Media")
        if isinstance(media, dict):
            ANILIST_DETAILS_CACHE[cache_key] = media
            if len(ANILIST_DETAILS_CACHE) > 100:
                ANILIST_DETAILS_CACHE.pop(next(iter(ANILIST_DETAILS_CACHE)))
            return media
        return None
    except Exception as e:
        logger.warning("Récupération fiche AniList impossible pour %s : %s", anime_name, e)
        return None



def format_anilist_date(date_data: Any) -> str:
    if not isinstance(date_data, dict) or not date_data.get("year"):
        return "Non renseignée"
    year = date_data.get("year")
    month = date_data.get("month")
    day = date_data.get("day")
    if month and day:
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    if month:
        return f"{int(year):04d}-{int(month):02d}"
    return str(year)




_TRADUCTION_FR_CACHE = {}

def traduire_texte_fr(texte: str) -> str:
    """Traduit rapidement le synopsis ; conserve le texte original en cas d'échec."""
    texte = str(texte or "").strip()
    if not texte:
        return texte

    cache = _TRADUCTION_FR_CACHE
    if texte in cache:
        return cache[texte]

    # Évite de bloquer longtemps la publication sur plusieurs requêtes lentes.
    try:
        response = requests.get(
            "https://api.mymemory.translated.net/get",
            params={"q": texte[:450], "langpair": "en|fr"},
            timeout=3,
            headers={"User-Agent": "AnimeAutoForwarder/1.0"},
        )
        response.raise_for_status()
        data = response.json()
        traduit = (data.get("responseData") or {}).get("translatedText", "")
        if data.get("responseStatus") == 200 and traduit:
            traduit = str(traduit).strip()
            if len(texte) <= 450 and traduit.casefold() != texte.casefold():
                cache[texte] = traduit
                return traduit
    except Exception as e:
        logger.warning("Traduction rapide MyMemory indisponible : %s", e)

    # Ne lance pas un second service lent : le synopsis original reste disponible.
    logger.warning("Synopsis conservé dans sa langue originale pour accélérer la fiche.")
    return texte

def build_anime_fiche_text(anime_name: str, media: Dict[str, Any]) -> str:
    from html import unescape
    title = media.get("title") or {}
    native = title.get("native") or "Non renseigné"
    english = title.get("english") or title.get("romaji") or anime_name
    romaji = title.get("romaji") or "Non renseigné"
    alternatives = []
    for value in [title.get("english"), title.get("romaji"), *(media.get("synonyms") or [])]:
        if value and value not in alternatives and value != native:
            alternatives.append(value)
    fmt = media.get("format") or "Non renseigné"
    country_code = media.get("countryOfOrigin") or ""
    country_names = {"JP": "Japon", "CN": "Chine", "KR": "Corée du Sud", "TW": "Taïwan", "US": "États-Unis", "GB": "Royaume-Uni"}
    country = country_names.get(country_code, country_code or "Non renseigné")
    episodes = media.get("episodes")
    duration = media.get("duration")
    count = f"{episodes} épisodes" if episodes else "nombre d'épisodes non renseigné"
    if duration:
        count += f" × {duration} min"
    source = (media.get("source") or "Non renseignée").replace("_", " ")
    start = format_anilist_date(media.get("startDate"))
    end = format_anilist_date(media.get("endDate"))
    season = media.get("season")
    season_year = media.get("seasonYear")
    season_names = {"WINTER": "hiver", "SPRING": "printemps", "SUMMER": "été", "FALL": "automne"}
    season_text = f"{season_names.get(str(season).upper(), str(season).lower())} {season_year}" if season and season_year else "Non renseignée"
    genres = " - ".join(media.get("genres") or []) or "Non renseignés"
    tags = []
    for tag in media.get("tags") or []:
        name = tag.get("name") if isinstance(tag, dict) else None
        if name and name not in tags:
            tags.append(name)
    themes = " - ".join(tags[:12]) or "Non renseignés"
    studios = []
    for edge in (media.get("studios") or {}).get("edges", []):
        node = edge.get("node") if isinstance(edge, dict) else None
        name = node.get("name") if isinstance(node, dict) else None
        if name and name not in studios:
            studios.append(name)
    studio_text = " - ".join(studios) or "Non renseigné"
    site_url = media.get("siteUrl") or "https://anilist.co/anime/"
    links = media.get("externalLinks") or []
    known_streamers = {"Crunchyroll", "ADN", "Animation Digital Network", "Netflix", "Amazon", "Prime Video", "Disney Plus", "Hulu"}
    found_streamers = []
    for link in links:
        if isinstance(link, dict) and link.get("site") in known_streamers:
            label = link.get("site")
            if label not in found_streamers:
                found_streamers.append(label)
    streaming = " - ".join(found_streamers) if found_streamers else "Non confirmé"
    description = media.get("description") or "Synopsis non renseigné sur AniList."
    description = unescape(re.sub(r"<[^>]+>", "", str(description))).strip()
    description = re.sub(r"\n{3,}", "\n\n", description)
    description = traduire_texte_fr(description)
    return (
        f"Titre : {english}\n"
        f"Titre original : {native}\n"
        f"Titres alternatifs : {' / '.join(alternatives) if alternatives else romaji}\n\n"
        f"{fmt} · {country} · {count}\n\n"
        f"Origine : {source}\n"
        f"Diffusion : {start} au {end}\n"
        f"Saison : {season_text}\n"
        f"Début de diffusion en simulcast/streaming : Non confirmé\n"
        f"Date de sortie en VF : Non renseignée\n"
        f"Genres : {genres}\n"
        f"Thèmes : {themes}\n"
        f"Studio d'animation / production : {studio_text}\n"
        f"Simulcast / streaming : {streaming}\n"
        f"Fiche source : {site_url}\n\n"
        f"Synopsis\n{description}\n\n"
        f"Source des données : AniList"
    )


async def send_anime_poster(context: ContextTypes.DEFAULT_TYPE, chat_id: int, media: Dict[str, Any], title: str) -> bool:
    cover = media.get("coverImage") or {}
    image_url = cover.get("extraLarge") or cover.get("large")
    if not image_url:
        return False

    try:
        image_bytes = ANIME_POSTER_CACHE.get(image_url)
        if image_bytes is None:
            response = await asyncio.to_thread(HTTP_SESSION.get, image_url, timeout=20)
            response.raise_for_status()
            image_bytes = response.content
            ANIME_POSTER_CACHE[image_url] = image_bytes
            if len(ANIME_POSTER_CACHE) > 12:
                ANIME_POSTER_CACHE.pop(next(iter(ANIME_POSTER_CACHE)))

        photo = io.BytesIO(image_bytes)
        photo.name = "anime_poster.jpg"
        await context.bot.send_photo(chat_id=chat_id, photo=photo, caption=title[:900])
        return True
    except Exception as e:
        logger.exception(
            "Envoi affiche AniList impossible | destination=%s | url=%s | type_erreur=%s | details=%s",
            chat_id, image_url, type(e).__name__, str(e)
        )
        return False



async def publish_anime_fiche(context: ContextTypes.DEFAULT_TYPE, anime: Dict[str, Any], force: bool = False) -> bool:
    name = str(anime.get("name") or "Anime")
    key = anime_fiche_key(name)
    if not force and key in ANIME_FICHES_PUBLISHED:
        return True
    media = await asyncio.to_thread(fetch_anilist_details, name)
    if not media:
        logger.warning("Fiche non publiée : AniList n'a pas retourné de résultat pour %s", name)
        return False
    title_data = media.get("title") or {}
    display_title = title_data.get("english") or title_data.get("romaji") or name
    poster_sent = await send_anime_poster(
        context, int(CONFIG["destination"]), media, display_title
    )
    if not poster_sent:
        logger.warning("Fiche annulée : affiche non publiée pour %s", name)
        return False

    fiche = build_anime_fiche_text(name, media)
    # Telegram limite les messages texte à environ 4096 caractères.
    chunks = []
    while len(fiche) > 3900:
        cut = fiche.rfind("\n", 0, 3900)
        if cut < 1000:
            cut = 3900
        chunks.append(fiche[:cut])
        fiche = fiche[cut:].lstrip()
    if fiche:
        chunks.append(fiche)
    for chunk in chunks:
        await context.bot.send_message(chat_id=int(CONFIG["destination"]), text=chunk, disable_web_page_preview=True)
    ANIME_FICHES_PUBLISHED.add(key)
    save_anime_fiches()
    logger.info("Fiche anime publiée : %s", name)
    return True



async def send_duplicate_episode_prompt(
    context: ContextTypes.DEFAULT_TYPE,
    admin_id: int,
    pending: Dict[str, Any],
):
    anime = pending.get("anime_entry") or {}
    name = anime.get("name", "Anime")
    season = int(pending.get("season") or 1)
    episode = int(pending.get("episode") or 0)
    version = pending.get("version") or pending.get("language") or "non précisée"
    quality = pending.get("quality") or "non précisée"
    chat_id = int(pending["source_chat_id"])
    message_id = int(pending["source_message_id"])

    key = (int(admin_id), chat_id, message_id)
    PENDING_VERSION_CHOICES[key] = dict(pending)

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "✅ Oui, avec affiche",
            callback_data=f"poster:yes:{chat_id}:{message_id}",
        ),
        InlineKeyboardButton(
            "🚫 Non, sans affiche",
            callback_data=f"poster:no:{chat_id}:{message_id}",
        ),
    ]])

    await context.bot.send_message(
        chat_id=admin_id,
        text=(
            "📺 NOUVELLE VERSION DU MÊME ÉPISODE\n\n"
            f"🎬 {name} — S{season:02d}E{episode:02d}\n"
            f"🌐 Version : {version}\n"
            f"🎞️ Qualité : {quality}\n\n"
            "Faut-il republier la fiche et l’affiche ?"
        ),
        reply_markup=keyboard,
    )
    logger.info(
        "Choix individuel envoyé : admin=%s source=%s/%s",
        admin_id, chat_id, message_id,
    )


def episode_has_been_published(
    anime_name: str,
    season: int,
    episode: int,
    source_chat_id: int = None,
    source_message_id: int = None,
) -> bool:
    """Vérifie uniquement si anime + saison + épisode sont enregistrés."""
    return episode_key(anime_name, season, episode) in EPISODE_LINKS


# ============================================================
# STICKER / FORWARD
# ============================================================

async def send_sticker_after_delay(context: ContextTypes.DEFAULT_TYPE, destination: int, message_id: int, anime: Dict[str, Any]):
    try:
        await asyncio.sleep(int(CONFIG.get("sticker_delay", DEFAULT_STICKER_DELAY)))
        sticker_id = anime.get("sticker")
        if sticker_id:
            await context.bot.send_sticker(chat_id=destination, sticker=sticker_id, reply_to_message_id=message_id)
            logger.info("Sticker envoyé : %s", anime.get("name"))
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.warning("Sticker erreur : %s", e)


def get_interactive_admin_id(update: Update) -> Optional[int]:
    """Retourne l'admin qui doit répondre aux demandes interactives."""
    user = update.effective_user
    if user and is_admin(user.id):
        return user.id

    admins = CONFIG.get("admins", [])
    for admin_id in admins:
        try:
            return int(admin_id)
        except Exception:
            continue
    return None


async def resend_source_media(context, destination, message, caption):
    """Republie un média à partir du file_id du message reçu."""
    common = {
        "chat_id": destination,
        "caption": caption,
        "parse_mode": "HTML",
    }

    if message.video:
        return await context.bot.send_video(
            video=message.video.file_id, **common
        )
    if message.document:
        return await context.bot.send_document(
            document=message.document.file_id, **common
        )
    if message.animation:
        return await context.bot.send_animation(
            animation=message.animation.file_id, **common
        )
    if message.audio:
        return await context.bot.send_audio(
            audio=message.audio.file_id, **common
        )
    if message.photo:
        return await context.bot.send_photo(
            photo=message.photo[-1].file_id, **common
        )
    if message.voice:
        return await context.bot.send_voice(
            voice=message.voice.file_id, **common
        )
    if message.text:
        return await context.bot.send_message(
            chat_id=destination,
            text=caption,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    raise RuntimeError("Aucun média compatible dans le message reçu.")


async def forward_anime_message(
    context: ContextTypes.DEFAULT_TYPE,
    pending: Dict[str, Any],
) -> bool:
    """Copie directement le message source vers la destination et enregistre son lien."""
    anime = pending.get("anime_entry") or {}
    season = pending.get("season")
    episode = pending.get("episode")
    language = pending.get("version") or pending.get("language") or "VOSTFR"
    quality = pending.get("quality")

    source_chat_id = pending.get("source_chat_id")
    source_message_id = pending.get("source_message_id")

    if source_chat_id is None or source_message_id is None:
        update = pending.get("update")
        message = (update.channel_post or update.message) if update else None
        if message:
            source_chat_id = message.chat_id
            source_message_id = message.message_id

    if source_chat_id is None or source_message_id is None:
        logger.warning("Transfert impossible : message source introuvable.")
        return False

    if season is None or episode is None:
        logger.warning("Transfert impossible : saison ou épisode manquant.")
        return False

    destination = CONFIG.get("destination")
    if not destination:
        logger.warning("Transfert impossible : destination non configurée.")
        return False

    message_key = (int(source_chat_id), int(source_message_id))

    try:
        # Pour la première vidéo d'un anime, publie automatiquement sa fiche.
        # Sur une version en doublon, le choix explicite de l'admin prévaut.
        if "include_fiche" in pending:
            if pending.get("include_fiche"):
                fiche_ok = await publish_anime_fiche(context, anime, force=bool(pending.get("fiche_authorized", False)))
                if not fiche_ok:
                    logger.warning(
                        "Vidéo non publiée : échec de la fiche demandée (%s S%02dE%02d)",
                        anime.get("name", ""), int(season), int(episode)
                    )
                    return False
        else:
            # Première publication : la fiche doit réussir avant la vidéo.
            fiche_ok = await publish_anime_fiche(context, anime, force=False)
            if not fiche_ok:
                logger.warning(
                    "Vidéo non publiée : fiche indisponible (%s S%02dE%02d)",
                    anime.get("name", ""), int(season), int(episode)
                )
                return False

        async with EPISODE_LOCK:
            previous_record = get_previous_episode_record(
                anime.get("name", ""), int(season), int(episode)
            )
            previous_link = (previous_record or {}).get("link")

            caption = build_caption(
                anime.get("name", ""),
                int(season),
                int(episode),
                language,
                quality,
                previous_link,
            )

            logger.info(
                "DIAGNOSTIC COPIE | source_chat_id=%s | source_message_id=%s | destination=%s",
                source_chat_id, source_message_id, destination
            )
            try:
                copied = await context.bot.copy_message(
                    chat_id=destination,
                    from_chat_id=int(source_chat_id),
                    message_id=int(source_message_id),
                    caption=caption,
                    parse_mode="HTML",
                )
            except Exception as copy_error:
                error_text = str(copy_error)
                source_message = pending.get("source_message")

                if (
                    "Message to copy not found" in error_text
                    or "Message to forward not found" in error_text
                ):
                    if source_message is None:
                        logger.error(
                            "Copie impossible : le message source n'est "
                            "pas disponible en mémoire | chat=%s message=%s",
                            source_chat_id, source_message_id,
                        )
                        raise

                    logger.warning(
                        "Tentative de secours par file_id | chat=%s message=%s",
                        source_chat_id, source_message_id,
                    )
                    try:
                        copied = await resend_source_media(
                            context, destination, source_message, caption
                        )
                    except Exception:
                        logger.exception(
                            "Échec du secours média | chat=%s message=%s",
                            source_chat_id, source_message_id,
                        )
                        raise copy_error
                else:
                    logger.exception(
                        "ÉCHEC COPY_MESSAGE | source_chat_id=%s | "
                        "source_message_id=%s | destination=%s | erreur=%s",
                        source_chat_id, source_message_id,
                        destination, copy_error,
                    )
                    raise

            current_link = await get_message_link(
                context.bot, destination, copied.message_id
            )

            record = {
                "anime": anime.get("name", ""),
                "season": int(season),
                "episode": int(episode),
                "destination": int(destination),
                "message_id": int(copied.message_id),
                "link": current_link,
                "language": language or "",
                "quality": quality or "",
                "source_chat_id": int(source_chat_id),
                "source_message_id": int(source_message_id),
            }

            # IMPORTANT : plusieurs publications du même épisode sont autorisées.
            # Exemple : HD puis version convertie. Le doublon est uniquement le
            # même message source Telegram (chat_id + message_id).
            key = episode_key(anime.get("name", ""), int(season), int(episode))
            # Remplacer l'ancienne version : conserver uniquement la dernière.
            existing_records = EPISODE_LINKS.get(key, [])
            if isinstance(existing_records, dict):
                existing_records = [existing_records]
            elif not isinstance(existing_records, list):
                existing_records = []

            source_identity = (
                int(source_chat_id), int(source_message_id)
            )
            updated_records = []
            replaced_existing = False

            for old_record in existing_records:
                if not isinstance(old_record, dict):
                    continue

                old_identity = (
                    old_record.get("source_chat_id"),
                    old_record.get("source_message_id"),
                )

                if old_identity == source_identity:
                    if not replaced_existing:
                        updated_records.append(record)
                        replaced_existing = True
                else:
                    updated_records.append(old_record)

            if not replaced_existing:
                updated_records.append(record)

            EPISODE_LINKS[key] = updated_records
            save_episode_links()

        mark_message_processed(message_key)

        task = asyncio.create_task(
            send_sticker_after_delay(context, destination, copied.message_id, anime)
        )
        STICKER_TASKS.add(task)
        task.add_done_callback(STICKER_TASKS.discard)

        if previous_link:
            logger.info(
                "⏮️ Épisode précédent ajouté : %s | %s S%sE%s",
                previous_link, anime.get("name"), int(season), int(episode)
            )

        logger.info(
            "Copié : %s | S%sE%s | %s | %s",
            anime.get("name"), int(season), int(episode), language, quality or "?"
        )
        return True

    except Exception as e:
        logger.exception("Erreur transfert message %s : %s", message_key, e)
        return False



async def handle_poster_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if not query or not user or not is_admin(user.id):
        if query:
            await query.answer("Action réservée à l'administrateur.", show_alert=True)
        return

    data = (query.data or "").split(":")
    if len(data) != 4 or data[0] != "poster" or data[1] not in ("yes", "no"):
        await query.answer("Bouton invalide ou expiré.", show_alert=True)
        return
    try:
        chat_id, message_id = int(data[2]), int(data[3])
    except ValueError:
        await query.answer("Identifiants invalides.", show_alert=True)
        return

    key = (int(user.id), chat_id, message_id)
    pending = PENDING_VERSION_CHOICES.get(key)
    if not pending:
        await query.answer("Ce choix a expiré, le bot a peut-être été redémarré.", show_alert=True)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    await query.answer()
    current = dict(pending)
    choice = data[1]
    current["include_fiche"] = choice == "yes"
    current["fiche_authorized"] = choice == "yes"
    try:
        status = await context.bot.send_message(
            chat_id=user.id,
            text="⏳ Préparation de la fiche et du transfert…" if choice == "yes" else "⏳ Transfert sans affiche…",
        )
    except Exception:
        logger.exception("Impossible d'envoyer le statut de transfert.")
        return

    async with EPISODE_ROUTING_LOCK:
        success = await forward_anime_message(context, current)

    if success:
        PENDING_VERSION_CHOICES.pop(key, None)
        anime_name = (current.get("anime_entry") or {}).get("name", "Anime")
        await status.edit_text(
            "✅ Transfert terminé !\n\n"
            f"🎬 {anime_name}\n📀 Saison {int(current['season']):02d} — Épisode {int(current['episode']):02d}\n"
            f"🖼️ Fiche : {'republiée' if choice == 'yes' else 'non republiée'}"
        )
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
    else:
        await status.edit_text("❌ Transfert échoué. Tu peux réessayer avec les boutons ci-dessous.")
        try:
            await send_duplicate_episode_prompt(context, user.id, current)
        except Exception:
            logger.exception("Impossible de réafficher les boutons après l'échec.")

async def setepisode_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return

    # Seuls les administrateurs configurés peuvent corriger une vidéo.
    configured_admins = CONFIG.get("admins", [])
    if not isinstance(configured_admins, (list, tuple, set)):
        configured_admins = [configured_admins]

    try:
        allowed_admins = {int(x) for x in configured_admins if str(x).strip()}
    except (TypeError, ValueError):
        allowed_admins = set()

    if int(user.id) not in allowed_admins:
        await message.reply_text("⛔ Cette commande est réservée aux administrateurs.")
        return

    pending = PENDING_EPISODE_FIXES.get(int(user.id))
    if not pending:
        await message.reply_text(
            "Aucune vidéo n'attend de correction. Le bot doit d'abord te demander "
            "la saison ou l'épisode en privé."
        )
        return

    if len(context.args) != 2:
        await message.reply_text(
            "Format attendu : /setepisode SAISON EPISODE\\n"
            "Exemple : /setepisode 1 12"
        )
        return

    try:
        season = int(context.args[0])
        episode = int(context.args[1])
        if season < 1 or episode < 1:
            raise ValueError
    except ValueError:
        await message.reply_text(
            "❌ La saison et l'épisode doivent être des nombres entiers positifs."
        )
        return

    pending["season"] = season
    pending["episode"] = episode
    anime = pending.get("anime_entry") or {}
    name = anime.get("name", "Anime")

    async with EPISODE_ROUTING_LOCK:
        already_published = episode_has_been_published(name, season, episode)

        if already_published:
            mark_message_processed((
                int(pending["source_chat_id"]),
                int(pending["source_message_id"]),
            ))
            await send_duplicate_episode_prompt(
                context, int(user.id), pending
            )
            PENDING_EPISODE_FIXES.pop(int(user.id), None)
            await message.reply_text(
                f"✅ Correction enregistrée : {name} S{season:02d}E{episode:02d}.\\n"
                "Cet épisode existe déjà : choisis si tu veux republier la fiche "
                "avec affiche ou transférer sans affiche via les boutons."
            )
            return

        pending["include_fiche"] = True
        success = await forward_anime_message(context, pending)

        if success:
            PENDING_EPISODE_FIXES.pop(int(user.id), None)
            await message.reply_text(
                f"✅ Correction appliquée : {name} S{season:02d}E{episode:02d}.\\n"
                "La vidéo a été transférée."
            )
        else:
            await message.reply_text(
                f"⚠️ Le transfert de {name} S{season:02d}E{episode:02d} a échoué.\\n"
                "La vidéo reste en attente. Tu peux réessayer avec "
                f"/setepisode {season} {episode} après vérification."
            )


async def addepisode_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    if not message or not await security_check(update, admin_only=True):
        return

    parts = [part.strip() for part in " ".join(context.args).split("|")]
    if len(parts) != 4:
        await message.reply_text(
            "Utilisation :\n"
            "/addepisode Nom anime | saison | épisode | lien Telegram\n\n"
            "Exemple :\n"
            "/addepisode Dark Machine | 1 | 2 | https://t.me/c/123456/789"
        )
        return

    anime_name, season_text, episode_text, link = parts

    try:
        season = int(season_text)
        episode = int(episode_text)
        if season < 1 or episode < 1:
            raise ValueError
    except ValueError:
        await message.reply_text(
            "La saison et l'épisode doivent être des nombres positifs."
        )
        return

    if not re.match(r"^https?://t\.me/", link):
        await message.reply_text(
            "Lien invalide : utilise un lien Telegram commençant par https://t.me/."
        )
        return

    anime = find_configured_anime(anime_name)
    if not anime:
        await message.reply_text(
            "Anime non reconnu. Vérifie son nom dans /animes."
        )
        return

    canonical_name = anime.get("name", anime_name)
    key = episode_key(canonical_name, season, episode)
    record = {
        "anime": canonical_name,
        "season": season,
        "episode": episode,
        "destination": int(CONFIG["destination"]),
        "message_id": None,
        "link": link,
        "language": "",
        "quality": "",
        "source_chat_id": -1,
        "source_message_id": -1,
        "manual": True,
    }

    async with EPISODE_LOCK:
        existing = EPISODE_LINKS.get(key)
        if isinstance(existing, list):
            records = existing
        elif isinstance(existing, dict):
            records = [existing]
        else:
            records = []

        if any(
            isinstance(item, dict)
            and item.get("manual")
            and item.get("link") == link
            for item in records
        ):
            already_exists = True
        else:
            already_exists = False
            records.append(record)
            EPISODE_LINKS[key] = records
            save_episode_links()

    if already_exists:
        await message.reply_text("Ce lien est déjà enregistré.")
        return

    await message.reply_text(
        "✅ Lien enregistré.\n"
        f"🎬 {canonical_name}\n"
        f"📀 Saison {season:02d} — Épisode {episode:02d}\n"
        f"🔗 {link}\n\n"
        "Il pourra servir de lien pour l'épisode précédent."
    )

async def process_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.channel_post or update.message
    if not message:
        return
    chat_id = message.chat_id
    if chat_id not in CONFIG.get("sources", []):
        return

    message_key = (chat_id, message.message_id)
    if message_key in PROCESSED_MESSAGES or message_key in PROCESSING_MESSAGES:
        return
    PROCESSING_MESSAGES.add(message_key)
    try:
        text = get_message_text(message)
        if not text:
            logger.warning("Message ignoré : aucun texte ni légende | %s/%s", chat_id, message.message_id)
            return
        logger.info("🔎 TEXTE SOURCE | %s", text[:1000])
        anime = find_configured_anime(text)
        if not anime:
            logger.warning("Anime non reconnu | %s/%s | %s", chat_id, message.message_id, text[:500])
            return

        episode = extract_episode(text)
        season = extract_season(text)
        language = detect_language(text)
        quality = detect_quality(text)
        if not CONFIG.get("destination"):
            logger.warning("Destination non configurée.")
            return

        if season is None or episode is None:
            admin_id = get_interactive_admin_id(update)
            if admin_id is None:
                logger.warning(
                    "Saison/épisode manquant, aucun administrateur disponible | %s/%s",
                    chat_id, message.message_id
                )
                mark_message_processed(message_key)
                return

            PENDING_EPISODE_FIXES[int(admin_id)] = {
                "source_chat_id": int(message.chat_id),
                "source_message_id": int(message.message_id),
                "source_message": message,
                "update": update,
                "anime_entry": anime,
                "season": season,
                "episode": episode,
                "version": language,
                "quality": quality,
            }

            await context.bot.send_message(
                chat_id=int(admin_id),
                text=(
                    "⚠️ VIDÉO EN ATTENTE : saison ou épisode introuvable.\\n\\n"
                    f"🎬 Anime : {anime.get('name', 'Anime')}\\n"
                    f"🆔 Message source : {message.message_id}\\n"
                    f"📀 Saison détectée : {season if season is not None else 'inconnue'}\\n"
                    f"🎞️ Épisode détecté : {episode if episode is not None else 'inconnu'}\\n\\n"
                    "Réponds avec la saison et l'épisode :\\n"
                    "/setepisode 1 12\\n\\n"
                    "Exemple : saison 1, épisode 12.\\n"
                    "Une seule vidéo peut attendre cette correction à la fois."
                ),
            )
            logger.info(
                "Correction saison/épisode demandée : admin=%s source=%s/%s",
                admin_id, chat_id, message.message_id
            )
            return

        pending = {
            "source_chat_id": int(message.chat_id),
            "source_message_id": int(message.message_id),
                "source_message": message,
                "update": update,
            "anime_entry": anime,
            "season": int(season),
            "episode": int(episode),
            "version": language,
            "quality": quality,
        }

        async with EPISODE_ROUTING_LOCK:
            already_published = episode_has_been_published(anime.get("name", ""), int(season), int(episode))
            if not already_published:
                pending["include_fiche"] = True
                success = await forward_anime_message(context, pending)
                if not success:
                    logger.warning("Première publication échouée : %s S%02dE%02d", anime.get("name", "Anime"), int(season), int(episode))
                return

            admin_id = get_interactive_admin_id(update)
            if admin_id is None:
                logger.warning("Aucun administrateur disponible pour le choix affiche/sans affiche.")
                return
            mark_message_processed(message_key)
            await send_duplicate_episode_prompt(context, admin_id, pending)

    except Exception as e:
        logger.exception("Erreur traitement message : %s", e)
    finally:
        PROCESSING_MESSAGES.discard(message_key)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await security_check(update):
        await update.effective_message.reply_text("🤖 AUTO FORWARDER actif.\n\nUtilise /help pour voir les commandes.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await security_check(update):
        return
    await update.effective_message.reply_text(
        "🤖 AUTO FORWARDER\n\n"
        "🎬 ANIMES\n/addanime Nom\n/removeanime Nom\n/animes\n\n"
        "🔤 ALIAS\n/aliases\n/addalias Nom | Alias\n/removealias Nom | Alias\n/findalias Nom\n\n"
        "🎨 STICKERS\n/setsticker Nom\n/removesticker Nom\n\n"
        "📡 SOURCES\n/sources\n/addsource ID\n/removesource ID\n\n"
        "🎯 DESTINATION\n/destination\n/setdestination ID\n\n"
        "👥 UTILISATEURS\n/adduser ID\n/removeuser ID\n/users\n/banuser ID\n/unbanuser ID\n/banned\n\n"
        "⚙️ SYSTÈME\n/status\n/config\n/reload\n/update\n/help\n\n"
        "🌐 Alias automatique : Nautiljon + AniList\n"
        "⏱️ Sticker : 3 minutes après publication."
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await security_check(update):
        return
    settings = CONFIG.get("settings", {})
    delay = int(CONFIG.get("sticker_delay", DEFAULT_STICKER_DELAY))
    await update.effective_message.reply_text(
        "🟢 BOT ACTIF\n\n"
        f"🎬 Animes : {len(CONFIG.get('animes', []))}\n"
        f"📡 Sources : {len(CONFIG.get('sources', []))}\n"
        f"👥 Utilisateurs : {len(CONFIG.get('users', []))}\n"
        f"🔐 Administrateurs : {len(CONFIG.get('admins', []))}\n"
        f"🚫 Bannissements : {len(CONFIG.get('banned_users', []))}\n"
        f"⏱️ Sticker : {delay // 60} min\n"
        f"🌐 Nautiljon : {'ON' if settings.get('nautiljon_enabled', True) else 'OFF'}\n"
        f"🌐 AniList : {'ON' if settings.get('anilist_enabled', True) else 'OFF'}\n"
        f"🔄 Update hebdo : {'ON' if settings.get('weekly_update', True) else 'OFF'}\n"
        f"🎯 Destination : {CONFIG.get('destination')}\n"
        f"🔄 Messages source traités : {len(PROCESSED_MESSAGES)}\n"
    )


async def config_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await security_check(update, admin_only=True):
        return
    await update.effective_message.reply_text(json.dumps(CONFIG, ensure_ascii=False, indent=2)[:4000])


async def reload_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global CONFIG
    if not await security_check(update, admin_only=True):
        return
    CONFIG = load_config()
    await update.effective_message.reply_text("🔄 Configuration rechargée.")

# ============================================================
# ANIMES / ALIAS
# ============================================================

async def animes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await security_check(update):
        return
    animes = CONFIG.get("animes", [])
    if not animes:
        await update.effective_message.reply_text("❌ Aucun anime configuré.")
        return
    lines = ["🎬 ANIMES CONFIGURÉS\n"]
    for i, raw in enumerate(animes, 1):
        anime = normalize_anime_entry(raw)
        if not anime:
            continue
        lines += [f"{i}. {anime['name']}", f"   🔤 Alias : {len(anime.get('aliases', []))}", f"   🎨 Sticker : {'🎨 Oui' if anime.get('sticker') else '❌ Non'}", ""]
    await update.effective_message.reply_text("\n".join(lines)[:4000])


async def addanime_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await security_check(update, admin_only=True):
        return
    if not context.args:
        await update.effective_message.reply_text("❌ Utilisation : /addanime Nom de l'anime")
        return
    name = " ".join(context.args).strip()
    existing = get_anime_by_name(name)
    if existing:
        await update.effective_message.reply_text(f"ℹ️ Cet anime existe déjà :\n🎬 {existing['name']}\n\nUtilise /findalias pour rechercher de nouveaux alias.")
        return
    anime = {"name": name, "aliases": []}
    CONFIG.setdefault("animes", []).append(anime)
    save_config()
    msg = await update.effective_message.reply_text(f"✅ Anime ajouté : {name}\n\n🔎 Recherche Nautiljon puis AniList...")
    aliases, source = await asyncio.to_thread(find_aliases, name)
    added = add_aliases_to_anime(anime, aliases)
    save_config()
    if aliases:
        lines = [f"🎬 {name}", "", f"🌐 Sources : {source}", f"🔤 Alias trouvés : {len(aliases)}", ""]
        lines.extend(("✅ " if alias in added else "• ") + alias for alias in aliases)
        lines += ["", f"💾 Nouveaux alias ajoutés : {len(added)}"]
        await msg.edit_text("\n".join(lines)[:4000])
    else:
        await msg.edit_text(f"🎬 Anime ajouté : {name}\n\n⚠️ Aucun alias trouvé.\n\nTu peux réessayer avec :\n/findalias {name}")


async def removeanime_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await security_check(update, admin_only=True):
        return
    if not context.args:
        await update.effective_message.reply_text("/removeanime Nom")
        return
    wanted = normalize_text(" ".join(context.args))
    new = []
    removed = None
    for raw in CONFIG.get("animes", []):
        anime = normalize_anime_entry(raw)
        if anime and normalize_text(anime["name"]) == wanted:
            removed = anime
        elif anime:
            new.append(anime)
    if not removed:
        await update.effective_message.reply_text("❌ Anime introuvable.")
        return
    CONFIG["animes"] = new
    save_config()
    await update.effective_message.reply_text(f"🗑️ Anime supprimé :\n🎬 {removed['name']}")


async def aliases_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await security_check(update):
        return
    lines = ["🔤 ALIAS\n"]
    for raw in CONFIG.get("animes", []):
        anime = normalize_anime_entry(raw)
        if not anime:
            continue
        lines.append(f"🎬 {anime['name']}")
        aliases = anime.get("aliases", [])
        lines.extend(f"  • {a}" for a in aliases) if aliases else lines.append("  • Aucun alias")
        lines.append("")
    await update.effective_message.reply_text("\n".join(lines)[:4000])


async def addalias_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await security_check(update, admin_only=True):
        return
    argument = " ".join(context.args)
    if "|" not in argument:
        await update.effective_message.reply_text("/addalias NomAnime | Alias")
        return
    anime_name, alias = [x.strip() for x in argument.split("|", 1)]
    anime = get_anime_by_name(anime_name)
    if not anime:
        await update.effective_message.reply_text("❌ Anime introuvable.")
        return
    if not add_aliases_to_anime(anime, [alias]):
        await update.effective_message.reply_text("ℹ️ Cet alias existe déjà ou est identique au nom.")
        return
    save_config()
    await update.effective_message.reply_text(f"✅ Alias ajouté :\n🎬 {anime['name']}\n🔤 {alias}")


async def removealias_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await security_check(update, admin_only=True):
        return
    argument = " ".join(context.args)
    if "|" not in argument:
        await update.effective_message.reply_text("/removealias NomAnime | Alias")
        return
    anime_name, alias = [x.strip() for x in argument.split("|", 1)]
    anime = get_anime_by_name(anime_name)
    if not anime:
        await update.effective_message.reply_text("❌ Anime introuvable.")
        return
    if not remove_alias_from_anime(anime, alias):
        await update.effective_message.reply_text("❌ Alias introuvable.")
        return
    save_config()
    await update.effective_message.reply_text(f"🗑️ Alias supprimé :\n{alias}")


async def findalias_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await security_check(update, admin_only=True):
        return
    if not context.args:
        await update.effective_message.reply_text("/findalias Nom")
        return
    name = " ".join(context.args)
    anime = get_anime_by_name(name)
    if not anime:
        await update.effective_message.reply_text("❌ Anime introuvable.\nAjoute-le d'abord avec /addanime.")
        return
    msg = await update.effective_message.reply_text(f"🔎 Recherche des alias :\n🎬 {anime['name']}\n\n🌐 Nautiljon → AniList")
    aliases, source = await asyncio.to_thread(find_aliases, anime["name"])
    added = add_aliases_to_anime(anime, aliases)
    save_config()
    if not aliases:
        await msg.edit_text("❌ Aucun alias trouvé.\n\nLe bot continuera normalement.")
        return
    lines = [f"🎬 {anime['name']}", "", f"🌐 Sources utilisées : {source}", f"🔤 Alias trouvés : {len(aliases)}", ""]
    lines.extend(f"• {alias}" for alias in aliases)
    lines += ["", f"💾 Nouveaux alias ajoutés : {len(added)}"]
    await msg.edit_text("\n".join(lines)[:4000])

# ============================================================
# STICKERS
# ============================================================

async def setsticker_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await security_check(update, admin_only=True):
        return
    replied = update.effective_message.reply_to_message
    if not replied or not replied.sticker:
        await update.effective_message.reply_text("❌ Réponds à un sticker avec : /setsticker NomAnime")
        return
    if not context.args:
        await update.effective_message.reply_text("/setsticker NomAnime")
        return
    anime = get_anime_by_name(" ".join(context.args))
    if not anime:
        await update.effective_message.reply_text("❌ Anime introuvable.")
        return
    anime["sticker"] = replied.sticker.file_id
    save_config()
    await update.effective_message.reply_text(f"🎨 Sticker enregistré !\n\n🎬 {anime['name']}\n⏱️ Il sera envoyé après 3 minutes.")


async def removesticker_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await security_check(update, admin_only=True):
        return
    if not context.args:
        await update.effective_message.reply_text("/removesticker NomAnime")
        return
    anime = get_anime_by_name(" ".join(context.args))
    if not anime:
        await update.effective_message.reply_text("❌ Anime introuvable.")
        return
    if "sticker" not in anime:
        await update.effective_message.reply_text("ℹ️ Aucun sticker configuré.")
        return
    del anime["sticker"]
    save_config()
    await update.effective_message.reply_text(f"🗑️ Sticker supprimé pour :\n🎬 {anime['name']}")

# ============================================================
# SOURCES / DESTINATION
# ============================================================

async def sources_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await security_check(update):
        return
    sources = CONFIG.get("sources", [])
    await update.effective_message.reply_text("📡 SOURCES\n\n" + ("\n".join(f"{i}. {x}" for i, x in enumerate(sources, 1)) if sources else "❌ Aucune source."))


async def addsource_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await security_check(update, admin_only=True):
        return
    try:
        source_id = int(context.args[0])
    except (IndexError, ValueError):
        await update.effective_message.reply_text("/addsource ID")
        return
    if source_id in CONFIG.setdefault("sources", []):
        await update.effective_message.reply_text("ℹ️ Source déjà présente.")
        return
    CONFIG["sources"].append(source_id)
    save_config()
    await update.effective_message.reply_text(f"✅ Source ajoutée :\n{source_id}")


async def removesource_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await security_check(update, admin_only=True):
        return
    try:
        source_id = int(context.args[0])
    except (IndexError, ValueError):
        await update.effective_message.reply_text("/removesource ID")
        return
    if source_id not in CONFIG.get("sources", []):
        await update.effective_message.reply_text("❌ Source introuvable.")
        return
    CONFIG["sources"].remove(source_id)
    save_config()
    await update.effective_message.reply_text(f"🗑️ Source supprimée :\n{source_id}")


async def destination_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await security_check(update):
        await update.effective_message.reply_text(f"🎯 Destination :\n{CONFIG.get('destination')}")


async def setdestination_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await security_check(update, admin_only=True):
        return
    try:
        destination = int(context.args[0])
    except (IndexError, ValueError):
        await update.effective_message.reply_text("/setdestination ID")
        return
    CONFIG["destination"] = destination
    save_config()
    await update.effective_message.reply_text(f"✅ Destination modifiée :\n{destination}")

# ============================================================
# USERS
# ============================================================

async def adduser_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await security_check(update, admin_only=True):
        return
    try:
        user_id = int(context.args[0])
    except (IndexError, ValueError):
        await update.effective_message.reply_text("/adduser ID_TELEGRAM")
        return
    if user_id in CONFIG.get("banned_users", []):
        await update.effective_message.reply_text("❌ Cet utilisateur est banni. Utilise d'abord /unbanuser ID.")
        return
    if user_id in CONFIG.get("users", []) or is_admin(user_id):
        await update.effective_message.reply_text("ℹ️ Cet utilisateur est déjà autorisé.")
        return
    CONFIG.setdefault("users", []).append(user_id)
    save_config()
    await update.effective_message.reply_text(f"✅ Utilisateur autorisé :\n{user_id}")


async def removeuser_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await security_check(update, admin_only=True):
        return
    try:
        user_id = int(context.args[0])
    except (IndexError, ValueError):
        await update.effective_message.reply_text("/removeuser ID")
        return
    if is_admin(user_id):
        await update.effective_message.reply_text("❌ Impossible de retirer un administrateur avec /removeuser.")
        return
    if user_id not in CONFIG.get("users", []):
        await update.effective_message.reply_text("❌ Utilisateur introuvable.")
        return
    CONFIG["users"].remove(user_id)
    save_config()
    await update.effective_message.reply_text(f"🗑️ Utilisateur retiré :\n{user_id}")


async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await security_check(update, admin_only=True):
        return
    users = CONFIG.get("users", [])
    admins = CONFIG.get("admins", [])
    text = "👥 UTILISATEURS AUTORISÉS\n\n" + ("\n".join(f"• {x}" for x in users) if users else "Aucun utilisateur supplémentaire.")
    text += "\n\n🔐 ADMINISTRATEURS\n\n" + "\n".join(f"• {x}" for x in admins)
    await update.effective_message.reply_text(text[:4000])


async def banuser_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await security_check(update, admin_only=True):
        return
    try:
        user_id = int(context.args[0])
    except (IndexError, ValueError):
        await update.effective_message.reply_text("/banuser ID")
        return
    if is_admin(user_id):
        await update.effective_message.reply_text("❌ Impossible de bannir un administrateur.")
        return
    if user_id not in CONFIG.setdefault("banned_users", []):
        CONFIG["banned_users"].append(user_id)
    if user_id in CONFIG.get("users", []):
        CONFIG["users"].remove(user_id)
    save_config()
    await update.effective_message.reply_text(f"🚫 Utilisateur banni :\n{user_id}")


async def unbanuser_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await security_check(update, admin_only=True):
        return
    try:
        user_id = int(context.args[0])
    except (IndexError, ValueError):
        await update.effective_message.reply_text("/unbanuser ID")
        return
    if user_id not in CONFIG.get("banned_users", []):
        await update.effective_message.reply_text("ℹ️ Cet utilisateur n'est pas banni.")
        return
    CONFIG["banned_users"].remove(user_id)
    save_config()
    await update.effective_message.reply_text(f"✅ Utilisateur débanni :\n{user_id}\n\nIl devra être ajouté avec /adduser pour utiliser le bot.")


async def banned_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await security_check(update, admin_only=True):
        return
    banned = CONFIG.get("banned_users", [])
    await update.effective_message.reply_text("🚫 UTILISATEURS BANNIS\n\n" + ("\n".join(f"• {x}" for x in banned) if banned else "Aucun utilisateur banni."))

# ============================================================
# GITHUB UPDATE
# ============================================================

def github_raw_url() -> Optional[str]:
    if not GITHUB_REPO:
        return None
    repo = GITHUB_REPO.strip("/")
    branch = requests.utils.quote(GITHUB_BRANCH, safe="")
    file_path = "/".join(requests.utils.quote(part, safe="") for part in GITHUB_FILE.split("/"))
    return f"https://raw.githubusercontent.com/{repo}/{branch}/{file_path}"


def calculate_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def download_github_bot() -> Optional[bytes]:
    url = github_raw_url()
    if not url:
        return None
    try:
        response = HTTP_SESSION.get(url, timeout=30)
        response.raise_for_status()
        data = response.content
        if not data:
            return None
        text = data.decode("utf-8", errors="replace")
        if "import " not in text and "def " not in text:
            logger.warning("Le fichier GitHub semble invalide.")
            return None
        return data
    except Exception as e:
        logger.warning("GitHub update erreur : %s", e)
        return None


def validate_python_file(data: bytes) -> bool:
    try:
        compile(data.decode("utf-8"), GITHUB_FILE, "exec")
        return True
    except Exception as e:
        logger.warning("Nouvelle version Python invalide : %s", e)
        return False


def get_local_bot_sha() -> Optional[str]:
    try:
        with open(__file__, "rb") as f:
            return calculate_sha256(f.read())
    except Exception:
        return None


async def update_bot(force: bool = False) -> Dict[str, Any]:
    async with UPDATE_LOCK:
        data = await asyncio.to_thread(download_github_bot)
        if not data:
            return {"success": False, "updated": False, "message": "❌ Impossible de récupérer bot.py depuis GitHub."}
        remote_sha = calculate_sha256(data)
        local_sha = get_local_bot_sha()
        if not force and local_sha and remote_sha == local_sha:
            return {"success": True, "updated": False, "message": "✅ Le bot est déjà à jour."}
        if not validate_python_file(data):
            return {"success": False, "updated": False, "message": "❌ La nouvelle version contient une erreur Python. Mise à jour annulée."}
        bot_path = os.path.abspath(__file__)
        backup_path = bot_path + ".backup"
        temp_path = None
        try:
            with open(bot_path, "rb") as f:
                old_data = f.read()
            with open(backup_path, "wb") as f:
                f.write(old_data)
            directory = os.path.dirname(bot_path)
            fd, temp_path = tempfile.mkstemp(prefix=".bot_update_", suffix=".py", dir=directory)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, bot_path)
            return {"success": True, "updated": True, "message": "🚀 Nouvelle version installée.\n🔄 Redémarrage..."}
        except Exception as e:
            logger.exception("Erreur installation update")
            try:
                if os.path.exists(backup_path):
                    with open(backup_path, "rb") as f:
                        backup_data = f.read()
                    with open(bot_path, "wb") as f:
                        f.write(backup_data)
            except Exception:
                pass
            return {"success": False, "updated": False, "message": f"❌ Mise à jour annulée : {e}"}


async def restart_process():
    await asyncio.sleep(3)
    os.execv(sys.executable, [sys.executable, *sys.argv])


async def update_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await security_check(update, admin_only=True):
        return
    if not GITHUB_REPO:
        await update.effective_message.reply_text('⚠️ GITHUB_REPO n\'est pas configuré.\n\nExemple : GITHUB_REPO="jinwooleboss/Telegram"')
        return
    msg = await update.effective_message.reply_text("🔎 Vérification de GitHub...")
    result = await update_bot()
    await msg.edit_text(result["message"])
    if result["updated"]:
        asyncio.create_task(restart_process())

# ============================================================
# BACKGROUND TASKS
# ============================================================

async def weekly_update_loop(application: Application):
    await asyncio.sleep(60)
    while True:
        try:
            settings = CONFIG.get("settings", {})
            if settings.get("weekly_update", True) and GITHUB_REPO:
                result = await update_bot()
                if result["updated"]:
                    asyncio.create_task(restart_process())
                    return
        except Exception as e:
            logger.warning("Update hebdomadaire erreur : %s", e)
        await asyncio.sleep(UPDATE_INTERVAL)


async def cleanup_loop(application: Application):
    # Les IDs des messages source sont conservés durablement afin qu'un
    # redémarrage du bot ne republie pas le même message.
    while True:
        await asyncio.sleep(3600)

# ============================================================
# POST INIT / ERROR
# ============================================================

async def post_init(application: Application):
    commands = [
        ("start", "Démarrer le bot"),
        ("setepisode", "Corriger saison/épisode"),
        ("addepisode", "Enregistrer un lien d’épisode"), ("help", "Commandes"), ("status", "État du bot"),
        ("addanime", "Ajouter un anime"), ("removeanime", "Supprimer un anime"), ("animes", "Liste des animes"),
        ("aliases", "Liste des alias"), ("addalias", "Ajouter un alias"), ("removealias", "Supprimer un alias"),
        ("findalias", "Rechercher les alias"), ("setsticker", "Définir un sticker"), ("removesticker", "Supprimer un sticker"),
        ("sources", "Liste des sources"), ("addsource", "Ajouter une source"), ("removesource", "Supprimer une source"),
        ("destination", "Voir destination"), ("setdestination", "Changer destination"),
        ("adduser", "Autoriser un utilisateur"), ("removeuser", "Retirer un utilisateur"), ("users", "Utilisateurs autorisés"),
        ("banuser", "Bannir utilisateur"), ("unbanuser", "Débannir utilisateur"), ("banned", "Liste des bannis"),
        ("config", "Configuration"), ("reload", "Recharger configuration"), ("update", "Vérifier une mise à jour"),
    ]
    try:
        await application.bot.set_my_commands([BotCommand(a, b) for a, b in commands])
    except Exception as e:
        logger.warning("Menu Telegram erreur : %s", e)
    application.create_task(cleanup_loop(application))
    if GITHUB_REPO:
        application.create_task(weekly_update_loop(application))
        logger.info("🔄 Mise à jour hebdomadaire activée.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    error = context.error
    if not error:
        return
    text = str(error).lower()
    if any(x in text for x in ("bad gateway", "timed out", "timeout", "network", "connection reset")):
        logger.warning("Erreur réseau temporaire : %s", error)
        return
    logger.exception("Exception Telegram : %s", error)

# ============================================================
# MAIN
# ============================================================

def main():
    global CONFIG
    CONFIG = load_config()
    load_episode_links()
    load_anime_fiches()
    load_processed_messages()
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN n'est pas configuré. Définis la variable BOT_TOKEN avant de lancer le bot.")

    logger.info("========================================")
    logger.info("🤖 AUTO FORWARDER DÉMARRÉ")
    logger.info("🎬 Animes : %d", len(CONFIG.get("animes", [])))
    logger.info("📡 Sources : %s", CONFIG.get("sources"))
    logger.info("🎯 Destination : %s", CONFIG.get("destination"))
    logger.info("🔐 Admins : %s", CONFIG.get("admins"))
    logger.info("👥 Utilisateurs : %s", CONFIG.get("users"))
    logger.info("🚫 Bannis : %s", CONFIG.get("banned_users"))
    logger.info("========================================")

    request = HTTPXRequest(
        connection_pool_size=50,
        pool_timeout=30,
        connect_timeout=30,
        read_timeout=60,
        write_timeout=60,
    )

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .request(request)
        .concurrent_updates(32)
        .post_init(post_init)
        .build()
    )

    handlers = {
        "start": start_command, "help": help_command, "status": status_command,
        "config": config_command, "reload": reload_command, "update": update_command,
        "addanime": addanime_command, "removeanime": removeanime_command, "animes": animes_command,
        "aliases": aliases_command, "addalias": addalias_command, "removealias": removealias_command,
        "findalias": findalias_command, "setsticker": setsticker_command, "removesticker": removesticker_command,
        "sources": sources_command, "addsource": addsource_command, "removesource": removesource_command,
        "destination": destination_command, "setdestination": setdestination_command,
        "adduser": adduser_command, "removeuser": removeuser_command, "users": users_command,
        "banuser": banuser_command, "unbanuser": unbanuser_command, "banned": banned_command,
        "setepisode": setepisode_command,
        "addepisode": addepisode_command,
    }
    for command, handler in handlers.items():
        application.add_handler(CommandHandler(command, handler))

    # Les boutons affiche/sans affiche et réponses numériques admin.
    application.add_handler(CallbackQueryHandler(handle_poster_choice, pattern=r"^poster:(yes|no)(:-?\d+:\d+)?$"), group=0)

    # Les messages des chaînes/sources arrivent ici.
    application.add_handler(MessageHandler(filters.ALL, process_message), group=1)
    application.add_error_handler(error_handler)

    logger.info("🚀 Polling Telegram...")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("🛑 Bot arrêté.")
    except Exception as e:
        logger.exception("💥 Erreur fatale : %s", e)