"""Discord client: records channel messages, answers mentions, handles privacy commands."""

from __future__ import annotations

import logging
import re

import discord
from discord import app_commands

from bot.config import Settings
from bot.llm import ChatProvider, LLMError
from bot.prompts import build_reply_messages
from bot.storage import MessageStore, OptOutStore, StoredMessage, SummaryStore
from bot.tasks import RetentionCleaner, SummaryWorker
from bot.timeutil import local_day, window_start

log = logging.getLogger(__name__)

DISCORD_MESSAGE_LIMIT = 2000

PRIVACY_NOTICE = (
    "**關於對話紀錄**\n"
    "這個機器人會記錄本伺服器頻道中的訊息，用來在被 @ 時根據近期對話回答問題。\n"
    "- 原始訊息保存在機器人主機本地，只保留最近 {days} 天，過期自動刪除。\n"
    "- 每個頻道每天會產生一份摘要。刪除訊息時，對應的紀錄會一併刪除，摘要會重建。\n"
    "- 使用 `/optout` 可以停止記錄你的訊息，並刪除已記錄的內容；使用 `/optin` 可以恢復。"
)


class MemoryBot(discord.Client):
    def __init__(
        self,
        settings: Settings,
        *,
        messages: MessageStore,
        summaries: SummaryStore,
        optouts: OptOutStore,
        provider: ChatProvider,
        worker: SummaryWorker,
        cleaner: RetentionCleaner,
    ) -> None:
        intents = discord.Intents.default()
        # Privileged intent: must also be enabled in the Developer Portal.
        intents.message_content = True
        super().__init__(
            intents=intents,
            # Never ping anyone from model output, even if memory contains "@everyone".
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.settings = settings
        self.messages = messages
        self.summaries = summaries
        self.optouts = optouts
        self.provider = provider
        self.worker = worker
        self.cleaner = cleaner
        self.tree = app_commands.CommandTree(self)
        self._register_commands()

    # -- lifecycle -----------------------------------------------------------

    async def setup_hook(self) -> None:
        if self.settings.dev_guild_id:
            # Guild sync is instant; global sync can take a while to show up.
            guild = discord.Object(id=self.settings.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        self.cleaner.start()
        self.worker.start()

    async def close(self) -> None:
        await self.worker.stop()
        await self.cleaner.stop()
        await super().close()

    async def on_ready(self) -> None:
        log.info(
            "Logged in as %s (id=%s); main model %s/%s",
            self.user,
            self.user.id if self.user else "?",
            self.provider.name,
            self.provider.model,
        )

    async def on_guild_join(self, guild: discord.Guild) -> None:
        # Tell members up front that the bot records conversations.
        channel = guild.system_channel
        if channel is not None and channel.permissions_for(guild.me).send_messages:
            await channel.send(PRIVACY_NOTICE.format(days=self.settings.retention_days))

    # -- recording -----------------------------------------------------------

    def channel_label(self, channel_id: int) -> str:
        channel = self.get_channel(channel_id)
        if channel is None:
            return str(channel_id)
        guild = getattr(channel, "guild", None)
        name = getattr(channel, "name", str(channel_id))
        return f"{guild.name} / #{name}" if guild else f"#{name}"

    @staticmethod
    def _message_text(message: discord.Message) -> str:
        """Message text plus short markers for attachments and stickers."""
        parts = [message.content] if message.content else []
        parts += [f"[attachment: {a.filename}]" for a in message.attachments]
        parts += [f"[sticker: {s.name}]" for s in message.stickers]
        return " ".join(parts).strip()

    def _to_stored(self, message: discord.Message) -> StoredMessage:
        return StoredMessage(
            message_id=message.id,
            guild_id=message.guild.id if message.guild else None,
            channel_id=message.channel.id,
            author_id=message.author.id,
            author_name=message.author.display_name,
            is_bot=message.author.bot,
            content=self._message_text(message),
            created_at=message.created_at,
            day=local_day(message.created_at, self.settings.timezone),
        )

    async def on_message(self, message: discord.Message) -> None:
        # Guild text only; DMs are neither recorded nor answered.
        if message.guild is None or message.type not in (discord.MessageType.default, discord.MessageType.reply):
            return

        if not self.optouts.is_opted_out(message.author.id) and self._message_text(message):
            try:
                await self.messages.add(self._to_stored(message))
            except Exception:
                log.exception("Failed to store message %s", message.id)

        if message.author.bot or self.user is None:
            return
        if self.user in message.mentions:
            await self._reply(message)

    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        # Keep the stored copy in sync, since raw messages are the source of truth.
        content = payload.data.get("content")
        if content is None:
            return
        attachments = payload.data.get("attachments") or []
        text = " ".join([content] + [f"[attachment: {a.get('filename', '')}]" for a in attachments]).strip()
        if not text:
            return
        key = await self.messages.update_content(payload.message_id, text)
        if key is not None:
            await self.summaries.mark_rebuild([key])

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        # Raw events fire even when the message is not in the client cache.
        affected = await self.messages.delete_many([payload.message_id])
        await self.summaries.mark_rebuild(affected)

    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        affected = await self.messages.delete_many(payload.message_ids)
        await self.summaries.mark_rebuild(affected)

    # -- replying ------------------------------------------------------------

    def _question_text(self, message: discord.Message) -> str:
        """Message content with the bot mention removed and other mentions shown as names."""
        assert self.user is not None
        text = re.sub(rf"<@!?{self.user.id}>", "", message.content)
        for user in message.mentions:
            text = re.sub(rf"<@!?{user.id}>", f"@{user.display_name}", text)
        for role in message.role_mentions:
            text = text.replace(f"<@&{role.id}>", f"@{role.name}")
        for channel in message.channel_mentions:
            text = text.replace(f"<#{channel.id}>", f"#{channel.name}")
        return text.strip()

    async def _reply(self, message: discord.Message) -> None:
        assert self.user is not None
        question = self._question_text(message) or "(The user mentioned you without writing a question.)"
        tz = self.settings.timezone
        today = local_day(message.created_at, tz)
        bot_name = message.guild.me.display_name if message.guild else self.user.name

        async with message.channel.typing():
            try:
                summaries = await self.summaries.for_channel(
                    message.channel.id,
                    since_day=window_start(tz, self.settings.summary_lookback_days),
                    until_day=today,
                )
                recent = await self.messages.recent(
                    message.channel.id, self.settings.recent_message_limit, before_id=message.id
                )
                prompt = build_reply_messages(
                    bot_name=bot_name,
                    language=self.settings.language,
                    tz=tz,
                    summaries=summaries,
                    recent=recent,
                    self_id=self.user.id,
                    asker_name=message.author.display_name,
                    question=question,
                )
                answer = await self.provider.chat(prompt)
            except LLMError as exc:
                log.warning("Reply failed: %s", exc)
                answer = "抱歉，模型暫時無法回應，請稍後再試。" if exc.retryable else "抱歉，這個問題無法處理。"
            except Exception:
                log.exception("Reply failed for message %s", message.id)
                answer = "抱歉，處理時發生錯誤。"

        if not answer:
            answer = "（模型沒有回傳內容）"
        chunks = split_for_discord(answer)
        try:
            await message.reply(chunks[0], mention_author=False)
        except discord.HTTPException:
            # The question may have been deleted while the model was working.
            await message.channel.send(chunks[0])
        for chunk in chunks[1:]:
            await message.channel.send(chunk)

    # -- slash commands ------------------------------------------------------

    def _register_commands(self) -> None:
        @self.tree.command(name="optout", description="停止記錄你的訊息，並刪除已記錄的內容")
        async def optout(interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            await self.optouts.opt_out(interaction.user.id)
            affected = await self.messages.delete_by_author(interaction.user.id)
            await self.summaries.mark_rebuild(affected)
            await interaction.followup.send(
                f"已停止記錄你的訊息，並刪除了 {len(affected)} 個頻道日的相關紀錄。相關摘要會重建。",
                ephemeral=True,
            )

        @self.tree.command(name="optin", description="恢復記錄你的訊息")
        async def optin(interaction: discord.Interaction) -> None:
            await self.optouts.opt_in(interaction.user.id)
            await interaction.response.send_message("已恢復記錄你之後的訊息。", ephemeral=True)

        @self.tree.command(name="privacy", description="說明這個機器人如何記錄對話")
        async def privacy(interaction: discord.Interaction) -> None:
            await interaction.response.send_message(
                PRIVACY_NOTICE.format(days=self.settings.retention_days), ephemeral=True
            )


def split_for_discord(text: str, limit: int = DISCORD_MESSAGE_LIMIT) -> list[str]:
    """Split text into Discord-sized chunks, preferring line breaks."""
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks or [""]
