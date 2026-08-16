import glob
import hashlib
import html
import json
import logging
import os
import re
import secrets
import shutil
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

HERMES_HOME = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
AUTH_DIR = os.path.join(HERMES_HOME, "weixin")
APPROVED_FILE = os.path.join(AUTH_DIR, "approved_users.json")
PENDING_FILE = os.path.join(AUTH_DIR, "pending_requests.json")
PROFILE_MAP_FILE = os.path.join(AUTH_DIR, "profile_map.json")

# In-memory store for pending two-step unregister confirmations (user_id -> timestamp)
UNREGISTER_PENDING: Dict[str, float] = {}
UNREGISTER_TIMEOUT_SECS = 120  # 2 minutes confirmation window

WELCOME_ON_SCAN_TEXT = """👋 您好！很高兴与您相遇。我是您的专属 AI 助理。

📌 请问我应该怎么称呼你呢？（您可以直接回复您的称呼或姓名）

💡 隐私与数据说明：
系统将为您提供专属独立 Profile 与物理记忆隔离。如需注销账号并彻底清空所有个人画像、记忆与对话数据，您可以随时发送【注销】或【/unregister】。

⏳ 系统正在为您接入中，请稍候..."""

APPROVED_COMMANDS_TEXT = """🎉 您的接入申请已获批准，专属独立空间已就绪！

🛠️ 常用指令说明：
• /commands — 查看所有可用命令与功能清单
• /new — 开启一段全新会话（重置当前上下文）
• /status — 查看当前 AI 状态、模型与系统信息
• /unregister 或发送【注销】 — 随时彻底清空所有记忆并注销账号

现在您可以直接向我发送任何消息，开始对话了！😊"""

UNREGISTER_CONFIRM_PROMPT = """⚠️ 【注销二次确认】请确认是否彻底注销账号？

注销后，您的专属独立空间、所有对话历史记录、个人偏好画像与长期记忆将【被永久彻底删除且无法恢复】。

🔴 如确认注销，请在 2 分钟内回复：【确认注销】
🟢 如需取消注销，请回复【取消】或直接继续发送其他正常对话内容。"""

# Pre-approved user IDs, opt-in only via WEIXIN_DEFAULT_APPROVED (comma separated).
# Never ship a hardcoded ID here: init_auth_store() re-adds these on every call,
# which would make the entry impossible to revoke via /reject_wechat.
DEFAULT_APPROVED = [
    uid.strip()
    for uid in os.getenv("WEIXIN_DEFAULT_APPROVED", "").split(",")
    if uid.strip()
]

# ── Admin channel authorization ──
# Management commands (approve / reject / list users / add account) may only be
# exercised from the Telegram admin channel. Everything else is refused.

ADMIN_CHANNEL = "telegram"

# authorize_admin() verdicts
ADMIN_OK = "ok"                  # proven to come from the Telegram admin channel
ADMIN_UNVERIFIED = "unverified"  # host passed no channel context — restricted mode
ADMIN_DENIED = "denied"          # proven to come from some other channel

ADMIN_ONLY_HINT = (
    "❌ 管理命令仅限管理员在 Telegram 渠道使用。\n"
    "请在 Telegram 管理员会话中重新执行该命令。"
)

TELEGRAM_UNCONFIGURED_HINT = (
    "❌ 未配置 Telegram 管理渠道，管理命令已禁用。\n"
    "请设置 TELEGRAM_BOT_TOKEN 与 TELEGRAM_HOME_CHANNEL（或在 "
    f"{os.path.join(HERMES_HOME, 'profiles', 'telegram', '.env')} 中配置）后重试。"
)

# Attribute / key names a host context object may use to identify the channel.
_CHANNEL_KEYS = ("platform", "channel", "platform_name", "adapter_name", "source_platform")
_CHAT_KEYS = ("chat_id", "user_id", "sender_id", "from_user_id")
_NESTED_KEYS = ("source", "context", "event", "message")


def _admin_channel_setting() -> str:
    """Which channel may run management commands. ``any`` disables the gate."""
    return (os.getenv("WEIXIN_ADMIN_CHANNEL", ADMIN_CHANNEL) or ADMIN_CHANNEL).strip().lower()


def _admin_chat_ids() -> set:
    """Configured Telegram admin chat/user IDs, if any."""
    ids = set()
    _, chat_id = _get_telegram_config()
    for raw in (chat_id or "").split(","):
        raw = raw.strip()
        if raw:
            ids.add(raw)
    return ids


def _admin_user_ids() -> set:
    """Telegram *user* IDs allowed to act as admin.

    Distinct from _admin_chat_ids(): TELEGRAM_HOME_CHANNEL is a chat to send
    to (and may be a negative group ID), which is not comparable to the user
    ID behind a button click. Prefer TELEGRAM_ALLOWED_USERS, and only fall
    back to the channel when that is all that is configured.
    """
    allowed = _read_telegram_env("TELEGRAM_ALLOWED_USERS")
    ids = {p.strip() for p in (allowed or "").split(",") if p.strip()}
    return ids or _admin_chat_ids()


def telegram_configured() -> bool:
    token, chat_id = _get_telegram_config()
    return bool(token and chat_id)


def _extract_channel(obj: Any, depth: int = 0) -> Tuple[Optional[str], Optional[str]]:
    """Best-effort duck-typed read of (channel, chat_id) from a host context object."""
    if obj is None or depth > 2 or isinstance(obj, (str, bytes, int, float, bool)):
        return None, None

    if isinstance(obj, dict):
        def get(key: str) -> Any:
            return obj.get(key)
    else:
        def get(key: str) -> Any:
            return getattr(obj, key, None)

    channel = None
    for key in _CHANNEL_KEYS:
        value = get(key)
        if value is None:
            continue
        # Platform enums expose .value; plain strings fall through unchanged.
        name = getattr(value, "value", None) or getattr(value, "name", None) or value
        if isinstance(name, str) and name.strip():
            channel = name.strip().lower()
            break

    chat_id = None
    for key in _CHAT_KEYS:
        value = get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            chat_id = str(value).strip()
            break

    if channel is None:
        for key in _NESTED_KEYS:
            nested_channel, nested_chat = _extract_channel(get(key), depth + 1)
            if nested_channel:
                return nested_channel, nested_chat or chat_id
    return channel, chat_id


