"""
Weixin Multi-Account Platform Adapter — Plugin Entry Point.

Registers the weixin_multi platform adapter under the name "weixin_multi",
separate from the built-in "weixin" (single-account) adapter.
Both can coexist in the same Hermes instance.

config.yaml::

    gateway:
      platforms:
        weixin_multi:
          enabled: true
          extra:
            dm_policy: open
            accounts:
              wechat-1:
                token: "..."
              wechat-2:
                token: "..."
"""

import html
import importlib.util
import json
import os
import sys
import uuid
import time
from pathlib import Path
from typing import Any, Optional

# Resolve the multi-weixin source directory
# weixin.py MUST be alongside adapter.py (hermes plugins install does this)
_plugin_dir = os.path.dirname(os.path.abspath(__file__))
_weixin_path = os.path.join(_plugin_dir, "weixin.py")
if os.path.exists(_weixin_path):
    _MULTI_DIR = _plugin_dir
else:
    _MULTI_DIR = _plugin_dir
    _weixin_path = None  # will be checked again in register()

if _MULTI_DIR not in sys.path:
    sys.path.insert(0, _MULTI_DIR)


def check_requirements() -> bool:
    try:
        import aiohttp  # noqa: F401
        import cryptography  # noqa: F401
        return True
    except ImportError:
        return False


def validate_config(config: Any) -> bool:
    """Validate config: True if platform is enabled (even without accounts).
    
    Accounts can be added dynamically via /wechat-login, so we don't require
    pre-configured accounts. Just check that weixin_multi is enabled.
    """
    enabled = getattr(config, "enabled", False)
    if not enabled:
        return False
    
    # Check if there are pre-configured accounts (optional)
    extra = getattr(config, "extra", {}) or {}
    accounts = extra.get("accounts", {})
    if isinstance(accounts, dict) and accounts and any(
        (a.get("token") or a.get("access_token") or "").strip()
        for a in accounts.values()
    ):
        return True
    
    # Even without pre-configured accounts, still valid — accounts
    # will be added via /wechat-login from any channel.
    return True


def _env_enablement() -> Optional[dict]:
    token = os.getenv("WEIXIN_MULTI_TOKEN") or os.getenv("WEIXIN_TOKEN")
    if not token:
        return None
    account_id = os.getenv("WEIXIN_MULTI_ACCOUNT_ID") or os.getenv("WEIXIN_ACCOUNT_ID", "default")
    base_url = os.getenv("WEIXIN_MULTI_BASE_URL") or os.getenv("WEIXIN_BASE_URL", "")
    dm_policy = os.getenv("WEIXIN_MULTI_DM_POLICY") or os.getenv("WEIXIN_DM_POLICY", "open")
    extra: dict[str, Any] = {
        "dm_policy": dm_policy,
        "accounts": {
            account_id: {
                "token": token,
                **({"base_url": base_url} if base_url else {}),
            }
        },
    }
    return extra


# ── Standalone command helpers (work from any process) ──
# These don't need the adapter instance — they use iLink API directly
# and read/write account files on disk.

ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
EP_GET_BOT_QR = "/ilink/bot/get_bot_qrcode"
EP_GET_QR_STATUS = "/ilink/bot/get_qrcode_status"
QR_TIMEOUT_MS = 5000

def _make_verified_connector(aio):
    """TCPConnector with certificate verification always on.

    This link carries the bot token, so verification is never disabled. When
    certifi is available its Mozilla CA bundle is used (some system CA stores
    cannot verify ilinkai.weixin.qq.com); otherwise aiohttp's default
    verification applies.
    """
    try:
        import ssl
        import certifi
        return aio.TCPConnector(ssl=ssl.create_default_context(cafile=certifi.where()), limit=10)
    except ImportError:
        return aio.TCPConnector(limit=10)


def _accounts_dir() -> str:
    hermes_home = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
    return os.path.join(hermes_home, "weixin", "accounts")


# Sidecar files that live in the accounts directory but are not accounts.
_ACCOUNT_SIDECAR_SUFFIXES = (".sync.json", ".context-tokens.json")
# Long-poll considered live if its sync buffer advanced within this window.
_ACCOUNT_LIVE_WINDOW_SECONDS = 300


