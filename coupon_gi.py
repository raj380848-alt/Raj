"""
================================================================================
 REFERRAL COUPON BOT — Firebase Edition
================================================================================
Setup:
  1. pip install python-telegram-bot firebase-admin
  2. Create a Firebase project -> Firestore Database -> generate a service
     account key (Project Settings > Service Accounts > Generate new private
     key).
  3. Set environment variables before running:
       BOT_TOKEN                - your Telegram bot token from @BotFather
       FIREBASE_CREDENTIALS_JSON - the ENTIRE contents of serviceAccountKey.json,
                                    pasted as one env var value (or its base64
                                    encoding — recommended on hosts like Railway,
                                    since their UI can mangle the multi-line
                                    private key in raw JSON). This is the
                                    preferred way to run on Railway/Heroku/etc.,
                                    since those platforms don't give you a
                                    persistent place to keep a credentials file.
       FIREBASE_CRED_PATH        - (optional) path to a serviceAccountKey.json
                                    file on disk. Only used as a fallback if
                                    FIREBASE_CREDENTIALS_JSON is not set — handy
                                    for local development. Defaults to
                                    "serviceAccountKey.json".
  4. Edit ADMIN_IDS and REQUIRED_CHANNELS below.
  5. Run: python coupon_bot.py

  --- Railway deployment note ---
  In your Railway service, open Variables and add FIREBASE_CREDENTIALS_JSON.
  Easiest/safest way to set it:
    base64 -i serviceAccountKey.json | pbcopy   # macOS, copies to clipboard
    base64 -w0 serviceAccountKey.json           # Linux, prints to stdout
  then paste that single-line base64 string as the variable's value. The bot
  detects and decodes base64 automatically; pasting the raw JSON also works,
  as long as newlines inside the private key survive the paste.

Firestore layout:
  users/{user_id}:
      name, username, referred_by, total_referrals, is_verified,
      is_banned, claims: {slot_1: bool, slot_2: bool}, created_at
  slots/slot_1, slots/slot_2:
      name (display name), required_refers, stock: [code, code, ...]
================================================================================
"""

import os
import time
import json
import base64
import html
import logging
import asyncio
from urllib.parse import quote
from datetime import datetime, timezone

import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1 import aggregation as firestore_aggregation

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ChatMemberHandler,
    filters,
    ContextTypes,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==================== CONFIGURATION ====================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")  # never hardcode this — set as an env var

ADMIN_IDS = [
    8084694525,
    1713020163,
    8012586357,
]

REQUIRED_CHANNELS = [
    {"name": "EXPILOT", "username": "@rajexpilot", "link": "https://t.me/rajexpilot"},
    {"name": "LOOT FACTORY", "username": "@LootFactoryX0", "link": "https://t.me/LootFactoryX0"},
    {"name": "SHEIN UPDATE", "username": "@rajuking54", "link": "https://t.me/rajuking54"},
    {"name": "TITAN LOOT", "username": "@titan_loot", "link": "https://t.me/titan_loot"},
    {"name": "LOOT JUNCTION GC", "username": "@lootjunctiongc", "link": "https://t.me/lootjunctiongc"},
]

SUPPORT_URL = "https://t.me/YourSupportUsername"  # edit this to your real support contact/channel

DEFAULT_SLOT_KEYS = ["slot_1", "slot_2"]
DEFAULT_SLOT_LABELS = {"slot_1": "Slot 1", "slot_2": "Slot 2"}

# ==================== FIREBASE INIT ====================
FIREBASE_CREDENTIALS_JSON = os.environ.get("FIREBASE_CREDENTIALS_JSON", "")
FIREBASE_CRED_PATH = os.environ.get("FIREBASE_CRED_PATH", "serviceAccountKey.json")


def _load_firebase_credentials() -> credentials.Certificate:
    """Load Firebase creds from an env var (preferred, e.g. on Railway) or a file (local dev)."""
    if FIREBASE_CREDENTIALS_JSON:
        raw = FIREBASE_CREDENTIALS_JSON.strip()
        try:
            cred_dict = json.loads(raw)
        except json.JSONDecodeError:
            # Not raw JSON — try treating it as base64-encoded JSON instead.
            try:
                decoded = base64.b64decode(raw).decode("utf-8")
                cred_dict = json.loads(decoded)
            except Exception as exc:
                raise SystemExit(
                    "FIREBASE_CREDENTIALS_JSON is set but is neither valid JSON nor "
                    "valid base64-encoded JSON. Re-copy the service account key "
                    "(or its base64 encoding) into that env var."
                ) from exc
        return credentials.Certificate(cred_dict)

    if os.path.exists(FIREBASE_CRED_PATH):
        return credentials.Certificate(FIREBASE_CRED_PATH)

    raise SystemExit(
        "No Firebase credentials found. Set the FIREBASE_CREDENTIALS_JSON env var "
        "(the service account JSON, or its base64 encoding — see the setup notes "
        "at the top of this file) or provide FIREBASE_CRED_PATH pointing to a "
        "serviceAccountKey.json file on disk."
    )


cred = _load_firebase_credentials()
firebase_admin.initialize_app(cred)
db = firestore.client()

USERS = db.collection("users")
SLOTS = db.collection("slots")
HISTORY = db.collection("history")
CONFIG = db.collection("config").document("general")


def _ensure_slots_exist():
    for key in DEFAULT_SLOT_KEYS:
        ref = SLOTS.document(key)
        if not ref.get().exists:
            ref.set(
                {
                    "name": DEFAULT_SLOT_LABELS[key],
                    "required_refers": 5,
                    "stock": [],
                }
            )


def _ensure_config_exists():
    if not CONFIG.get().exists:
        CONFIG.set({"coupon_name": "Coupon", "slot_keys": list(DEFAULT_SLOT_KEYS)})
    elif "slot_keys" not in (CONFIG.get().to_dict() or {}):
        CONFIG.set({"slot_keys": list(DEFAULT_SLOT_KEYS)}, merge=True)


_ensure_slots_exist()
_ensure_config_exists()


# ==================== LIGHTWEIGHT READ CACHE ====================
_CONFIG_TTL = 30
_SLOT_TTL = 15
_config_cache: dict = {"data": None, "ts": 0.0}
_slot_cache: dict = {}


def _get_config_cached(force: bool = False) -> dict:
    now = time.monotonic()
    if force or _config_cache["data"] is None or (now - _config_cache["ts"]) > _CONFIG_TTL:
        _config_cache["data"] = CONFIG.get().to_dict() or {}
        _config_cache["ts"] = now
    return _config_cache["data"]


def _invalidate_config_cache():
    _config_cache["data"] = None