def resolve_invocation_channel(*args: Any, **kwargs: Any) -> Tuple[Optional[str], Optional[str]]:
    """Identify the channel a command was invoked from, or (None, None) if unknowable."""
    for candidate in list(kwargs.values()) + list(args):
        channel, chat_id = _extract_channel(candidate)
        if channel:
            return channel, chat_id
    return None, None


def authorize_admin(*args: Any, **kwargs: Any) -> Tuple[str, str]:
    """
    Decide whether a management command may run.

    Returns ``(verdict, denial_message)``. Callers must treat
    ``ADMIN_UNVERIFIED`` as restricted: never return sensitive data to the
    caller (push it to the Telegram admin chat instead) and never accept a
    state change that is not backed by a secret pairing code.
    """
    if _admin_channel_setting() == "any":
        return ADMIN_OK, ""

    if not telegram_configured():
        return ADMIN_DENIED, TELEGRAM_UNCONFIGURED_HINT

    channel, chat_id = resolve_invocation_channel(*args, **kwargs)
    if channel is None:
        return ADMIN_UNVERIFIED, ""

    if channel != ADMIN_CHANNEL and not channel.startswith(ADMIN_CHANNEL):
        logger.warning("[Weixin Auth] Refused management command from channel=%s", channel)
        return ADMIN_DENIED, ADMIN_ONLY_HINT

    admins = _admin_chat_ids()
    if admins and chat_id and chat_id not in admins:
        logger.warning("[Weixin Auth] Refused management command from telegram chat=%s", chat_id)
        return ADMIN_DENIED, ADMIN_ONLY_HINT

    return ADMIN_OK, ""


# Throttle for actions an unverified caller can still trigger, so they cannot be
# used to spam the Telegram admin chat.
_THROTTLE_LAST: Dict[str, float] = {}


def throttle(key: str, min_interval_secs: float) -> bool:
    """True if *key* may run now; False if it ran less than *min_interval_secs* ago."""
    now = time.time()
    if now - _THROTTLE_LAST.get(key, 0.0) < min_interval_secs:
        return False
    _THROTTLE_LAST[key] = now
    return True


_TELEGRAM_ENV_FILES = (
    os.path.join(HERMES_HOME, "profiles", "telegram", ".env"),
    os.path.join(HERMES_HOME, ".env"),
)


def _read_telegram_env(key: str) -> Optional[str]:
    """Read *key* from the environment, falling back to the Hermes .env files."""
    value = os.getenv(key)
    if value:
        return value.strip()
    prefix = f"{key}="
    for path in _TELEGRAM_ENV_FILES:
        if not os.path.exists(path):
            continue
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith(prefix):
                        found = line[len(prefix):].strip()
                        if found:
                            return found
        except OSError:
            continue
    return None


def _get_telegram_config() -> Tuple[Optional[str], Optional[str]]:
    """Retrieve Telegram Bot Token and Admin Chat ID from env files."""
    token = _read_telegram_env("TELEGRAM_BOT_TOKEN")
    chat_id = (
        _read_telegram_env("TELEGRAM_HOME_CHANNEL")
        or _read_telegram_env("TELEGRAM_ALLOWED_USERS")
    )
    return token, chat_id

def _load_json(file_path: str, default: dict) -> dict:
    if os.path.exists(file_path):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning("Failed to load JSON from %s: %s", file_path, e)
    return default

def _save_json(file_path: str, data: dict) -> None:
    """Write *data* atomically, so a crash can never truncate the whitelist."""
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    tmp_path = f"{file_path}.tmp.{os.getpid()}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, file_path)
    except Exception as e:
        logger.error("Failed to save JSON to %s: %s", file_path, e)
        try:
            os.remove(tmp_path)
        except OSError:
            pass

# ── Per-user profile naming ──
# The profile directory name must be a *lossless* function of the WeChat user
# ID. The original scheme lowercased the ID, replaced non-alphanumerics with
# "_" and truncated to 26 chars — but WeChat openids are 28 chars and case
# sensitive, so distinct users could collide onto one profile and share
# USER.md / MEMORY.md / session history. Hashing removes the collision.

def _legacy_profile_name(user_id: str) -> str:
    """The pre-migration (lossy, collision-prone) profile name."""
    raw_id = user_id.split("@")[0] if "@" in user_id else user_id
    clean_id = re.sub(r"[^a-zA-Z0-9_]", "_", raw_id).lower()[:26]
    return f"wx_{clean_id}"


def _hashed_profile_name(user_id: str) -> str:
    return "wx_" + hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:16]


def _load_profile_map() -> dict:
    data = _load_json(PROFILE_MAP_FILE, {})
    if not isinstance(data.get("users"), dict):
        data = {"users": {}, "adopted_legacy": []}
    if not isinstance(data.get("adopted_legacy"), list):
        data["adopted_legacy"] = []
    return data


