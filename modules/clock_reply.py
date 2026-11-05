"""Retain fresh clock-target replies while the DM sender is still awaiting ACK."""

import asyncio
import re

from meshcore import EventType

from .dm_contacts import DmContactResolver


class ClockReplyWatch:
    def __init__(self, bot, public_key: str):
        self.bot = bot
        self.key = public_key.lower()
        self.reply = asyncio.get_running_loop().create_future()
        self.subscription = None
        self.closed = False

    def __enter__(self):
        meshcore = getattr(self.bot, 'meshcore', None)
        subscribe = getattr(meshcore, 'subscribe', None)
        if callable(subscribe) and re.fullmatch('[0-9a-f]{64}', self.key):
            self.subscription = subscribe(EventType.CONTACT_MSG_RECV, self._receive)
        return self

    def _receive(self, event):
        if self.closed or self.reply.done() or event.type != EventType.CONTACT_MSG_RECV:
            return
        payload = event.payload
        if not isinstance(payload, dict):
            return
        prefix = str(payload.get('pubkey_prefix', '')).lower()
        if not re.fullmatch('[0-9a-f]{12}|[0-9a-f]{64}', prefix) or not self.key.startswith(prefix):
            return
        try:
            key, _, _ = DmContactResolver(self.bot).identify(prefix)
        except Exception:
            return
        if key != self.key:
            return
        text = payload.get('text')
        if isinstance(text, str) and text.strip():
            # Freshness is the live subscription, not the target clock: fixing
            # that clock is precisely the purpose of this operation.
            self.reply.set_result(text.strip())

    async def wait(self, timeout: float) -> tuple[bool, str]:
        if self.subscription is None:
            return False, ''
        if not self.reply.done() and timeout <= 0:
            return False, ''
        try:
            text = await asyncio.wait_for(asyncio.shield(self.reply), timeout=max(timeout, 0.001))
            return True, text
        except asyncio.TimeoutError:
            return False, ''

    def __exit__(self, *args):
        self.closed = True
        try:
            if self.subscription is not None:
                self.subscription.unsubscribe()
        finally:
            if not self.reply.done():
                self.reply.cancel()