def _get_slot_cached(key: str, force: bool = False) -> dict:
    now = time.monotonic()
    entry = _slot_cache.get(key)
    if force or entry is None or (now - entry[1]) > _SLOT_TTL:
        data = SLOTS.document(key).get().to_dict() or {}
        _slot_cache[key] = (data, now)
        return data
    return entry[0]


def _invalidate_slot_cache(key: str | None = None):
    if key is None:
        _slot_cache.clear()
    else:
        _slot_cache.pop(key, None)

# ==================== ASYNC FIRESTORE HELPERS ====================
async def run_sync(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)


async def get_user(user_id: int) -> dict | None:
    def _get():
        snap = USERS.document(str(user_id)).get()
        return snap.to_dict() if snap.exists else None

    return await run_sync(_get)


async def get_or_create_user(user_id: int, name: str, username: str, referred_by: int | None) -> dict:
    def _run():
        ref = USERS.document(str(user_id))
        snap = ref.get()
        if snap.exists:
            return snap.to_dict()
        data = {
            "name": name,
            "username": username or "",
            "referred_by": referred_by,
            "total_referrals": 0,
            "is_verified": False,
            "referral_credited": False,
            "is_banned": False,
            "claims": {"slot_1": False, "slot_2": False},
            "created_at": datetime.now(timezone.utc),
        }
        ref.set(data)
        return data

    return await run_sync(_run)


async def mark_verified_and_reward_inviter(user_id: int) -> None:
    def _run():
        user_ref = USERS.document(str(user_id))
        snap = user_ref.get()
        data = snap.to_dict() or {}
        updates = {}
        if not data.get("is_verified"):
            updates["is_verified"] = True
        if not data.get("referral_credited") and data.get("referred_by"):
            inviter_id = data["referred_by"]
            inviter_ref = USERS.document(str(inviter_id))
            if inviter_ref.get().exists:
                inviter_ref.update({"total_referrals": firestore.Increment(1)})
            updates["referral_credited"] = True
        if updates:
            user_ref.update(updates)

    await run_sync(_run)


async def revoke_access(user_id: int) -> None:
    def _run():
        ref = USERS.document(str(user_id))
        snap = ref.get()
        if not snap.exists:
            return
        data = snap.to_dict() or {}
        if not data.get("is_verified"):
            return
        updates = {"is_verified": False}
        if "referral_credited" not in data:
            updates["referral_credited"] = True
        ref.update(updates)

    await run_sync(_run)


async def get_leaderboard(limit: int = 10):
    def _get():
        query = USERS.order_by("total_referrals", direction=firestore.Query.DESCENDING).limit(limit)
        return [(doc.id, doc.to_dict()) for doc in query.stream()]

    return await run_sync(_get)


async def get_user_rank(user_id: int, referral_count: int | None = None) -> int:
    def _get():
        my_refs = referral_count
        if my_refs is None:
            me = USERS.document(str(user_id)).get().to_dict() or {}
            my_refs = me.get("total_referrals", 0)
        query = USERS.where("total_referrals", ">", my_refs)
        agg_query = firestore_aggregation.AggregationQuery(query)
        agg_query.count(alias="higher")
        results = agg_query.get()
        higher = results[0][0].value if results and results[0] else 0
        return int(higher) + 1

    return await run_sync(_get)


async def set_ban(user_id: int, banned: bool) -> bool:
    def _run():
        ref = USERS.document(str(user_id))
        if not ref.get().exists:
            return False
        ref.update({"is_banned": banned})
        return True

    return await run_sync(_run)


async def add_referrals_to_user(user_id: int, amount: int) -> bool:
    def _run():
        ref = USERS.document(str(user_id))
        if not ref.get().exists:
            return False
        ref.update({"total_referrals": firestore.Increment(amount)})
        return True

    return await run_sync(_run)


async def get_slot(slot_key: str) -> dict:
    return await run_sync(_get_slot_cached, slot_key)


async def rename_slot(slot_key: str, new_name: str):
    def _run():
        SLOTS.document(slot_key).update({"name": new_name})
        _invalidate_slot_cache(slot_key)

    await run_sync(_run)


async def set_slot_required(slot_key: str, required: int):
    def _run():
        SLOTS.document(slot_key).update({"required_refers": required})
        _invalidate_slot_cache(slot_key)

    await run_sync(_run)


async def add_stock(slot_key: str, items: list[str]) -> int:
    def _run():
        SLOTS.document(slot_key).update({"stock": firestore.ArrayUnion(items)})
        _invalidate_slot_cache(slot_key)
        return len(items)

    return await run_sync(_run)


async def remove_all_stock(slot_key: str) -> int:
    def _run():
        ref = SLOTS.document(slot_key)
        data = ref.get().to_dict() or {}
        count = len(data.get("stock", []))
        ref.update({"stock": []})
        _invalidate_slot_cache(slot_key)
        return count

    return await run_sync(_run)


async def get_total_stock() -> int:
    def _get():
        config_data = _get_config_cached()
        keys = config_data.get("slot_keys", list(DEFAULT_SLOT_KEYS))
        total = 0
        for key in keys:
            data = _get_slot_cached(key)
            total += len(data.get("stock", []))
        return total

    return await run_sync(_get)


async def redeem_slot(user_id: int, slot_key: str):
    @firestore.transactional
    def _txn(transaction):
        user_ref = USERS.document(str(user_id))
        slot_ref = SLOTS.document(slot_key)
        user_snap = user_ref.get(transaction=transaction)
        slot_snap = slot_ref.get(transaction=transaction)
        user_data = user_snap.to_dict() or {}
        slot_data = slot_snap.to_dict() or {}

        claims = user_data.get("claims", {})
        if claims.get(slot_key):
            return "already_claimed"

        required = slot_data.get("required_refers", 0)
        if user_data.get("total_referrals", 0) < required:
            return "not_eligible"

        stock = slot_data.get("stock", [])
        if not stock:
            return "out_of_stock"

        item = stock[0]
        transaction.update(slot_ref, {"stock": stock[1:]})
        claims[slot_key] = True
        transaction.update(user_ref, {"claims": claims})
        return item

    def _run():
        transaction = db.transaction()
        result = _txn(transaction)
        if result not in ("already_claimed", "not_eligible", "out_of_stock"):
            _invalidate_slot_cache(slot_key)
        return result

    return await run_sync(_run)


async def get_users_summary() -> tuple[int, int]:
    def _get():
        agg_query = firestore_aggregation.AggregationQuery(USERS)
        agg_query.count(alias="total")
        agg_query.sum("total_referrals", alias="refs")
        results = agg_query.get()
        values = {r.alias: r.value for group in results for r in group}
        return int(values.get("total", 0) or 0), int(values.get("refs", 0) or 0)

    return await run_sync(_get)


