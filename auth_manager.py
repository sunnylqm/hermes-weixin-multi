import glob
import html
import json
import logging
import os
import random
import re
import shutil
import time
import uuid
from datetime import datetime
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

HERMES_HOME = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
AUTH_DIR = os.path.join(HERMES_HOME, "weixin")
APPROVED_FILE = os.path.join(AUTH_DIR, "approved_users.json")
PENDING_FILE = os.path.join(AUTH_DIR, "pending_requests.json")

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

# Default admin IDs that are pre-approved
DEFAULT_APPROVED = [
    "o9cq8007gAIIIlHnC3Gh-DiCK9Hs@im.wechat",
]

def _get_telegram_config() -> Tuple[Optional[str], Optional[str]]:
    """Retrieve Telegram Bot Token and Admin Chat ID from env files."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_HOME_CHANNEL") or os.getenv("TELEGRAM_ALLOWED_USERS")
    
    if not token or not chat_id:
        for p in [
            os.path.join(HERMES_HOME, "profiles", "telegram", ".env"),
            os.path.join(HERMES_HOME, ".env"),
        ]:
            if os.path.exists(p):
                with open(p) as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("TELEGRAM_BOT_TOKEN=") and not token:
                            token = line.split("=", 1)[1].strip()
                        if line.startswith("TELEGRAM_HOME_CHANNEL=") and not chat_id:
                            chat_id = line.split("=", 1)[1].strip()
                        if line.startswith("TELEGRAM_ALLOWED_USERS=") and not chat_id:
                            chat_id = line.split("=", 1)[1].strip()
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
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("Failed to save JSON to %s: %s", file_path, e)

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

def send_wechat_message(user_id: str, account_id: Optional[str] = None, text: str = APPROVED_COMMANDS_TEXT) -> bool:
    """Proactively send a message to a WeChat user with robust account discovery."""
    try:
        import requests
        accounts_dir = os.path.join(HERMES_HOME, "weixin", "accounts")
        if not os.path.exists(accounts_dir):
            return False

        account_files = []
        if account_id and os.path.exists(os.path.join(accounts_dir, f"{account_id}.json")):
            account_files.append(os.path.join(accounts_dir, f"{account_id}.json"))

        for c in sorted(glob.glob(os.path.join(accounts_dir, "*.json"))):
            if not c.endswith(".sync.json") and not c.endswith(".context-tokens.json") and not c.endswith("pending_qr.json"):
                if c not in account_files:
                    account_files.append(c)

        for account_file in account_files:
            try:
                with open(account_file, "r", encoding="utf-8") as f:
                    acc = json.load(f)
                token = acc.get("token")
                base_url = acc.get("base_url", "https://ilinkai.weixin.qq.com").rstrip("/")
                if not token or token == "???":
                    continue

                headers = {
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {token}",
                    "X-WECHAT-UIN": "0",
                }

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
                resp = requests.post(url, json=payload, headers=headers, timeout=10)
                data = resp.json() if resp.status_code == 200 else {}
                if resp.status_code == 200 and data.get("ret") in (0, None):
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

    code = f"{random.randint(100000, 999999)}"
    while code in pending:
        code = f"{random.randint(100000, 999999)}"

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

def approve_user_request(identifier: str, approver: str = "telegram_admin") -> Tuple[bool, str, Optional[str], Optional[str]]:
    """
    Approve a user by pairing code OR user_id, creating profile and pushing approved command guide.
    Returns (success, message, user_id, account_id).
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
    else:
        for code, info in pending.items():
            if info.get("user_id") == identifier or info.get("user_id", "").startswith(identifier):
                target_code = code
                target_user_id = info.get("user_id")
                account_id = info.get("account_id")
                break
        if not target_user_id and "@im.wechat" in identifier:
            target_user_id = identifier

    if not target_user_id:
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

    from hermes_cli.profiles import create_profile, profile_exists
    raw_id = target_user_id.split("@")[0] if "@" in target_user_id else target_user_id
    clean_id = re.sub(r"[^a-zA-Z0-9_]", "_", raw_id).lower()[:26]
    profile_name = f"wx_{clean_id}"

    try:
        if not profile_exists(profile_name):
            pdir = create_profile(profile_name, clone_from="default", clone_config=True, no_alias=True)
            cfg_path = os.path.join(pdir, "config.yaml")
            if os.path.exists(cfg_path):
                try:
                    import yaml
                    with open(cfg_path) as f:
                        c = yaml.safe_load(f) or {}
                    if "gateway" in c and isinstance(c["gateway"], dict):
                        c["gateway"]["platforms"] = {}
                    with open(cfg_path, "w") as f:
                        yaml.safe_dump(c, f, default_flow_style=False, allow_unicode=True)
                except Exception:
                    pass
            mem_dir = os.path.join(pdir, "memories")
            os.makedirs(mem_dir, exist_ok=True)
            with open(os.path.join(mem_dir, "USER.md"), "w") as uf:
                uf.write("_Learn about the person you're helping. Update this as you go.\n§\n**Name:**\n§\n**What to call them:**\n§\n**Pronouns:** _(optional)_\n§\n**Timezone:**\n§\n**Notes:**\n")
            with open(os.path.join(mem_dir, "MEMORY.md"), "w") as mf:
                mf.write("")
    except Exception as e:
        logger.error("[Weixin Auth] Error creating profile for approved user: %s", e)

    # Proactively push approval notice and command usage guide
    send_wechat_message(target_user_id, account_id=account_id, text=APPROVED_COMMANDS_TEXT)

    return True, f"已成功批准微信用户 {target_user_id}，并已创建专属独立 Profile [{profile_name}]，已向用户推送指令指南！", target_user_id, account_id

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

    raw_id = user_id.split("@")[0] if "@" in user_id else user_id
    clean_id = re.sub(r"[^a-zA-Z0-9_]", "_", raw_id).lower()[:26]
    profile_name = f"wx_{clean_id}"

    try:
        pdir = os.path.join(HERMES_HOME, "profiles", profile_name)
        if os.path.exists(pdir):
            shutil.rmtree(pdir)
            logger.info("[Weixin Auth] Purged profile directory %s for unregistering user %s", pdir, user_id)
    except Exception as e:
        logger.error("[Weixin Auth] Failed to delete profile dir %s: %s", profile_name, e)

    admin_msg = (
        f"🗑️ <b>微信用户已主动注销</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>用户 ID:</b> <code>{html.escape(user_id)}</code>\n"
        f"⏰ <b>注销时间:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"已彻底删除该用户的专属 Profile、对话历史与所有记忆，并已移出白名单。"
    )
    send_telegram_notification(admin_msg)

    return True, "✅ 您的账号及相关数据已成功注销！所有个人画像、历史记忆与对话数据已彻底清空。如需再次使用，请直接发送消息重新接入。"

def reject_user_request(identifier: str) -> Tuple[bool, str]:
    """Reject or remove a user from approved or pending."""
    init_auth_store()
    pending = _load_json(PENDING_FILE, {})
    approved_data = _load_json(APPROVED_FILE, {"approved": {}})

    identifier = identifier.strip()
    removed = False

    if identifier in pending:
        del pending[identifier]
        _save_json(PENDING_FILE, pending)
        removed = True

    for code, info in list(pending.items()):
        if info.get("user_id") == identifier:
            del pending[code]
            _save_json(PENDING_FILE, pending)
            removed = True

    if identifier in approved_data.get("approved", {}):
        del approved_data["approved"][identifier]
        _save_json(APPROVED_FILE, approved_data)
        removed = True

    if removed:
        return True, f"已成功拒绝/移除用户或申请: {identifier}"
    return False, f"未找到相关用户或申请: {identifier}"

def list_auth_status() -> dict:
    """Return lists of approved users and pending requests."""
    init_auth_store()
    pending = _load_json(PENDING_FILE, {})
    approved_data = _load_json(APPROVED_FILE, {"approved": {}})
    return {
        "approved": approved_data.get("approved", {}),
        "pending": pending,
    }