def _is_account_file(filename: str) -> bool:
    """True for a real account file.

    Deliberately not matched by an ``wechat-`` name prefix: accounts added by
    the QR flow are named after their ``ilink_bot_id`` (e.g.
    ``b9e607bd8f41@im.bot.json``), so a prefix test hides them from the admin
    listing entirely.
    """
    if not filename.endswith(".json"):
        return False
    if filename.endswith(_ACCOUNT_SIDECAR_SUFFIXES):
        return False
    return filename not in ("pending_qr.json", "account_counter.json")


def _generate_account_id() -> str:
    """Generate next available wechat-N account ID."""
    accounts_dir = _accounts_dir()
    existing = set()
    if os.path.isdir(accounts_dir):
        for f in os.listdir(accounts_dir):
            if _is_account_file(f):
                existing.add(f[: -len(".json")])

    n = 1
    while f"wechat-{n}" in existing:
        n += 1
    return f"wechat-{n}"

def _save_account(account_id: str, token: str, base_url: str = "") -> str:
    """Save account to disk. Returns file path."""
    accounts_dir = _accounts_dir()
    os.makedirs(accounts_dir, exist_ok=True)
    
    account_data = {
        "token": token,
        "base_url": base_url or ILINK_BASE_URL,
        "cdn_base_url": CDN_BASE_URL,
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    
    # 0600 — this file holds the bot token.
    path = os.path.join(accounts_dir, f"{account_id}.json")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(account_data, f, indent=2)
    return path

def _list_accounts() -> list:
    """List all accounts from disk, with whether each is actually polling.

    A configured token says nothing about liveness — an expired iLink session
    leaves the account configured but silently not receiving anything. The
    sync-buffer file is rewritten on every successful long poll, so its mtime
    is the honest liveness signal.
    """
    accounts_dir = _accounts_dir()
    accounts = []
    if not os.path.isdir(accounts_dir):
        return accounts

    now = time.time()
    for f in sorted(os.listdir(accounts_dir)):
        if not _is_account_file(f):
            continue
        account_id = f[: -len(".json")]
        path = os.path.join(accounts_dir, f)
        try:
            with open(path) as fh:
                data = json.load(fh)
            entry = {
                "id": account_id,
                "token": data.get("token", ""),
                "base_url": data.get("base_url", ""),
            }
        except Exception:
            entry = {"id": account_id, "token": "???", "base_url": ""}

        sync_path = os.path.join(accounts_dir, f"{account_id}.sync.json")
        try:
            age = now - os.path.getmtime(sync_path)
            entry["live"] = age < _ACCOUNT_LIVE_WINDOW_SECONDS
            entry["last_poll_age"] = int(age)
        except OSError:
            entry["live"] = False
            entry["last_poll_age"] = None
        accounts.append(entry)
    return accounts


def _format_poll_age(seconds: Optional[int]) -> str:
    if seconds is None:
        return "从未"
    if seconds < 120:
        return f"{seconds} 秒前"
    if seconds < 7200:
        return f"{seconds // 60} 分钟前"
    return f"{seconds // 3600} 小时前"

# ── Pending QR management ──
# When /wechat-login is called from WebUI, the QR data is saved to disk.
# The gateway process polls this file and completes the login when confirmed.

def _pending_qr_file() -> str:
    hermes_home = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
    return os.path.join(hermes_home, "weixin", "pending_qr.json")

def _save_pending_qr(qrcode_value: str, qr_link: str) -> None:
    """Save pending QR for gateway to poll."""
    pending_file = _pending_qr_file()
    os.makedirs(os.path.dirname(pending_file), exist_ok=True)
    with open(pending_file, "w") as f:
        json.dump({
            "qrcode": qrcode_value,
            "link": qr_link,
            "created_at": time.time(),
        }, f)

def _load_pending_qr() -> Optional[dict]:
    """Load pending QR data. Returns None if none or expired."""
    pending_file = _pending_qr_file()
    if not os.path.exists(pending_file):
        return None
    try:
        with open(pending_file) as f:
            data = json.load(f)
        # Expire after 5 minutes
        if time.time() - data.get("created_at", 0) > 300:
            os.remove(pending_file)
            return None
        return data
    except Exception:
        return None

def _clear_pending_qr() -> None:
    """Remove pending QR file after successful login."""
    pending_file = _pending_qr_file()
    if os.path.exists(pending_file):
        os.remove(pending_file)


# ── Admin authorization ──
# Every command below is a management command and is Telegram-only. The host
# registers commands globally (WebUI / CLI / Telegram / WeChat all reach the
# same handler), so each handler must gate itself.

_ADMIN_DISABLED_MSG = "❌ 授权模块不可用，微信管理命令已禁用。"


def _auth_manager():
    """Import auth_manager, or None when it is not installed alongside us."""
    try:
        import auth_manager  # type: ignore
        return auth_manager
    except ImportError:
        try:
            from . import auth_manager  # type: ignore
            return auth_manager
        except ImportError:
            return None


def _guard(*args, **kwargs):
    """Authorize a management command.

    Returns ``(auth_manager, verdict, denial_message)``. When *denial_message*
    is non-empty the caller must return it unchanged and do nothing else.
    Fails closed: no auth_manager means no management commands.
    """
    am = _auth_manager()
    if am is None:
        return None, None, _ADMIN_DISABLED_MSG
    verdict, message = am.authorize_admin(*args, **kwargs)
    if verdict == am.ADMIN_DENIED:
        return am, verdict, message or am.ADMIN_ONLY_HINT
    return am, verdict, ""


def register(ctx):
    """
    Plugin entry point — called by Hermes plugin system.

    Dynamically imports WeixinMultiAdapter from /opt/hermes-weixin-multi/weixin.py
    and registers it as the "weixin_multi" platform adapter.

    Also registers /wechat-login and /wechat-list as GLOBAL slash commands
    (available from any channel — WebUI, Telegram, etc.).
    """
    weixin_path = os.path.join(_MULTI_DIR, "weixin.py")
    if not os.path.exists(weixin_path):
        raise FileNotFoundError(
            f"weixin_multi: weixin.py not found at {weixin_path}. "
            f"Plugin installation incomplete. Re-run: hermes plugins install"
        )

    # Dynamic import of the weixin module
    spec = importlib.util.spec_from_file_location("weixin_multi_impl", weixin_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["weixin_multi_impl"] = mod
    spec.loader.exec_module(mod)

    WeixinMultiAdapter = mod.WeixinMultiAdapter

    # ── Fix platform identity: set self.platform after __init__ ──
    from gateway.config import Platform

    _orig_init = WeixinMultiAdapter.__init__

    def _patched_init(self, config, **kwargs):
        _orig_init(self, config, **kwargs)
        self.platform = Platform("weixin_multi")

    WeixinMultiAdapter.__init__ = _patched_init

    # Register with the platform registry
    ctx.register_platform(
        name="weixin_multi",
        label="Weixin Multi",
        adapter_factory=lambda cfg: WeixinMultiAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        required_env=[],
        install_hint="pip install aiohttp cryptography",
        env_enablement_fn=_env_enablement,
    )

    # ── Register global slash commands ──
    # These work from ANY channel (WebUI, Telegram, etc.) and from ANY
    # process (gateway or WebUI) — they use iLink API directly and
    # read/write account files on disk, no adapter instance needed.

    async def _handle_wechat_login_cmd(raw_args: str = "", *args, **kwargs) -> str:
        """Global /wechat-login: generate QR and wait for scan — Telegram admin only.

        Whoever scans the QR gets a bot account attached to this Hermes
        instance, so the QR is only ever delivered to the Telegram admin chat.
        A caller we cannot prove to be the Telegram admin gets an
        acknowledgement and nothing else.
        Saves token to ~/.hermes/weixin/accounts/<id>.json on success.
        """
        am, verdict, denial = _guard(raw_args, *args, **kwargs)
        if denial:
            return denial
        if verdict == am.ADMIN_UNVERIFIED and not am.throttle("wechat-login", 60.0):
            return "⏳ 请求过于频繁，请稍后再试（二维码已发送至 Telegram 管理员会话）。"

        try:
            import aiohttp as aio
        except ImportError:
            return "❌ aiohttp 未安装。请运行: pip install aiohttp"

        try:
            async with aio.ClientSession(
                trust_env=True, connector=_make_verified_connector(aio)
            ) as session:
                # Step 1: Get QR code
                url = f"{ILINK_BASE_URL}{EP_GET_BOT_QR}?bot_type=3"
                timeout = aio.ClientTimeout(total=QR_TIMEOUT_MS / 1000)
                async with session.get(url, timeout=timeout) as resp:
                    qr_resp = await resp.json(content_type=None)

                qrcode_value = str(qr_resp.get("qrcode") or "")
                qrcode_url = str(qr_resp.get("qrcode_img_content") or "")

                if not qrcode_value:
                    return "❌ 获取二维码失败：服务端无响应"

                qr_link = qrcode_url or qrcode_value

                # Store pending QR for gateway to poll
                _save_pending_qr(qrcode_value, qr_link)

                body = (
                    f"📱 微信扫码登录\n\n"
                    f"请用微信扫描：\n"
                    f"{qr_link}\n\n"
                    f"⏳ 二维码5分钟内有效\n\n"
                    f"扫码后手机上点「确认」即可完成登录。\n"
                    f"用 /wechat-list 查看账号状态。"
                )
                # The QR is a credential — deliver it to the Telegram admin chat.
                am.send_telegram_notification(
                    "📱 <b>微信扫码登录</b>\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    f"请用微信扫描：\n<code>{html.escape(qr_link)}</code>\n\n"
                    "⏳ 二维码 5 分钟内有效，扫码后在手机上点「确认」即可完成登录。"
                )
                if verdict == am.ADMIN_OK:
                    return body
                return (
                    "✅ 二维码已发送至 Telegram 管理员会话。\n"
                    "请在 Telegram 中查看并扫码完成登录。"
                )
        except Exception as e:
            return f"❌ 获取二维码失败: {e}"

    def _handle_wechat_list_cmd(raw_args: str = "", *args, **kwargs) -> str:
        """Global /wechat-list: show all accounts and status — Telegram admin only.

        Works from any process — reads account files from disk.
        """
        am, verdict, denial = _guard(raw_args, *args, **kwargs)
        if denial:
            return denial

        accounts = _list_accounts()
        if not accounts:
            body = "📱 暂无微信账号。在 Telegram 发送 /wechat-login 添加第一个账号。"
        else:
            lines = ["📱 Weixin Multi 账号列表：\n"]
            for acc in accounts:
                has_token = bool(acc.get("token")) and acc["token"] != "???"
                if not has_token:
                    lines.append(f"  ❌ {acc['id']} — 缺少 token")
                elif acc.get("live"):
                    lines.append(f"  ✅ {acc['id']} — 在线（{_format_poll_age(acc.get('last_poll_age'))}轮询）")
                elif acc.get("last_poll_age") is None:
                    lines.append(f"  ⚠️ {acc['id']} — 已掉线（从未成功轮询）")
                else:
                    lines.append(
                        f"  ⚠️ {acc['id']} — 已掉线（最后轮询 {_format_poll_age(acc['last_poll_age'])}）"
                    )
            dead = sum(1 for a in accounts if not a.get("live"))
            lines.append(f"\n共 {len(accounts)} 个账号，{len(accounts) - dead} 个在线")
            if dead:
                lines.append("⚠️ 掉线账号通常是 iLink 会话过期，需重新 /wechat-login 扫码恢复。")
            lines.append("在 Telegram 发送 /wechat-login 添加新账号")
            body = "\n".join(lines)

        if verdict == am.ADMIN_OK:
            return body
        if am.throttle("wechat-list", 30.0):
            am.send_telegram_notification(f"<pre>{html.escape(body)}</pre>")
        return "✅ 账号列表已发送至 Telegram 管理员会话。"

    ctx.register_command(
        name="wechat-login",
        handler=_handle_wechat_login_cmd,
        description="添加新微信账号（扫码登录）",
    )
    ctx.register_command(
        name="wechat-list",
        handler=_handle_wechat_list_cmd,
        description="查看所有微信账号状态",
    )

    # ── Telegram User Approval Commands ──
    async def _handle_approve_wechat_cmd(raw_args: str = "", *args, **kwargs) -> str:
        am, verdict, denial = _guard(raw_args, *args, **kwargs)
        if denial:
            return denial
        arg = (raw_args or "").strip()
        if not arg:
            return "❌ 请输入申请配对码。\n例如：/approve_wechat a3f9c1d2"
        # Unverified callers must present the pairing code, which only ever
        # reaches the Telegram admin chat — approval stays a Telegram capability.
        success, msg, target_user, account_id = am.approve_user_request(
            arg, allow_user_id=(verdict == am.ADMIN_OK)
        )
        if success and verdict != am.ADMIN_OK:
            am.send_telegram_notification(f"✅ {html.escape(msg)}")
        return msg

    def _handle_wechat_users_cmd(raw_args: str = "", *args, **kwargs) -> str:
        am, verdict, denial = _guard(raw_args, *args, **kwargs)
        if denial:
            return denial

        status = am.list_auth_status()
        approved = status.get("approved", {})
        pending = status.get("pending", {})

        lines = ["👥 微信用户授权状态：\n"]
        lines.append(f"【已批准用户 ({len(approved)})】")
        for u, meta in approved.items():
            lines.append(f"  ✅ {u}")

        lines.append(f"\n【待审批申请 ({len(pending)})】")
        for code, info in pending.items():
            lines.append(f"  ⏳ 配对码: {code} | 用户: {info.get('user_id')}")

        if pending:
            lines.append("\n👉 输入 /approve_wechat <配对码> 即可批准。")
        body = "\n".join(lines)

        if verdict == am.ADMIN_OK:
            return body
        # User IDs and pairing codes are secrets — never echo them to an
        # unverified caller; push the listing to the Telegram admin instead.
        if am.throttle("wechat-users", 30.0):
            am.send_telegram_notification(f"<pre>{html.escape(body)}</pre>")
        return "✅ 授权状态已发送至 Telegram 管理员会话。"

    def _handle_wechat_model_cmd(raw_args: str = "", *args, **kwargs) -> str:
        """Show or set the model used by every WeChat user — Telegram admin only.

        WeChat users can read the current model with /status but cannot change
        it; this is the only way it moves, and it moves for everyone at once.
        """
        am, verdict, denial = _guard(raw_args, *args, **kwargs)
        if denial:
            return denial

        arg = (raw_args or "").strip()
        if not arg:
            current = am.current_model()
            lines = [f"🧠 主模型：{current or '(未配置)'}"]
            lines.append(f"🔁 备用模型：{am.current_fallback_model() or '(未配置)'}")
            managed = am.managed_config_path()
            if managed and os.path.isfile(managed):
                lines.append(f"📌 由 managed 层强制：{managed}（覆盖以下各 profile 的自有配置）")
            lines.append("")
            for name, path in am.all_profile_configs():
                try:
                    cfg = am._load_yaml(path)
                    m = (cfg.get("model") or {}).get("default")
                except Exception:
                    m = "(读取失败)"
                flag = "✅" if m == current else "⚠️"
                lines.append(f"  {flag} {name} — {m}")
            lines.append(
                "\n用法：\n"
                "  /wechat-model <主模型ID>\n"
                "  /wechat-model <主模型ID> <备用模型ID>\n"
                "改动对所有 profile 生效。"
            )
            body = "\n".join(lines)
            if verdict == am.ADMIN_OK:
                return body
            if am.throttle("wechat-model-show", 30.0):
                am.send_telegram_notification(f"<pre>{html.escape(body)}</pre>")
            return "✅ 模型状态已发送至 Telegram 管理员会话。"

        parts = arg.split()
        primary = parts[0]
        fallback = parts[1] if len(parts) > 1 else None
        result = am.set_model_everywhere(primary, fallback_model=fallback)
        if result["errors"] and not result["updated"]:
            return "❌ 切换失败：\n" + "\n".join(result["errors"][:5])

        lines = [f"🧠 主模型 → {result['model']}"]
        if fallback:
            lines.append(f"🔁 备用模型 → {result['fallback']}")
        if result.get("managed"):
            lines.append("\n📌 已写入 managed 层，对所有 profile 强制生效（覆盖各自的 config.yaml）。")
        if result["updated"]:
            lines.append(f"\n已更新 {len(result['updated'])} 个 profile：")
            for name, changed in result["updated"]:
                lines.append(f"  • {name} — {', '.join(changed[:4])}{' …' if len(changed) > 4 else ''}")
        if result["unchanged"]:
            lines.append(f"\n{len(result['unchanged'])} 个已是该配置，无需改动。")
        if result["errors"]:
            lines.append("\n⚠️ 部分失败：\n" + "\n".join(result["errors"][:5]))
        if result["backup"]:
            lines.append(f"\n备份：{result['backup']}")
        lines.append("\n用户下一轮对话生效；已有会话若曾被 /model 临时覆盖，需 /new 开新会话。")
        body = "\n".join(lines)

        # State change — always leave an audit trail in the admin chat.
        if verdict != am.ADMIN_OK:
            am.send_telegram_notification(f"🧠 <b>模型已统一切换</b>\n<pre>{html.escape(body)}</pre>")
            return "✅ 已切换，详情已发送至 Telegram 管理员会话。"
        return body

    def _handle_reject_wechat_cmd(raw_args: str = "", *args, **kwargs) -> str:
        am, verdict, denial = _guard(raw_args, *args, **kwargs)
        if denial:
            return denial
        arg = (raw_args or "").strip()
        if not arg:
            return "❌ 请输入申请配对码或微信用户 ID。\n例如：/reject_wechat a3f9c1d2"
        success, msg = am.reject_user_request(arg, allow_user_id=(verdict == am.ADMIN_OK))
        if success and verdict != am.ADMIN_OK:
            am.send_telegram_notification(f"🚫 {html.escape(msg)}")
        return msg

    ctx.register_command(
        name="approve_wechat",
        handler=_handle_approve_wechat_cmd,
        description="批准微信新用户配对申请 (/approve_wechat <申请码|用户ID>)",
    )
    ctx.register_command(
        name="approve-wechat",
        handler=_handle_approve_wechat_cmd,
        description="批准微信新用户配对申请",
    )
    ctx.register_command(
        name="wechat-users",
        handler=_handle_wechat_users_cmd,
        description="查看微信已批准用户与待审批列表",
    )
    ctx.register_command(
        name="wechat_users",
        handler=_handle_wechat_users_cmd,
        description="查看微信已批准用户与待审批列表",
    )
    ctx.register_command(
        name="wechat-model",
        handler=_handle_wechat_model_cmd,
        description="查看/统一切换所有微信用户的模型 (/wechat-model [模型ID])",
    )
    ctx.register_command(
        name="wechat_model",
        handler=_handle_wechat_model_cmd,
        description="查看/统一切换所有微信用户的模型",
    )
    ctx.register_command(
        name="reject_wechat",
        handler=_handle_reject_wechat_cmd,
        description="拒绝或移除微信用户授权 (/reject_wechat <申请码|用户ID>)",
    )
    ctx.register_command(
        name="reject-wechat",
        handler=_handle_reject_wechat_cmd,
        description="拒绝或移除微信用户授权",
    )

    # ── Register as agent tools (works in WebUI/Desktop where commands bypass gateway) ──
    ctx.register_tool(
        name="wechat_login",
        toolset="hermes-cli",
        schema={
            "description": "添加新的微信账号到 Hermes（扫码登录）。仅限管理员在 Telegram 渠道使用；"
                           "从其他渠道调用时，二维码只会发送到 Telegram 管理员会话。",
            "parameters": {"type": "object", "properties": {}},
        },
        handler=_handle_wechat_login_cmd,
        is_async=True,
        description="添加新微信账号（扫码登录）",
    )
    ctx.register_tool(
        name="wechat_list",
        toolset="hermes-cli",
        schema={
            "description": "查看所有已连接的微信账号及其状态。仅限管理员在 Telegram 渠道使用；"
                           "从其他渠道调用时，结果只会发送到 Telegram 管理员会话。",
            "parameters": {"type": "object", "properties": {}},
        },
        handler=_handle_wechat_list_cmd,
        is_async=False,
        description="查看微信账号列表",
    )