async def get_all_user_ids() -> list[int]:
    def _get():
        ids = []
        for doc in USERS.stream():
            try:
                ids.append(int(doc.id))
            except ValueError:
                logger.warning("Skipping user doc with non-numeric ID: %s", doc.id)
        return ids

    return await run_sync(_get)


async def remove_referrals_from_user(user_id: int, amount: int) -> bool:
    def _run():
        ref = USERS.document(str(user_id))
        snap = ref.get()
        if not snap.exists:
            return False
        current = (snap.to_dict() or {}).get("total_referrals", 0)
        ref.update({"total_referrals": max(0, current - amount)})
        return True

    return await run_sync(_run)


def _batched_commit(docs, update_fn):
    count = 0
    batch = db.batch()
    for doc in docs:
        batch.update(doc.reference, update_fn(doc))
        count += 1
        if count % 450 == 0:
            batch.commit()
            batch = db.batch()
    if count % 450 != 0:
        batch.commit()
    return count


async def add_referrals_to_all(amount: int) -> int:
    def _run():
        docs = list(USERS.stream())
        return _batched_commit(docs, lambda doc: {"total_referrals": firestore.Increment(amount)})

    return await run_sync(_run)


async def remove_referrals_from_all(amount: int) -> int:
    def _run():
        docs = list(USERS.stream())

        def _update(doc):
            current = (doc.to_dict() or {}).get("total_referrals", 0)
            return {"total_referrals": max(0, current - amount)}

        return _batched_commit(docs, _update)

    return await run_sync(_run)


# ==================== SLOTS (dynamic list) ====================
async def get_slot_keys() -> list[str]:
    def _get():
        return _get_config_cached().get("slot_keys", list(DEFAULT_SLOT_KEYS))

    return await run_sync(_get)


async def get_all_slots() -> list[tuple[str, str]]:
    def _get():
        keys = _get_config_cached().get("slot_keys", list(DEFAULT_SLOT_KEYS))
        return [(k, _get_slot_cached(k).get("name", k)) for k in keys]

    return await run_sync(_get)


async def get_all_slots_full() -> list[tuple[str, dict]]:
    def _get():
        keys = _get_config_cached().get("slot_keys", list(DEFAULT_SLOT_KEYS))
        return [(k, _get_slot_cached(k)) for k in keys]

    return await run_sync(_get)


async def add_new_slot(name: str) -> str:
    def _run():
        data = _get_config_cached(force=True)
        keys = list(data.get("slot_keys", list(DEFAULT_SLOT_KEYS)))
        # Start counting from the highest existing slot number (not len(keys)+1),
        # so a gap left by a removed slot can never collide with an existing key.
        existing_nums = []
        for k in keys:
            suffix = k.rsplit("_", 1)[-1]
            if suffix.isdigit():
                existing_nums.append(int(suffix))
        n = max(existing_nums, default=0) + 1
        new_key = f"slot_{n}"
        while new_key in keys:
            n += 1
            new_key = f"slot_{n}"
        SLOTS.document(new_key).set({"name": name, "required_refers": 5, "stock": []})
        keys.append(new_key)
        CONFIG.set({"slot_keys": keys}, merge=True)
        _invalidate_config_cache()
        return new_key

    return await run_sync(_run)

async def remove_slot(slot_key: str) -> None:
    def _run():
        data = _get_config_cached(force=True)
        keys = list(data.get("slot_keys", list(DEFAULT_SLOT_KEYS)))
        if slot_key in keys:
            keys.remove(slot_key)
            CONFIG.set({"slot_keys": keys}, merge=True)
            
        SLOTS.document(slot_key).delete()
        _invalidate_config_cache()
        _invalidate_slot_cache(slot_key)

    await run_sync(_run)


# ==================== COUPON NAME ====================
async def get_coupon_name() -> str:
    def _get():
        return _get_config_cached().get("coupon_name", "Coupon")

    return await run_sync(_get)


async def set_coupon_name(name: str):
    def _run():
        CONFIG.set({"coupon_name": name}, merge=True)
        _invalidate_config_cache()

    await run_sync(_run)


# ==================== HISTORY ====================
async def log_history(user_id: int, slot_key: str, code: str):
    def _run():
        HISTORY.add(
            {
                "user_id": user_id,
                "slot": slot_key,
                "code": code,
                "timestamp": datetime.now(timezone.utc),
            }
        )

    await run_sync(_run)


async def get_user_history(user_id: int, limit: int = 20):
    def _get():
        query = (
            HISTORY.where("user_id", "==", user_id)
            .order_by("timestamp", direction=firestore.Query.DESCENDING)
            .limit(limit)
        )
        return [doc.to_dict() for doc in query.stream()]

    return await run_sync(_get)


async def get_global_history(limit: int = 20):
    def _get():
        query = HISTORY.order_by("timestamp", direction=firestore.Query.DESCENDING).limit(limit)
        return [doc.to_dict() for doc in query.stream()]

    return await run_sync(_get)


# ==================== CHANNEL VERIFICATION ====================
async def check_single_channel(channel_username: str, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id=channel_username, user_id=user_id)
        return member.status not in ["left", "kicked"]
    except Exception:
        return False


async def is_user_subscribed(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    tasks = [check_single_channel(ch["username"], user_id, context) for ch in REQUIRED_CHANNELS]
    results = await asyncio.gather(*tasks)
    return all(results)


_REQUIRED_CHANNEL_USERNAMES = {ch["username"].lstrip("@").lower() for ch in REQUIRED_CHANNELS}
_ACTIVE_STATUSES = {"member", "administrator", "creator"}
_INACTIVE_STATUSES = {"left", "kicked"}


async def on_channel_membership_change(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmu = update.chat_member
    if cmu is None:
        return

    chat_username = (cmu.chat.username or "").lower()
    if chat_username not in _REQUIRED_CHANNEL_USERNAMES:
        return 

    user_id = cmu.new_chat_member.user.id
    if user_id == context.bot.id:
        return

    new_status = cmu.new_chat_member.status
    old_status = cmu.old_chat_member.status if cmu.old_chat_member else None

    if new_status in _INACTIVE_STATUSES:
        await revoke_access(user_id)
        return

    if new_status in _ACTIVE_STATUSES and old_status in _INACTIVE_STATUSES:
        if await is_user_subscribed(user_id, context):
            await mark_verified_and_reward_inviter(user_id)


def channels_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"📢 {ch['name']}", url=ch["link"])] for ch in REQUIRED_CHANNELS]
    rows.append([InlineKeyboardButton("✅ I've Joined — Verify Me", callback_data="verify")])
    return InlineKeyboardMarkup(rows)