def profile_name_for(user_id: str) -> str:
    """Stable, collision-free profile name for *user_id*.

    A user who already has a legacy-named profile directory keeps that name.
    Renaming the directory is deliberately avoided: the profile name is also
    embedded in gateway session keys
    (``agent:<profile>:weixin_multi:dm:<user>``) and in persisted gateway
    state, so a rename leaves those pointing at a name that no longer exists —
    splitting the user's session records and making the host re-materialize an
    empty directory under the old name.

    A legacy name is claimed by at most one user, recorded in the map. If a
    second user hashes onto the same legacy name (i.e. they were already
    sharing a profile under the old lossy scheme) the later user gets a clean
    hashed profile rather than inheriting the first user's data — which is the
    collision this scheme exists to prevent. Every new user gets a hashed name
    from the start.
    """
    data = _load_profile_map()
    existing = data["users"].get(user_id)
    if isinstance(existing, str) and existing:
        return existing

    name = _hashed_profile_name(user_id)
    legacy = _legacy_profile_name(user_id)
    legacy_dir = os.path.join(HERMES_HOME, "profiles", legacy)

    if legacy != name and os.path.isdir(legacy_dir):
        if legacy in data["adopted_legacy"]:
            logger.warning(
                "[Weixin Auth] Legacy profile %s is already claimed by another user; "
                "giving a clean profile instead so it cannot inherit their data.",
                legacy,
            )
        else:
            name = legacy
            data["adopted_legacy"].append(legacy)
            logger.info("[Weixin Auth] Claiming existing profile %s for its owner", legacy)

    data["users"][user_id] = name
    _save_json(PROFILE_MAP_FILE, data)
    return name


def forget_profile_mapping(user_id: str) -> None:
    """Drop a user's profile mapping (used on unregister)."""
    data = _load_profile_map()
    if data["users"].pop(user_id, None) is not None:
        _save_json(PROFILE_MAP_FILE, data)


# A profile cloned from "default" inherits its .env verbatim, including the
# shared platform credentials. That makes every per-user profile try to start
# its own Telegram/Discord/WeChat adapters (the gateway then refuses them as
# duplicate-credential) and leaves a copy of the master bot tokens in each
# user's directory. Strip platform credentials — but never the model API keys,
# which the per-user agent needs to run.
_PLATFORM_CREDENTIAL_RE = re.compile(
    r"^(TELEGRAM|DISCORD|SLACK|WHATSAPP|SIGNAL|MATRIX|LINE|VIBER|TWILIO|WEIXIN"
    r"|WECHAT|MESSENGER|INSTAGRAM|IMESSAGE|RELAY)_[A-Z0-9_]*"
    r"(TOKEN|SECRET|PASSWORD|CREDENTIAL)$"
)
_EXTRA_SCRUBBED_ENV_KEYS = frozenset({
    "HERMES_GATEWAY_TOKEN",
    # Not a credential, but an *enablement* key: the host enables the built-in
    # weixin platform when either WEIXIN_TOKEN or WEIXIN_ACCOUNT_ID is set
    # (gateway/config.py: `if weixin_token or weixin_account_id`). Leaving it
    # behind makes a per-user profile try to start an adapter it has no token
    # for, logging "WEIXIN_TOKEN is required" on every restart. Secondary
    # profiles never own platform connections in multiplex mode.
    "WEIXIN_ACCOUNT_ID",
})


def _is_platform_credential(key: str) -> bool:
    return bool(_PLATFORM_CREDENTIAL_RE.match(key)) or key in _EXTRA_SCRUBBED_ENV_KEYS


def scrub_platform_credentials(env_path: str) -> List[str]:
    """Remove inherited platform credentials from a profile .env. Returns removed keys."""
    if not os.path.exists(env_path):
        return []
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as e:
        logger.warning("[Weixin Auth] Could not read %s: %s", env_path, e)
        return []

    removed, kept = [], []
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if key and not line.lstrip().startswith("#") and _is_platform_credential(key):
            removed.append(key)
        else:
            kept.append(line)

    if removed:
        try:
            fd = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.writelines(kept)
            # O_CREAT's mode is ignored for an existing file — set it explicitly.
            os.chmod(env_path, 0o600)
            logger.info(
                "[Weixin Auth] Stripped %d inherited platform credential(s) from %s: %s",
                len(removed), env_path, ", ".join(removed),
            )
        except OSError as e:
            logger.error("[Weixin Auth] Could not rewrite %s: %s", env_path, e)
            return []
    return removed


USER_MD_TEMPLATE = (
    "_Learn about the person you're helping. Update this as you go.\n§\n"
    "**Name:**\n§\n**What to call them:**\n§\n**Pronouns:** _(optional)_\n§\n"
    "**Timezone:**\n§\n**Notes:**\n"
)


def _reset_memories(mem_dir: str) -> None:
    """Write a blank per-user memory set into *mem_dir*."""
    os.makedirs(mem_dir, exist_ok=True)
    with open(os.path.join(mem_dir, "USER.md"), "w", encoding="utf-8") as uf:
        uf.write(USER_MD_TEMPLATE)
    with open(os.path.join(mem_dir, "MEMORY.md"), "w", encoding="utf-8") as mf:
        mf.write("")


def _is_copy_of(path: str, other: str) -> bool:
    """True when *path* is byte-identical to a non-empty *other*.

    Compares size first so the common case costs two stats. An empty source
    cannot leak anything, so it never counts as a copy.
    """
    try:
        size = os.path.getsize(other)
        if size == 0 or os.path.getsize(path) != size:
            return False
    except OSError:
        return False
    try:
        with open(path, "rb") as a, open(other, "rb") as b:
            return a.read() == b.read()
    except OSError:
        return False


def purge_inherited_memories(profile_name: str) -> List[str]:
    """Blank a profile's memories if they are a copy of the default profile's.

    The host materializes a missing profile by cloning the default one — which
    copies its ``memories/`` verbatim. Because ``ensure_user_profile`` only
    creates a profile when ``profile_exists()`` is False, a profile the host
    materialized first is treated as already set up and its inherited memories
    would never be blanked, leaving one user's agent primed with the instance
    owner's USER.md and MEMORY.md. Returns the files that were reset.
    """
    pdir = os.path.join(_profiles_dir(), profile_name)
    mem_dir = os.path.join(pdir, "memories")
    default_mem = os.path.join(HERMES_HOME, "memories")

    leaked = [
        name for name in ("MEMORY.md", "USER.md")
        if _is_copy_of(os.path.join(mem_dir, name), os.path.join(default_mem, name))
    ]
    if leaked:
        logger.warning(
            "[Weixin Auth] Profile %s inherited the default profile's %s; resetting to a blank set.",
            profile_name, ", ".join(leaked),
        )
        _reset_memories(mem_dir)
    return leaked


