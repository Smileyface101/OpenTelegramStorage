import logging

from fastapi import APIRouter, Depends, HTTPException
from telethon.errors import RPCError

from app import security
from app.models import User
from app.schemas import ChannelSelect, KeyExport, TelegramConnect
from app import settings_store
from app.db import get_db
from sqlalchemy.ext.asyncio import AsyncSession
from app.telegram.manager import TelegramNotConfigured, manager

router = APIRouter(prefix="/api/telegram", tags=["telegram"])
logger = logging.getLogger(__name__)


def status_out() -> dict:
    st = manager.status
    return {
        "configured": st.configured, "connected": st.connected, "error": st.error,
        "bot_username": st.bot_username, "bot_id": st.bot_id,
        "channel": ({"id": st.channel_id, "title": st.channel_title} if st.channel_id else None),
        "auto_delete_seconds": st.auto_delete_seconds,
        "discovered": [{"chat_id": c.chat_id, "title": c.title} for c in manager.discovered()],
    }


@router.get("/status")
async def status(user: User = Depends(security.current_user)):
    return status_out()


@router.post("/connect")
async def connect(data: TelegramConnect, user: User = Depends(security.current_admin)):
    try:
        await manager.configure(data.api_id, data.api_hash.strip(), data.bot_token.strip())
    except RPCError as e:
        raise HTTPException(400, f"Telegram rejected the credentials: {e.__class__.__name__}: {e}")
    except Exception as e:  # noqa: BLE001
        logger.exception("connect failed")
        raise HTTPException(400, f"Could not connect: {e}")
    return status_out()


@router.post("/channel")
async def select_channel(data: ChannelSelect, user: User = Depends(security.current_admin)):
    try:
        await manager.set_channel(data.chat_id)
    except TelegramNotConfigured as e:
        raise HTTPException(409, str(e))
    except (RPCError, ValueError) as e:
        raise HTTPException(400, f"Cannot use that channel: {e.__class__.__name__}: {e}. "
                                 "Make sure the bot is an admin with permission to post and delete messages, "
                                 "then post any message in the channel so the bot can see it.")
    return status_out()


@router.post("/disconnect")
async def disconnect(user: User = Depends(security.current_admin)):
    await manager.disconnect_and_forget()
    return status_out()


@router.post("/reconnect")
async def reconnect(user: User = Depends(security.current_admin)):
    """Drop the current MTProto session and connect again with the stored credentials."""
    await manager.reconnect()
    return status_out()


@router.post("/reveal")
async def reveal_credentials(data: KeyExport, db: AsyncSession = Depends(get_db), user: User = Depends(security.current_admin)):
    """Show the stored API id/hash and bot token (password required), e.g. to
    set up a second server on the same channel."""
    if not security.verify_password(data.password, user.password_hash):
        raise HTTPException(400, "Password is incorrect")
    return {
        "api_id": await settings_store.get(db, "telegram.api_id"),
        "api_hash": await settings_store.get(db, "telegram.api_hash"),
        "bot_token": await settings_store.get(db, "telegram.bot_token"),
        "channel_id": await settings_store.get(db, "telegram.channel_id"),
        "channel_title": await settings_store.get(db, "telegram.channel_title"),
    }


@router.post("/recheck")
async def recheck(user: User = Depends(security.current_user)):
    """Re-read the channel's auto-delete setting (after the user turned it off)."""
    await manager.check_auto_delete()
    return status_out()