# ==================== KEYBOARDS ====================
def main_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton("👤 Profile"), KeyboardButton("🔗 Invite & Earn")],
        [KeyboardButton("🎁 Claim Reward"), KeyboardButton("📜 History")],
        [KeyboardButton("❓ Help"), KeyboardButton("📞 Support")],
    ]
    if user_id in ADMIN_IDS:
        rows.append([KeyboardButton("🛠 Admin Panel")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def admin_panel_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📦 Inventory Management", callback_data="admin_inventory")],
        [InlineKeyboardButton("➕ Add Slot", callback_data="admin_addslot"),
         InlineKeyboardButton("➖ Remove Slot", callback_data="admin_removeslot")],
        [InlineKeyboardButton("🎯 Set Refer Requirement", callback_data="admin_setrefer")],
        [InlineKeyboardButton("📨 Send Refer", callback_data="admin_sendrefer"),
         InlineKeyboardButton("➖ Remove Refer", callback_data="admin_removerefer")],
        [InlineKeyboardButton("🏷 Coupon Name", callback_data="admin_couponname")],
        [InlineKeyboardButton("🚫 Ban User", callback_data="admin_ban"),
         InlineKeyboardButton("✅ Unban User", callback_data="admin_unban")],
        [InlineKeyboardButton("📊 Stats", callback_data="admin_stats"),
         InlineKeyboardButton("🏆 Leaderboard", callback_data="leaderboard")],
        [InlineKeyboardButton("📜 Redemption Log", callback_data="admin_history")],
    ]
    return InlineKeyboardMarkup(rows)


def slot_picker_keyboard(prefix: str, slots: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"🔹 {name}", callback_data=f"{prefix}:{key}")] for key, name in slots]
    rows.append([InlineKeyboardButton("🔙 Back", callback_data="admin_panel")])
    return InlineKeyboardMarkup(rows)


def inventory_slot_menu_keyboard(slot_key: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("✏️ Rename", callback_data=f"inv_rename:{slot_key}")],
        [InlineKeyboardButton("➕ Add Item", callback_data=f"inv_additem:{slot_key}")],
        [InlineKeyboardButton("🗑 Remove All", callback_data=f"inv_removeall:{slot_key}")],
        [InlineKeyboardButton("🔙 Back", callback_data="admin_inventory")],
    ]
    return InlineKeyboardMarkup(rows)


def additem_mode_keyboard(slot_key: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("1️⃣ Single", callback_data=f"inv_additem_single:{slot_key}")],
        [InlineKeyboardButton("📋 All (Bulk)", callback_data=f"inv_additem_bulk:{slot_key}")],
        [InlineKeyboardButton("🔙 Back", callback_data=f"admin_inventory:{slot_key}")],
    ]
    return InlineKeyboardMarkup(rows)


def send_refer_mode_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("1️⃣ Single", callback_data="admin_sendrefer_single")],
        [InlineKeyboardButton("📋 All", callback_data="admin_sendrefer_all")],
        [InlineKeyboardButton("🔙 Back", callback_data="admin_panel")],
    ]
    return InlineKeyboardMarkup(rows)


def remove_refer_mode_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("1️⃣ Single", callback_data="admin_removerefer_single")],
        [InlineKeyboardButton("📋 All", callback_data="admin_removerefer_all")],
        [InlineKeyboardButton("🔙 Back", callback_data="admin_panel")],
    ]
    return InlineKeyboardMarkup(rows)


# ==================== START / VERIFICATION ====================
async def _build_welcome_text(display_name: str) -> str:
    coupon_name = await get_coupon_name()
    slots = await get_all_slots_full()
    lines = []
    for key, slot in slots:
        name = slot.get("name", key)
        required = slot.get("required_refers", 0)
        lines.append(f"  • {html.escape(name)} — {required} referrals")
    slots_block = "\n".join(lines) if lines else "  • (rewards coming soon)"

    safe_coupon_name = html.escape(coupon_name)
    return (
        f"✨ <b>Welcome, {html.escape(display_name)}!</b>\n\n"
        f"This bot gives away free <b>{safe_coupon_name}</b> codes to people who invite friends. "
        "Here's how it works:\n\n"
        "🔗 <b>Invite & Earn</b> — Get your personal referral link and share it anywhere. "
        "Every friend who joins and verifies adds 1 to your referral count.\n\n"
        f"🎁 <b>Claim Reward</b> — Once you hit a slot's required referrals, redeem it for a free "
        f"{safe_coupon_name}:\n{slots_block}\n\n"
        "👤 <b>Profile</b> — See your referral count, leaderboard rank, and what you've claimed.\n\n"
        "📜 <b>History</b> — See every code you've redeemed and when.\n\n"
        "⚠️ Each reward slot can only be claimed once per person, and stock is limited — the earlier "
        "you invite, the better your chances of getting a code before a slot runs out.\n\n"
        "Tap a button below to get started. 👇"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    referred_by = None
    if context.args and context.args[0].isdigit():
        ref_id = int(context.args[0])
        if ref_id != user.id:
            referred_by = ref_id

    data = await get_or_create_user(user.id, user.first_name or "User", user.username or "", referred_by)

    if data.get("is_banned"):
        await update.message.reply_text("🚫 You are banned from using this bot.")
        return

    if not data.get("is_verified"):
        if not await is_user_subscribed(user.id, context):
            await update.message.reply_text(
                "👋 <b>Welcome!</b>\n\nPlease join all the channels below, then tap "
                "<b>I've Joined — Verify Me</b> to unlock the bot.",
                parse_mode="HTML",
                reply_markup=channels_keyboard(),
            )
            return
        await mark_verified_and_reward_inviter(user.id)

    await update.message.reply_text(
        await _build_welcome_text(user.first_name or "friend"),
        parse_mode="HTML",
        reply_markup=main_keyboard(user.id),
    )


async def handle_verify_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    if not await is_user_subscribed(user.id, context):
        await query.answer("⚠️ You haven't joined all channels yet.", show_alert=True)
        return
    await mark_verified_and_reward_inviter(user.id)
    await query.answer("✅ Verified!")
    await query.message.delete()
    await context.bot.send_message(
        chat_id=user.id,
        text=await _build_welcome_text(user.first_name or "friend"),
        parse_mode="HTML",
        reply_markup=main_keyboard(user.id),
    )


async def _get_user_or_banned(user_id: int, send) -> dict | None:
    data = await get_user(user_id)
    if data and data.get("is_banned"):
        await send("🚫 You are banned from using this bot.")
        return None
    return data or {}


# ==================== PROFILE ====================
async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = await _get_user_or_banned(user.id, update.message.reply_text)
    if data is None:
        return
    claims = data.get("claims", {})
    rank, all_slots = await asyncio.gather(
        get_user_rank(user.id, data.get("total_referrals", 0)),
        get_all_slots(),
    )
    claimed_list = [name for key, name in all_slots if claims.get(key)] or ["None yet"]

    msg = (
        "👤 <b>YOUR PROFILE</b>\n\n"
        f"• Name: <code>{html.escape(data.get('name', user.first_name or ''))}</code>\n"
        f"• ID: <code>{user.id}</code>\n"
        f"• Referrals: <code>{data.get('total_referrals', 0)}</code>\n"
        f"• Leaderboard Rank: <code>#{rank}</code>\n"
        f"• Claimed Rewards: <code>{', '.join(claimed_list)}</code>"
    )
    await update.message.reply_text(msg, parse_mode="HTML")


# ==================== INVITE ====================
async def show_invite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = await _get_user_or_banned(user.id, update.message.reply_text)
    if data is None:
        return
    coupon_name, me = await asyncio.gather(get_coupon_name(), context.bot.get_me())
    bot_username = me.username
    link = f"https://t.me/{bot_username}?start={user.id}"
    share_text = f"Join and get free {coupon_name} codes! 🎁"
    share_url = f"https://t.me/share/url?url={quote(link, safe='')}&text={quote(share_text, safe='')}"
    msg = (
        "🔗 <b>INVITE & EARN</b>\n\n"
        "Share your link below — every friend who joins and verifies counts toward your rewards!\n\n"
        f"<code>{link}</code>\n\n"
        f"👥 Total Referrals: <code>{data.get('total_referrals', 0)}</code>"
    )
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("📤 Share via Telegram", url=share_url)]])
    await update.message.reply_text(msg, parse_mode="HTML", reply_markup=keyboard)