def ensure_user_profile(user_id: str) -> str:
    """Return the user's profile name, creating an isolated profile if needed."""
    from hermes_cli.profiles import create_profile, profile_exists

    profile_name = profile_name_for(user_id)
    if profile_exists(profile_name):
        # Existing is not the same as isolated — the host may have created it.
        purge_inherited_memories(profile_name)
        return profile_name

    logger.info("[Weixin Auth] Creating isolated profile for user=%s -> %s", user_id, profile_name)
    pdir = create_profile(profile_name, clone_from="default", clone_config=True, no_alias=True)

    # Strip inherited platform config: a cloned profile must not start its own
    # gateway adapters (that would double-poll every configured account).
    cfg_path = os.path.join(pdir, "config.yaml")
    if os.path.exists(cfg_path):
        try:
            import yaml
            with open(cfg_path) as f:
                cfg = yaml.safe_load(f) or {}
            if isinstance(cfg.get("gateway"), dict):
                cfg["gateway"]["platforms"] = {}
                with open(cfg_path, "w") as f:
                    yaml.safe_dump(cfg, f, default_flow_style=False, allow_unicode=True)
        except Exception as e:
            logger.warning("[Weixin Auth] Could not scrub platforms from %s: %s", cfg_path, e)

    # Same reason, for credentials inherited through the cloned .env.
    scrub_platform_credentials(os.path.join(pdir, ".env"))

    # The clone also copied the default profile's memories — replace them.
    _reset_memories(os.path.join(pdir, "memories"))
    return profile_name


def init_auth_store():
    """Ensure approved_users.json is initialized with defaults."""
    data = _load_json(APPROVED_FILE, {"approved": {}})
    changed = False
    for uid in DEFAULT_APPROVED:
        if uid not in data.get("approved", {}):
            if "approved" not in data:
                data["approved"] = {}
            data["approved"][uid] = {
                "approved_at": datetime.now().isoformat(),
                "approved_by": "system_default"
            }
            changed = True
    if changed or not os.path.exists(APPROVED_FILE):
        _save_json(APPROVED_FILE, data)

def is_user_approved(user_id: str) -> bool:
    """Check if a WeChat user ID is approved."""
    init_auth_store()
    data = _load_json(APPROVED_FILE, {"approved": {}})
    return user_id in data.get("approved", {})

def send_telegram_notification(text: str) -> bool:
    """Send alert to Telegram admin."""
    token, chat_id = _get_telegram_config()
    if not token or not chat_id:
        logger.warning("[Weixin Auth] Cannot send Telegram alert: token or chat_id missing.")
        return False
    
    try:
        import requests
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
        }
        resp = requests.post(url, json=payload, timeout=8)
        return resp.status_code == 200
    except Exception as e:
        logger.error("[Weixin Auth] Error sending Telegram alert: %s", e)
    return False

def send_telegram_approval_card(
    user_id: str,
    code: str,
    initial_text: Optional[str] = None,
    account_id: Optional[str] = None,
) -> bool:
    """Send an interactive approval card with buttons to Telegram admin."""
    token, chat_id = _get_telegram_config()
    if not token or not chat_id:
        logger.warning("[Weixin Auth] Cannot send Telegram approval: token or chat_id missing.")
        return False

    text_preview = (initial_text or "").strip()
    if len(text_preview) > 200:
        text_preview = text_preview[:200] + "..."
    elif not text_preview:
        text_preview = "(无文本内容或为图片/表情)"

    safe_preview = html.escape(text_preview)
    safe_user_id = html.escape(user_id)
    safe_account_id = html.escape(account_id or "默认")

    msg = (
        f"🔔 <b>微信新用户申请接入</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>用户 ID:</b> <code>{safe_user_id}</code>\n"
        f"🤖 <b>接收账号:</b> <code>{safe_account_id}</code>\n"
        f"💬 <b>初次发送内容:</b>\n<blockquote>{safe_preview}</blockquote>\n"
        f"⏰ <b>申请时间:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"请点击下方按钮直接处理："
    )

    reply_markup = {
        "inline_keyboard": [
            [
                {"text": "✅ 批准加入", "callback_data": f"wx:appr:{code}"},
                {"text": "❌ 拒绝", "callback_data": f"wx:deny:{code}"}
            ]
        ]
    }

    try:
        import requests
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": msg,
            "parse_mode": "HTML",
            "reply_markup": reply_markup,
        }
        resp = requests.post(url, json=payload, timeout=8)
        if resp.status_code == 200:
            logger.info("[Weixin Auth] Sent Telegram approval card successfully for user=%s.", user_id)
            return True
        else:
            logger.warning("[Weixin Auth] Telegram sendMessage failed: %s", resp.text)
    except Exception as e:
        logger.error("[Weixin Auth] Error sending Telegram approval card: %s", e)
    return False

def _ilink_headers(token: str, body_len: int) -> Dict[str, str]:
    """Headers for a direct iLink call.

    ``AuthorizationType`` is not optional: without it the API answers
    ``errcode:-14 session timeout`` and the message is never delivered, no
    matter how well-formed the rest of the request is. Keep this in sync with
    ``_headers()`` in weixin.py — that module is loaded under a synthetic
    module name, so it cannot simply be imported here.
    """
    return {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Content-Length": str(body_len),
        "X-WECHAT-UIN": "0",
        "iLink-App-Id": "bot",
        "iLink-App-ClientVersion": str((2 << 16) | (2 << 8) | 0),
        "Authorization": f"Bearer {token}",
    }


def _ilink_call_ok(data: Dict[str, Any]) -> bool:
    """True only when iLink reported no error.

    Errors come back as HTTP 200 with either ``ret`` or ``errcode`` set, so
    both must be checked — testing ``ret`` alone reads ``errcode:-14`` as
    success (``data.get("ret")`` is None, and None was treated as OK).
    """
    return data.get("ret") in (0, None) and data.get("errcode") in (0, None)


def send_wechat_message(user_id: str, account_id: Optional[str] = None, text: str = APPROVED_COMMANDS_TEXT) -> bool:
    """Proactively send a message to a WeChat user.

    When *account_id* is known we use only that account — the user has no
    relationship with the other bots, so falling back to them cannot work and
    would at best deliver from a stranger.
    """
    try:
        import requests
        accounts_dir = os.path.join(HERMES_HOME, "weixin", "accounts")
        if not os.path.exists(accounts_dir):
            return False

        account_files = []
        if account_id and os.path.exists(os.path.join(accounts_dir, f"{account_id}.json")):
            account_files.append(os.path.join(accounts_dir, f"{account_id}.json"))
        else:
            for c in sorted(glob.glob(os.path.join(accounts_dir, "*.json"))):
                if not c.endswith((".sync.json", ".context-tokens.json", "pending_qr.json")):
                    account_files.append(c)

        for account_file in account_files:
            try:
                with open(account_file, "r", encoding="utf-8") as f:
                    acc = json.load(f)
                token = acc.get("token")
                base_url = acc.get("base_url", "https://ilinkai.weixin.qq.com").rstrip("/")
                if not token or token == "???":
                    continue

                ctx_token = None
                token_file = account_file.replace(".json", ".context-tokens.json")
                if os.path.exists(token_file):
                    try:
                        with open(token_file, "r", encoding="utf-8") as tf:
                            tokens = json.load(tf)
                            ctx_token = tokens.get(user_id)
                    except Exception:
                        pass

                payload = {
                    "msg": {
                        "from_user_id": "",
                        "to_user_id": user_id,
                        "client_id": f"hermes-msg-{uuid.uuid4().hex}",
                        "message_type": 2,
                        "message_state": 2,
                        "item_list": [{"type": 1, "text_item": {"text": text}}]
                    }
                }
                if ctx_token:
                    payload["msg"]["context_token"] = ctx_token

                url = f"{base_url}/ilink/bot/sendmessage"
                body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                resp = requests.post(
                    url, data=body, headers=_ilink_headers(token, len(body)), timeout=10
                )
                data = resp.json() if resp.status_code == 200 else {}
                if resp.status_code == 200 and _ilink_call_ok(data):
                    logger.info("[Weixin Auth] Successfully sent message to %s using %s", user_id, os.path.basename(account_file))
                    return True
                else:
                    logger.warning("[Weixin Auth] Send message using %s returned %s: %s", os.path.basename(account_file), resp.status_code, resp.text)
            except Exception as e:
                logger.warning("[Weixin Auth] Error trying account %s: %s", account_file, e)

        return False
    except Exception as e:
        logger.error("[Weixin Auth] Failed to send message to %s: %s", user_id, e)
        return False

# Backward-compat alias
send_wechat_welcome_message = send_wechat_message

def create_pending_request(
    user_id: str,
    account_id: Optional[str] = None,
    initial_text: Optional[str] = None,
) -> Tuple[str, bool]:
    """
    Create or get existing pending request code for a user.
    Sends interactive button card to Telegram.
    Returns (code, is_new_or_not_rate_limited).
    """
    init_auth_store()
    pending = _load_json(PENDING_FILE, {})

    now = time.time()
    existing_code = None
    for code, info in list(pending.items()):
        if info.get("user_id") == user_id:
            if now - info.get("created_at", 0) < 86400:
                existing_code = code
                break
            else:
                del pending[code]

    if existing_code:
        last_notified = pending[existing_code].get("last_notified_at", 0)
        if now - last_notified > 180:
            pending[existing_code]["last_notified_at"] = now
            if initial_text:
                pending[existing_code]["initial_text"] = initial_text
            _save_json(PENDING_FILE, pending)
            send_telegram_approval_card(
                user_id, existing_code, initial_text=initial_text, account_id=account_id
            )
        return existing_code, False

    # High-entropy code: it doubles as the capability that authorizes approval,
    # and it is only ever transmitted to the Telegram admin chat.
    code = secrets.token_hex(4)
    while code in pending:
        code = secrets.token_hex(4)

    pending[code] = {
        "user_id": user_id,
        "account_id": account_id,
        "initial_text": initial_text or "",
        "created_at": now,
        "last_notified_at": now,
    }
    _save_json(PENDING_FILE, pending)
    send_telegram_approval_card(
        user_id, code, initial_text=initial_text, account_id=account_id
    )
    return code, True