# ==================== LEADERBOARD ====================
async def _leaderboard_text() -> str:
    rows = await get_leaderboard(10)
    msg = "🏆 <b>LEADERBOARD — Top 10</b>\n\n"
    if rows:
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        for i, (uid, data) in enumerate(rows, 1):
            medal = medals.get(i, "🏅")
            name = html.escape(data.get("name", "User"))
            refs = data.get("total_referrals", 0)
            msg += f"{medal} {name} (<code>{uid}</code>) — <b>{refs}</b> referrals\n"
    else:
        msg += "<i>No data yet.</i>"
    return msg


async def leaderboard_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]])
    await query.message.edit_text(await _leaderboard_text(), parse_mode="HTML", reply_markup=keyboard)


# ==================== CLAIM REWARD ====================
async def _claim_menu_content(user_id: int, user_data: dict | None = None):
    data = user_data if user_data is not None else (await get_user(user_id) or {})
    claims = data.get("claims", {})
    refs = data.get("total_referrals", 0)
    coupon_name, slots = await asyncio.gather(get_coupon_name(), get_all_slots_full())
    rows = []
    for key, slot in slots:
        name = slot.get("name", key)
        required = slot.get("required_refers", 0)
        stock_left = len(slot.get("stock", []))
        if claims.get(key):
            continue 
        label = f"🎁 {name} — {refs}/{required} refers | {stock_left} left"
        rows.append([InlineKeyboardButton(label, callback_data=f"claim:{key}")])

    safe_coupon_name = html.escape(coupon_name)
    if not slots:
        text = f"⚠️ No {safe_coupon_name} reward slots are configured yet — check back soon."
    elif not rows:
        text = f"🎉 You've already claimed every available {safe_coupon_name}!"
    else:
        text = f"🎁 <b>CLAIM {safe_coupon_name.upper()}</b>\n\nYour referrals: <b>{refs}</b>\nPick a slot to redeem:"

    rows.append(
        [InlineKeyboardButton("🔄 Refresh", callback_data="claim_refresh"),
         InlineKeyboardButton("🏠 Main Menu", callback_data="claim_backmenu")]
    )
    return text, InlineKeyboardMarkup(rows)


async def show_claim_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = await _get_user_or_banned(user.id, update.message.reply_text)
    if data is None:
        return
    text, keyboard = await _claim_menu_content(user.id, data)
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


async def claim_refresh_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_data = await get_user(query.from_user.id)
    if user_data and user_data.get("is_banned"):
        await query.answer("🚫 You are banned from using this bot.", show_alert=True)
        return
    await query.answer("🔄 Refreshed")
    text, keyboard = await _claim_menu_content(query.from_user.id, user_data or {})
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)


async def claim_backmenu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.edit_text("👇 Use the menu below to continue.")


async def claim_slot_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    slot_key = query.data.split(":", 1)[1]

    user_data = await get_user(user.id)
    if user_data and user_data.get("is_banned"):
        await query.answer("🚫 You are banned from using this bot.", show_alert=True)
        return

    result = await redeem_slot(user.id, slot_key)

    if result == "already_claimed":
        await query.answer("You've already claimed this reward.", show_alert=True)
    elif result == "not_eligible":
        slot = await get_slot(slot_key)
        await query.answer(
            f"You need {slot.get('required_refers', 0)} referrals to unlock this slot.", show_alert=True
        )
    elif result == "out_of_stock":
        await query.answer(
            "📦 Stock out! This reward is temporarily unavailable — stock will be updated soon.",
            show_alert=True,
        )
    else:
        await query.answer("✅ Reward claimed!")
        coupon_name = html.escape(await get_coupon_name())
        await log_history(user.id, slot_key, result)
        await context.bot.send_message(
            chat_id=user.id,
            text=f"🎉 <b>{coupon_name} Claimed!</b>\n\nHere's your {coupon_name} code:\n<code>{html.escape(result)}</code>",
            parse_mode="HTML",
        )

    text, keyboard = await _claim_menu_content(user.id)
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)


# ==================== ADMIN: PANEL / STATS ====================
async def show_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    await update.message.reply_text("🛠 <b>ADMIN PANEL</b>", parse_mode="HTML", reply_markup=admin_panel_keyboard())


async def admin_panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.edit_text("🛠 <b>ADMIN PANEL</b>", parse_mode="HTML", reply_markup=admin_panel_keyboard())


async def admin_stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    total_users, total_refs = await get_users_summary()
    total_stock = await get_total_stock()
    msg = (
        "📊 <b>BOT STATS</b>\n\n"
        f"👥 Total Users: <code>{total_users}</code>\n"
        f"🔗 Total Referrals: <code>{total_refs}</code>\n"
        f"📦 Total Stock (all slots): <code>{total_stock}</code>"
    )
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]])
    await query.message.edit_text(msg, parse_mode="HTML", reply_markup=keyboard)


# ==================== ADMIN: INVENTORY ====================
async def admin_inventory_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    slots = await get_all_slots()
    await query.message.edit_text(
        "📦 <b>INVENTORY MANAGEMENT</b>\n\nChoose a slot:",
        parse_mode="HTML",
        reply_markup=slot_picker_keyboard("admin_inventory", slots),
    )


async def admin_inventory_slot_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    slot_key = query.data.split(":", 1)[1]
    slot = await get_slot(slot_key)
    msg = (
        f"📦 <b>{html.escape(slot.get('name', slot_key))}</b>\n\n"
        f"🎯 Required Referrals: <code>{slot.get('required_refers', 0)}</code>\n"
        f"📦 Stock Remaining: <code>{len(slot.get('stock', []))}</code>"
    )
    await query.message.edit_text(msg, parse_mode="HTML", reply_markup=inventory_slot_menu_keyboard(slot_key))


async def inv_rename_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    slot_key = query.data.split(":", 1)[1]
    await query.answer()
    context.user_data["pending_action"] = f"rename:{slot_key}"
    slot = await get_slot(slot_key)
    await query.message.edit_text(
        f"✏️ Send the new name for <b>{html.escape(slot.get('name', slot_key))}</b>:", parse_mode="HTML"
    )


async def inv_additem_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    slot_key = query.data.split(":", 1)[1]
    await query.answer()
    coupon_name = html.escape(await get_coupon_name())
    slot = await get_slot(slot_key)
    await query.message.edit_text(
        f"➕ Add {coupon_name} to <b>{html.escape(slot.get('name', slot_key))}</b> — single code or bulk paste?",
        parse_mode="HTML",
        reply_markup=additem_mode_keyboard(slot_key),
    )


async def inv_additem_single_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    slot_key = query.data.split(":", 1)[1]
    await query.answer()
    context.user_data["pending_action"] = f"additem_single:{slot_key}"
    coupon_name = await get_coupon_name()
    await query.message.edit_text(f"1️⃣ Send the single {coupon_name} code to add:")


async def inv_additem_bulk_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    slot_key = query.data.split(":", 1)[1]
    await query.answer()
    context.user_data["pending_action"] = f"additem_bulk:{slot_key}"
    coupon_name = await get_coupon_name()
    await query.message.edit_text(f"📋 Send all {coupon_name} codes, one per line:")


async def inv_removeall_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    slot_key = query.data.split(":", 1)[1]
    await query.answer()
    slot = await get_slot(slot_key)
    count = await remove_all_stock(slot_key)
    await query.message.edit_text(
        f"🗑 Removed <b>{count}</b> item(s) from <b>{html.escape(slot.get('name', slot_key))}</b>.",
        parse_mode="HTML",
        reply_markup=inventory_slot_menu_keyboard(slot_key),
    )


# ==================== ADMIN: ADD SLOT ====================
async def admin_addslot_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["pending_action"] = "addslot"
    await query.message.edit_text("➕ Send a name for the new slot (e.g. <i>Amazon ₹100</i>):", parse_mode="HTML")


# ==================== ADMIN: REMOVE SLOT ====================
async def admin_removeslot_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    slots = await get_all_slots()
    
    if not slots:
        await query.message.edit_text(
            "⚠️ There are no slots available to remove.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]])
        )
        return

    await query.message.edit_text(
        "➖ <b>REMOVE SLOT</b>\n\nChoose a slot to permanently delete (this action cannot be undone):",
        parse_mode="HTML",
        reply_markup=slot_picker_keyboard("admin_removeslot_slot", slots),
    )

async def admin_removeslot_slot_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    slot_key = query.data.split(":", 1)[1]
    
    await query.answer()
    slot = await get_slot(slot_key)
    slot_name = slot.get("name", slot_key)
    
    await remove_slot(slot_key)
    
    await query.message.edit_text(
        f"✅ Slot <b>{html.escape(slot_name)}</b> has been permanently removed.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]])
    )


# ==================== ADMIN: SET REFER ====================
async def admin_setrefer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    slots = await get_all_slots()
    await query.message.edit_text(
        "🎯 <b>SET REFER REQUIREMENT</b>\n\nChoose a slot:",
        parse_mode="HTML",
        reply_markup=slot_picker_keyboard("admin_setrefer_slot", slots),
    )


async def admin_setrefer_slot_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    slot_key = query.data.split(":", 1)[1]
    await query.answer()
    context.user_data["pending_action"] = f"setrefer:{slot_key}"
    slot = await get_slot(slot_key)
    await query.message.edit_text(
        f"🎯 Send the number of referrals required to unlock <b>{html.escape(slot.get('name', slot_key))}</b>:",
        parse_mode="HTML",
    )


# ==================== ADMIN: BAN / UNBAN ====================
async def admin_ban_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["pending_action"] = "ban_user"
    await query.message.edit_text("🚫 Send the User ID to ban:")


async def admin_unban_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["pending_action"] = "unban_user"
    await query.message.edit_text("✅ Send the User ID to unban:")


# ==================== ADMIN: SEND REFER ====================
async def admin_sendrefer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.edit_text(
        "📨 <b>SEND REFER</b>\n\nGrant referral credit to a single user, or to everyone at once:",
        parse_mode="HTML",
        reply_markup=send_refer_mode_keyboard(),
    )


async def admin_sendrefer_single_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["pending_action"] = "sendrefer_single"
    await query.message.edit_text("1️⃣ Send: <code>USER_ID AMOUNT</code>\ne.g. <code>123456789 5</code>", parse_mode="HTML")


async def admin_sendrefer_all_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["pending_action"] = "sendrefer_all"
    await query.message.edit_text("📋 Send the amount of referrals to grant to <b>every</b> user:", parse_mode="HTML")


# ==================== ADMIN: REMOVE REFER ====================
async def admin_removerefer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.edit_text(
        "➖ <b>REMOVE REFER</b>\n\nDeduct referral credit from a single user, or from everyone at once:",
        parse_mode="HTML",
        reply_markup=remove_refer_mode_keyboard(),
    )


async def admin_removerefer_single_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["pending_action"] = "removerefer_single"
    await query.message.edit_text("1️⃣ Send: <code>USER_ID AMOUNT</code>\ne.g. <code>123456789 5</code>", parse_mode="HTML")


async def admin_removerefer_all_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["pending_action"] = "removerefer_all"
    await query.message.edit_text("📋 Send the amount of referrals to deduct from <b>every</b> user:", parse_mode="HTML")


# ==================== ADMIN: COUPON NAME ====================
async def admin_couponname_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["pending_action"] = "couponname"
    current = await get_coupon_name()
    await query.message.edit_text(
        f"🏷 Current coupon name: <b>{html.escape(current)}</b>\n\n"
        "Send the new coupon name (e.g. <i>Amazon Gift Card</i>). It will update everywhere "
        "the bot mentions the coupon.",
        parse_mode="HTML",
    )