def approve_user_request(
    identifier: str,
    approver: str = "telegram_admin",
    allow_user_id: bool = True,
) -> Tuple[bool, str, Optional[str], Optional[str]]:
    """
    Approve a user by pairing code, or (verified Telegram admins only) by exact user_id.
    Returns (success, message, user_id, account_id).

    ``allow_user_id=False`` restricts approval to presenting a valid pairing
    code. The code is only ever delivered to the Telegram admin chat, so this
    keeps approval a Telegram-only capability even when the host gives us no
    channel context to check.
    """
    init_auth_store()
    pending = _load_json(PENDING_FILE, {})
    approved_data = _load_json(APPROVED_FILE, {"approved": {}})

    identifier = identifier.strip()
    target_code = None
    target_user_id = None
    account_id = None

    if identifier in pending:
        target_code = identifier
        target_user_id = pending[identifier].get("user_id")
        account_id = pending[identifier].get("account_id")
    elif allow_user_id:
        # Exact match only — prefix matching could approve an unintended user.
        for code, info in pending.items():
            if info.get("user_id") == identifier:
                target_code = code
                target_user_id = info.get("user_id")
                account_id = info.get("account_id")
                break
        if not target_user_id and identifier.endswith("@im.wechat"):
            target_user_id = identifier

    if not target_user_id:
        if not allow_user_id:
            return False, f"未找到该配对码: {identifier}\n（未验证来源的调用只能使用配对码批准）", None, None
        return False, f"未找到该申请或用户: {identifier}", None, None

    if "approved" not in approved_data:
        approved_data["approved"] = {}
    approved_data["approved"][target_user_id] = {
        "approved_at": datetime.now().isoformat(),
        "approved_by": approver,
        "account_id": account_id,
    }
    _save_json(APPROVED_FILE, approved_data)

    if target_code and target_code in pending:
        del pending[target_code]
        _save_json(PENDING_FILE, pending)

    try:
        profile_name = ensure_user_profile(target_user_id)
    except Exception as e:
        logger.error("[Weixin Auth] Error creating profile for approved user: %s", e)
        profile_name = profile_name_for(target_user_id)

    # Proactively push approval notice and command usage guide. Report whether
    # it actually landed — this used to be fire-and-forget, which let a broken
    # push go unnoticed indefinitely.
    pushed = send_wechat_message(target_user_id, account_id=account_id, text=APPROVED_COMMANDS_TEXT)
    push_note = (
        "已向用户推送指令指南！" if pushed
        else "⚠️ 但向用户推送指令指南失败，请检查日志（用户仍可正常对话）。"
    )

    return True, f"已成功批准微信用户 {target_user_id}，并已创建专属独立 Profile [{profile_name}]，{push_note}", target_user_id, account_id

# ── Two-step Unregister Workflow ──

def request_unregister(user_id: str) -> str:
    """Initiate unregister request, asking user for secondary confirmation."""
    UNREGISTER_PENDING[user_id] = time.time()
    logger.info("[Weixin Auth] User %s requested unregister, waiting for confirmation", user_id)
    return UNREGISTER_CONFIRM_PROMPT

def has_pending_unregister(user_id: str) -> bool:
    """Check if user has an active pending unregister confirmation."""
    t = UNREGISTER_PENDING.get(user_id)
    if not t:
        return False
    if time.time() - t > UNREGISTER_TIMEOUT_SECS:
        UNREGISTER_PENDING.pop(user_id, None)
        return False
    return True

def cancel_unregister(user_id: str) -> bool:
    """Cancel unregister request."""
    if user_id in UNREGISTER_PENDING:
        UNREGISTER_PENDING.pop(user_id, None)
        logger.info("[Weixin Auth] User %s cancelled unregister", user_id)
        return True
    return False

def confirm_unregister(user_id: str) -> Tuple[bool, str]:
    """Execute complete unregister after two-step confirmation."""
    if not has_pending_unregister(user_id):
        return False, "⚠️ 注销请求已超时或未发起。如需注销，请重新发送【注销】指令。"
    UNREGISTER_PENDING.pop(user_id, None)
    return unregister_user(user_id)

def unregister_user(user_id: str) -> Tuple[bool, str]:
    """
    Completely unregister a user:
    1. Remove from approved_users.json and pending_requests.json
    2. Delete profile directory ~/.hermes/profiles/wx_<user_id>
    3. Send Telegram notification to admin
    """
    init_auth_store()
    approved_data = _load_json(APPROVED_FILE, {"approved": {}})
    pending = _load_json(PENDING_FILE, {})

    if user_id in approved_data.get("approved", {}):
        del approved_data["approved"][user_id]
        _save_json(APPROVED_FILE, approved_data)

    for code, info in list(pending.items()):
        if info.get("user_id") == user_id:
            del pending[code]
            _save_json(PENDING_FILE, pending)

    # Resolve through the mapping so we can never delete a different user's
    # profile because of a name collision.
    profile_name = profile_name_for(user_id)

    try:
        pdir = os.path.join(HERMES_HOME, "profiles", profile_name)
        if os.path.exists(pdir):
            shutil.rmtree(pdir)
            logger.info("[Weixin Auth] Purged profile directory %s for unregistering user %s", pdir, user_id)
    except Exception as e:
        logger.error("[Weixin Auth] Failed to delete profile dir %s: %s", profile_name, e)
    forget_profile_mapping(user_id)

    admin_msg = (
        f"🗑️ <b>微信用户已完成二次确认并注销</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>用户 ID:</b> <code>{html.escape(user_id)}</code>\n"
        f"⏰ <b>注销时间:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"已彻底删除该用户的专属 Profile、对话历史与所有记忆，并已移出白名单。"
    )
    send_telegram_notification(admin_msg)

    return True, "✅ 您的账号及相关数据已成功注销！所有个人画像、历史记忆与对话数据已彻底清空。如需再次使用，请直接发送消息重新接入。"

def reject_user_request(identifier: str, allow_user_id: bool = True) -> Tuple[bool, str]:
    """Reject or remove a user from approved or pending.

    ``allow_user_id=False`` restricts revocation to presenting a valid pairing
    code, so an unverified caller cannot revoke an existing user by ID.
    """
    init_auth_store()
    pending = _load_json(PENDING_FILE, {})
    approved_data = _load_json(APPROVED_FILE, {"approved": {}})

    identifier = identifier.strip()
    removed = False

    if identifier in pending:
        del pending[identifier]
        _save_json(PENDING_FILE, pending)
        removed = True

    if allow_user_id:
        for code, info in list(pending.items()):
            if info.get("user_id") == identifier:
                del pending[code]
                _save_json(PENDING_FILE, pending)
                removed = True

        if identifier in approved_data.get("approved", {}):
            del approved_data["approved"][identifier]
            _save_json(APPROVED_FILE, approved_data)
            removed = True

    if not removed and not allow_user_id:
        return False, f"未找到该配对码: {identifier}\n（未验证来源的调用只能使用配对码拒绝待审申请）"

    if removed:
        return True, f"已成功拒绝/移除用户或申请: {identifier}"
    return False, f"未找到相关用户或申请: {identifier}"