# ==================== HISTORY ====================
async def show_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = await _get_user_or_banned(user.id, update.message.reply_text)
    if data is None:
        return
    try:
        entries, coupon_name, all_slots = await asyncio.gather(
            get_user_history(user.id, 20),
            get_coupon_name(),
            get_all_slots(),
        )
    except Exception:
        logger.exception("show_history: failed to load history for user_id=%s", user.id)
        await update.message.reply_text(
            "⚠️ Couldn't load your history right now — please try again in a moment."
        )
        return

    slot_names = dict(all_slots)
    if not entries:
        msg = f"📜 <b>YOUR HISTORY</b>\n\n<i>No {html.escape(coupon_name)} claims yet.</i>"
    else:
        msg = f"📜 <b>YOUR HISTORY</b>\n\n"
        for e in entries:
            slot_name = slot_names.get(e.get("slot"), e.get("slot", "?"))
            ts = e.get("timestamp")
            ts_str = ts.strftime("%Y-%m-%d %H:%M") if ts else "?"
            msg += f"• {slot_name} — <code>{html.escape(e.get('code', ''))}</code> ({ts_str})\n"
    await update.message.reply_text(msg, parse_mode="HTML")


async def admin_history_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]])
    try:
        entries, all_slots = await asyncio.gather(get_global_history(20), get_all_slots())
    except Exception:
        logger.exception("admin_history_callback: failed to load global history")
        await query.message.edit_text(
            "⚠️ Couldn't load the redemption log right now — please try again in a moment.",
            reply_markup=keyboard,
        )
        return
    slot_names = dict(all_slots)
    if not entries:
        msg = "📜 <b>REDEMPTION LOG</b>\n\n<i>No claims yet.</i>"
    else:
        msg = "📜 <b>REDEMPTION LOG — Last 20</b>\n\n"
        for e in entries:
            slot_name = slot_names.get(e.get("slot"), e.get("slot", "?"))
            ts = e.get("timestamp")
            ts_str = ts.strftime("%Y-%m-%d %H:%M") if ts else "?"
            msg += f"• <code>{e.get('user_id')}</code> — {slot_name} — <code>{html.escape(e.get('code', ''))}</code> ({ts_str})\n"
    await query.message.edit_text(msg, parse_mode="HTML", reply_markup=keyboard)


# ==================== ADMIN: BROADCAST ====================
_BROADCAST_CONCURRENCY = 25


async def _send_broadcast_one(context, uid: int, text: str, sem: asyncio.Semaphore) -> bool:
    async with sem:
        try:
            await context.bot.send_message(chat_id=uid, text=text, parse_mode="HTML")
            return True
        except Exception:
            return False
        finally:
            await asyncio.sleep(1.0 / 28)


async def cancel_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("pending_action"):
        context.user_data["pending_action"] = None
        await update.message.reply_text("❌ Cancelled.")
    else:
        await update.message.reply_text("Nothing to cancel.")


async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if not context.args:
        await update.message.reply_text("⚠️ <b>Usage:</b> <code>/broadcast YOUR_MESSAGE</code>", parse_mode="HTML")
        return
    text = " ".join(context.args)
    user_ids = await get_all_user_ids()
    sem = asyncio.Semaphore(_BROADCAST_CONCURRENCY)
    results = await asyncio.gather(*(_send_broadcast_one(context, uid, text, sem) for uid in user_ids))
    sent = sum(results)
    await update.message.reply_text(f"📢 Broadcast sent to <code>{sent}/{len(user_ids)}</code> users.", parse_mode="HTML")


# ==================== HELP / SUPPORT ====================
async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = await _get_user_or_banned(user.id, update.message.reply_text)
    if data is None:
        return
    coupon_name = html.escape(await get_coupon_name())
    msg = (
        "❓ <b>HELP & FAQ</b>\n\n"
        "<b>My referral count didn't go up — why?</b>\n"
        "It only counts once your friend joins <i>and</i> verifies by joining all required channels. "
        "Ask them to check they've done both.\n\n"
        f"<b>Can I claim the same {coupon_name} slot twice?</b>\n"
        "No — each slot can be redeemed once per person. Once claimed, it disappears from your Claim menu.\n\n"
        "<b>A slot shows 0 stock — what now?</b>\n"
        "That slot is temporarily out and will be restocked soon. Keep an eye on 🎁 Claim Reward for updates.\n\n"
        "<b>Where do I see my progress?</b>\n"
        "👤 Profile shows your referral count and what you've claimed. 🎁 Claim Reward shows every slot's "
        "requirement and stock.\n\n"
        "<b>Still stuck?</b>\n"
        "Tap 📞 Support and we'll sort it out."
    )
    await update.message.reply_text(msg, parse_mode="HTML")