# ── Model configuration ──
# Model choice is an admin decision that must apply to every WeChat user at
# once. Hermes gives each profile a standalone config.yaml (load_config() reads
# the profile-scoped HERMES_HOME; there is no merge with the default profile),
# so "apply to everyone" means writing the same model config into the default
# profile and every wx_* profile.
#
# A model id alone is not enough to run: it resolves through provider entries,
# so those travel with it. Reasoning effort rides along because /reasoning is
# likewise admin-controlled.

_MODEL_TOP_LEVEL_KEYS = ("model", "fallback_providers", "custom_providers")
# Per-auxiliary-task keys that travel together.
_AUX_SYNCED_KEYS = ("model", "provider")


def _profiles_dir() -> str:
    return os.path.join(HERMES_HOME, "profiles")


def wx_profile_configs() -> List[Tuple[str, str]]:
    """(profile_name, config_path) for every wx_* profile that has a config."""
    out = []
    for d in sorted(glob.glob(os.path.join(_profiles_dir(), "wx_*"))):
        cfg = os.path.join(d, "config.yaml")
        if os.path.isfile(cfg):
            out.append((os.path.basename(d), cfg))
    return out


def all_profile_configs() -> List[Tuple[str, str]]:
    """(profile_name, config_path) for every profile, default profile excluded.

    Model choice is instance-wide, not WeChat-specific: a per-channel profile
    (telegram, discord, ...) carries its own standalone copy for the same
    reason a wx_* profile does, so "apply everywhere" has to include them.
    """
    out = []
    for d in sorted(glob.glob(os.path.join(_profiles_dir(), "*"))):
        cfg = os.path.join(d, "config.yaml")
        if os.path.isdir(d) and os.path.isfile(cfg):
            out.append((os.path.basename(d), cfg))
    return out


def _load_yaml(path: str) -> dict:
    import yaml
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _dump_yaml(path: str, data: dict) -> None:
    import yaml
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _extract_model_config(cfg: dict) -> dict:
    """The model-related subset of a Hermes config."""
    out = {k: cfg[k] for k in _MODEL_TOP_LEVEL_KEYS if k in cfg}
    aux = cfg.get("auxiliary")
    if isinstance(aux, dict):
        sub = {}
        for name, task in aux.items():
            if not isinstance(task, dict):
                continue
            # provider travels with model: an auxiliary backend pinned to a
            # different provider than the main model (e.g. routing vision to
            # openai-codex while the main model is a text-only custom model)
            # is meaningless if only the model id propagates.
            keep = {k: task[k] for k in _AUX_SYNCED_KEYS if k in task}
            if keep:
                sub[name] = keep
        if sub:
            out["auxiliary"] = sub
    for parent, key in (("delegation", "model"), ("agent", "reasoning_effort")):
        node = cfg.get(parent)
        if isinstance(node, dict) and key in node:
            out.setdefault(parent, {})[key] = node[key]
    return out


def _apply_model_config(cfg: dict, model_cfg: dict) -> List[str]:
    """Merge *model_cfg* into *cfg* in place. Returns the changed key paths."""
    import copy
    changed: List[str] = []

    for key in _MODEL_TOP_LEVEL_KEYS:
        if key in model_cfg and cfg.get(key) != model_cfg[key]:
            cfg[key] = copy.deepcopy(model_cfg[key])
            changed.append(key)

    if "auxiliary" in model_cfg:
        node = cfg.get("auxiliary")
        if not isinstance(node, dict):
            node = cfg["auxiliary"] = {}
        for name, values in model_cfg["auxiliary"].items():
            entry = node.get(name)
            if not isinstance(entry, dict):
                entry = node[name] = {}
            for key, value in values.items():
                if entry.get(key) != value:
                    entry[key] = value
                    changed.append(f"auxiliary.{name}.{key}")

    for parent in ("delegation", "agent"):
        if parent not in model_cfg:
            continue
        node = cfg.get(parent)
        if not isinstance(node, dict):
            node = cfg[parent] = {}
        for key, value in model_cfg[parent].items():
            if node.get(key) != value:
                node[key] = value
                changed.append(f"{parent}.{key}")

    return changed


def current_model() -> Optional[str]:
    """The model the default profile is configured with."""
    try:
        cfg = _load_yaml(os.path.join(HERMES_HOME, "config.yaml"))
    except Exception as e:
        logger.warning("[Weixin Auth] Could not read default config: %s", e)
        return None
    model = cfg.get("model")
    return model.get("default") if isinstance(model, dict) else None


def current_fallback_model() -> Optional[str]:
    """The model of the first entry in the default profile's fallback chain."""
    try:
        cfg = _load_yaml(os.path.join(HERMES_HOME, "config.yaml"))
    except Exception:
        return None
    chain = cfg.get("fallback_providers")
    if isinstance(chain, list) and chain and isinstance(chain[0], dict):
        return chain[0].get("model")
    return None


def set_model_everywhere(
    model_id: Optional[str] = None,
    fallback_model: Optional[str] = None,
    wx_only: bool = False,
) -> Dict[str, Any]:
    """Point every profile at the same model.

    *model_id* sets ``model.default`` and *fallback_model* sets the model of
    the first entry in the fallback chain, both on the default profile; the
    default profile's model config is then propagated to every other profile
    so a change lands everywhere at once. Only model-related keys are touched
    — notably not gateway.platforms or credentials, which a per-user profile
    must not inherit.

    Pass ``wx_only=True`` to limit propagation to the wx_* profiles.
    """
    default_cfg_path = os.path.join(HERMES_HOME, "config.yaml")
    result: Dict[str, Any] = {
        "model": None, "fallback": None,
        "updated": [], "unchanged": [], "errors": [], "backup": None,
    }

    try:
        default_cfg = _load_yaml(default_cfg_path)
    except Exception as e:
        result["errors"].append(f"读取默认配置失败: {e}")
        return result

    backup_dir = os.path.join(AUTH_DIR, f"model-sync-{time.strftime('%Y%m%d-%H%M%S')}")

    def _backup(name: str, path: str) -> None:
        os.makedirs(backup_dir, exist_ok=True)
        os.chmod(backup_dir, 0o700)
        shutil.copy2(path, os.path.join(backup_dir, f"{name}.config.yaml"))
        result["backup"] = backup_dir

    default_changed: List[str] = []

    if model_id:
        node = default_cfg.get("model")
        if not isinstance(node, dict):
            node = default_cfg["model"] = {}
        if node.get("default") != model_id:
            node["default"] = model_id
            default_changed.append("model.default")

    if fallback_model:
        chain = default_cfg.get("fallback_providers")
        if isinstance(chain, list) and chain and isinstance(chain[0], dict):
            if chain[0].get("model") != fallback_model:
                chain[0]["model"] = fallback_model
                default_changed.append("fallback_providers[0].model")
            if len(chain) > 1:
                result["errors"].append(
                    f"备用链有 {len(chain)} 项，只改了第 1 项；其余请用 hermes fallback 处理"
                )
        else:
            result["errors"].append("默认配置没有可用的 fallback_providers 链，未设置备用模型")

    if default_changed:
        try:
            _backup("default", default_cfg_path)
            _dump_yaml(default_cfg_path, default_cfg)
            result["updated"].append(("default", default_changed))
        except Exception as e:
            result["errors"].append(f"写入默认配置失败: {e}")
            return result
    elif model_id or fallback_model:
        result["unchanged"].append("default")

    model_cfg = _extract_model_config(default_cfg)
    result["model"] = (default_cfg.get("model") or {}).get("default")
    chain = default_cfg.get("fallback_providers")
    if isinstance(chain, list) and chain and isinstance(chain[0], dict):
        result["fallback"] = chain[0].get("model")

    targets = wx_profile_configs() if wx_only else all_profile_configs()
    for name, path in targets:
        try:
            cfg = _load_yaml(path)
            changed = _apply_model_config(cfg, model_cfg)
            if not changed:
                result["unchanged"].append(name)
                continue
            _backup(name, path)
            _dump_yaml(path, cfg)
            result["updated"].append((name, changed))
        except Exception as e:
            logger.error("[Weixin Auth] Failed to sync model config into %s: %s", name, e)
            result["errors"].append(f"{name}: {e}")

    logger.info(
        "[Weixin Auth] Model sync -> %s; updated=%d unchanged=%d errors=%d",
        result["model"], len(result["updated"]), len(result["unchanged"]), len(result["errors"]),
    )
    return result


CALLBACK_PREFIX = "wx:"


def is_telegram_admin(clicker_id: Optional[str]) -> bool:
    """True when *clicker_id* is a configured Telegram admin user.

    Fails closed: an unknown clicker, or no configured admin list at all, is
    not an admin.
    """
    if clicker_id is None or str(clicker_id).strip() == "":
        return False
    admins = _admin_user_ids()
    if not admins:
        return False
    return str(clicker_id).strip() in admins


def handle_telegram_callback(data: str, clicker_id: Optional[str] = None) -> Dict[str, Any]:
    """Handle a ``wx:appr:<code>`` / ``wx:deny:<code>`` inline-button click.

    All authorization and state changes live here rather than in the host's
    Telegram adapter, so the host only needs a thin dispatch that survives
    upgrades. Returns a render instruction:

        {"success": bool, "answer": <toast text>, "note": <HTML to append|None>}
    """
    if not is_telegram_admin(clicker_id):
        logger.warning(
            "[Weixin Auth] Refused approval button click from non-admin telegram user=%s",
            clicker_id,
        )
        return {"success": False, "answer": "⛔ 你无权处理微信接入申请。", "note": None}

    parts = (data or "").split(":", 2)
    if len(parts) != 3 or f"{parts[0]}:" != CALLBACK_PREFIX:
        return {"success": False, "answer": "无效的回调数据。", "note": None}

    action, code = parts[1], parts[2]
    if action == "appr":
        success, msg, _user, _acc = approve_user_request(code, approver=f"telegram:{clicker_id}")
        return {
            "success": success,
            "answer": "✅ 已批准该微信用户！" if success else f"❌ {msg[:180]}",
            "note": (
                "\n\n🟢 <b>【已批准】</b> 已自动为其开通独立专属 Profile！"
                if success else None
            ),
        }
    if action == "deny":
        success, msg = reject_user_request(code)
        return {
            "success": success,
            "answer": "❌ 已拒绝该申请！" if success else f"❌ {msg[:180]}",
            "note": (
                "\n\n🔴 <b>【已拒绝】</b> 该用户的接入申请已被拒绝。"
                if success else None
            ),
        }
    return {"success": False, "answer": f"未知操作: {action}", "note": None}


def list_auth_status() -> dict:
    """Return lists of approved users and pending requests."""
    init_auth_store()
    pending = _load_json(PENDING_FILE, {})
    approved_data = _load_json(APPROVED_FILE, {"approved": {}})
    return {
        "approved": approved_data.get("approved", {}),
        "pending": pending,
    }