async def show_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if await _get_user_or_banned(user.id, update.message.reply_text) is None:
        return
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("📞 Contact Support", url=SUPPORT_URL)]])
    await update.message.reply_text(
        "📞 <b>SUPPORT</b>\n\nNeed help or have an issue with a claim? Tap below to reach us.",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


# ==================== TEXT MESSAGE ROUTER ====================
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (update.message.text or "").strip()

    pending = context.user_data.get("pending_action")
    if pending and user.id in ADMIN_IDS:
        if text.strip().lower() == "cancel":
            context.user_data["pending_action"] = None
            await update.message.reply_text("❌ Cancelled.")
            return
        await handle_pending_admin_input(update, context, pending, text)
        context.user_data["pending_action"] = None
        return

    if text == "👤 Profile":
        await show_profile(update, context)
    elif text == "🔗 Invite & Earn":
        await show_invite(update, context)
    elif text == "🎁 Claim Reward":
        await show_claim_menu(update, context)
    elif text == "📜 History":
        await show_history(update, context)
    elif text == "❓ Help":
        await show_help(update, context)
    elif text == "📞 Support":
        await show_support(update, context)
    elif text == "🛠 Admin Panel":
        await show_admin_panel(update, context)


async def handle_pending_admin_input(update: Update, context: ContextTypes.DEFAULT_TYPE, pending: str, text: str):
    action, _, slot_key = pending.partition(":")

    if action == "rename":
        await rename_slot(slot_key, text)
        await update.message.reply_text(
            f"✅ Slot renamed to: <b>{html.escape(text)}</b>", parse_mode="HTML"
        )

    elif action == "additem_single":
        slot = await get_slot(slot_key)
        await add_stock(slot_key, [text])
        await update.message.reply_text(f"✅ Added 1 item to {slot.get('name', slot_key)}.")

    elif action == "additem_bulk":
        items = [line.strip() for line in text.splitlines() if line.strip()]
        if not items:
            await update.message.reply_text("⚠️ No valid items found.")
            return
        slot = await get_slot(slot_key)
        await add_stock(slot_key, items)
        await update.message.reply_text(f"✅ Added {len(items)} items to {slot.get('name', slot_key)}.")

    elif action == "setrefer":
        if not text.isdigit():
            await update.message.reply_text("⚠️ Please send a valid number.")
            return
        slot = await get_slot(slot_key)
        await set_slot_required(slot_key, int(text))
        await update.message.reply_text(
            f"✅ {slot.get('name', slot_key)} now requires <b>{text}</b> referrals.", parse_mode="HTML"
        )

    elif pending == "addslot":
        if not text.strip():
            await update.message.reply_text("⚠️ Please send a valid slot name.")
            return
        new_key = await add_new_slot(text.strip())
        await update.message.reply_text(
            f"✅ New slot created: <b>{html.escape(text.strip())}</b> (<code>{new_key}</code>)\n\n"
            "Don't forget to set its referral requirement and add stock via Inventory Management.",
            parse_mode="HTML",
        )

    elif pending == "ban_user":
        if not text.isdigit():
            await update.message.reply_text("⚠️ Please send a valid numeric User ID.")
            return
        ok = await set_ban(int(text), True)
        await update.message.reply_text("🚫 User banned." if ok else "❌ User not found.")

    elif pending == "unban_user":
        if not text.isdigit():
            await update.message.reply_text("⚠️ Please send a valid numeric User ID.")
            return
        ok = await set_ban(int(text), False)
        await update.message.reply_text("✅ User unbanned." if ok else "❌ User not found.")

    elif pending == "removerefer_single":
        parts = text.split()
        if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
            await update.message.reply_text("⚠️ Format: <code>USER_ID AMOUNT</code>", parse_mode="HTML")
            return
        uid, amount = int(parts[0]), int(parts[1])
        ok = await remove_referrals_from_user(uid, amount)
        if ok:
            await update.message.reply_text(f"✅ {amount} referrals removed from <code>{uid}</code>.", parse_mode="HTML")
        else:
            await update.message.reply_text("❌ User not found.")

    elif pending == "removerefer_all":
        if not text.isdigit():
            await update.message.reply_text("⚠️ Please send a valid number.")
            return
        amount = int(text)
        count = await remove_referrals_from_all(amount)
        await update.message.reply_text(f"✅ {amount} referrals removed from all {count} users.")

    elif pending == "couponname":
        if not text:
            await update.message.reply_text("⚠️ Please send a valid name.")
            return
        await set_coupon_name(text)
        await update.message.reply_text(f"✅ Coupon name updated to: <b>{html.escape(text)}</b>", parse_mode="HTML")

    elif pending == "sendrefer_single":
        parts = text.split()
        if len(parts) != 2 or not parts[0].isdigit() or not parts[1].lstrip("-").isdigit():
            await update.message.reply_text("⚠️ Format: <code>USER_ID AMOUNT</code>", parse_mode="HTML")
            return
        uid, amount = int(parts[0]), int(parts[1])
        ok = await add_referrals_to_user(uid, amount)
        if ok:
            await update.message.reply_text(f"✅ {amount:+} referrals applied to <code>{uid}</code>.", parse_mode="HTML")
        else:
            await update.message.reply_text("❌ User not found.")

    elif pending == "sendrefer_all":
        if not text.lstrip("-").isdigit():
            await update.message.reply_text("⚠️ Please send a valid number.")
            return
        amount = int(text)
        count = await add_referrals_to_all(amount)
        await update.message.reply_text(f"✅ {amount:+} referrals applied to all {count} users.")


# ==================== CALLBACK ROUTER ====================
async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    if data == "verify":
        await handle_verify_callback(update, context)
    elif data == "admin_panel":
        await admin_panel_callback(update, context)
    elif data == "admin_stats":
        await admin_stats_callback(update, context)
    elif data == "leaderboard":
        await leaderboard_callback(update, context)
    elif data == "admin_inventory":
        await admin_inventory_callback(update, context)
    elif data.startswith("admin_inventory:"):
        await admin_inventory_slot_callback(update, context)
    elif data.startswith("inv_rename:"):
        await inv_rename_callback(update, context)
    elif data.startswith("inv_additem_single:"):
        await inv_additem_single_callback(update, context)
    elif data.startswith("inv_additem_bulk:"):
        await inv_additem_bulk_callback(update, context)
    elif data.startswith("inv_additem:"):
        await inv_additem_callback(update, context)
    elif data.startswith("inv_removeall:"):
        await inv_removeall_callback(update, context)
    elif data == "admin_addslot":
        await admin_addslot_callback(update, context)
    elif data == "admin_removeslot":
        await admin_removeslot_callback(update, context)
    elif data.startswith("admin_removeslot_slot:"):
        await admin_removeslot_slot_callback(update, context)
    elif data == "admin_setrefer":
        await admin_setrefer_callback(update, context)
    elif data.startswith("admin_setrefer_slot:"):
        await admin_setrefer_slot_callback(update, context)
    elif data == "admin_ban":
        await admin_ban_callback(update, context)
    elif data == "admin_unban":
        await admin_unban_callback(update, context)
    elif data == "admin_sendrefer_single":
        await admin_sendrefer_single_callback(update, context)
    elif data == "admin_sendrefer_all":
        await admin_sendrefer_all_callback(update, context)
    elif data == "admin_sendrefer":
        await admin_sendrefer_callback(update, context)
    elif data == "admin_removerefer_single":
        await admin_removerefer_single_callback(update, context)
    elif data == "admin_removerefer_all":
        await admin_removerefer_all_callback(update, context)
    elif data == "admin_removerefer":
        await admin_removerefer_callback(update, context)
    elif data == "admin_couponname":
        await admin_couponname_callback(update, context)
    elif data == "admin_history":
        await admin_history_callback(update, context)
    elif data == "claim_refresh":
        await claim_refresh_callback(update, context)
    elif data == "claim_backmenu":
        await claim_backmenu_callback(update, context)
    elif data.startswith("claim:"):
        await claim_slot_callback(update, context)
    else:
        await query.answer()


# ==================== MAIN ====================
def main():
    if not BOT_TOKEN:
        raise SystemExit("Set the BOT_TOKEN environment variable before running.")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CommandHandler("cancel", cancel_pending))
    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(ChatMemberHandler(on_channel_membership_change, ChatMemberHandler.CHAT_MEMBER))

    print("🤖 Bot started (Firebase edition)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()