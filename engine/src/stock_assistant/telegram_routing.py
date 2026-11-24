"""Telegram destinations shared by the paper service and scheduled research."""
from dataclasses import dataclass, field
from enum import StrEnum
import re


class BotChannel(StrEnum):
    DAYTRADE = 'DAYTRADE'
    SWING = 'SWING'
    LONGTERM = 'LONGTERM'


_HEADERS = {
    BotChannel.DAYTRADE: '[단타]',
    BotChannel.SWING: '[스윙]',
    BotChannel.LONGTERM: '[장기]',
}


def channel_header(bot_channel):
    return _HEADERS[BotChannel(bot_channel)]


def with_channel_header(text, bot_channel):
    header = channel_header(bot_channel)
    first, _, rest = text.lstrip('\r\n').partition('\n')
    body = rest if first.rstrip('\r') in _HEADERS.values() else text.lstrip('\r\n')
    return f'{header}\n{body}'


@dataclass(frozen=True)
class TelegramRoute:
    bot_channel: BotChannel
    chat_id: str = field(repr=False)
    message_thread_id: int | None = field(repr=False)
    dedicated_chat: bool


def _channel_overrides(bot_channel, env):
    prefix = f'TELEGRAM_{BotChannel(bot_channel)}'
    return tuple((env.get(f'{prefix}_{key}') or '').strip() for key in ('CHAT_ID', 'MESSAGE_THREAD_ID'))


def resolve_telegram_route(bot_channel, env, *, default_chat_id=None):
    bot_channel = BotChannel(bot_channel)
    dedicated_chat, thread = _channel_overrides(bot_channel, env)
    chat = dedicated_chat or (default_chat_id or env.get('TELEGRAM_HOME_CHANNEL') or '').strip()
    if not re.fullmatch(r'-?[1-9][0-9]*', chat):
        raise ValueError('TELEGRAM_CHAT_ID_INVALID')
    if thread and not re.fullmatch(r'[1-9][0-9]*', thread):
        raise ValueError('TELEGRAM_MESSAGE_THREAD_ID_INVALID')
    return TelegramRoute(bot_channel, chat, int(thread) if thread else None, bool(dedicated_chat))


def longterm_cron_target(env):
    """Keep Hermes responsible for sending and handling MEDIA attachments."""
    if not any(_channel_overrides(BotChannel.LONGTERM, env)):
        return 'telegram'
    route = resolve_telegram_route(BotChannel.LONGTERM, env)
    target = f'telegram:{route.chat_id}'
    if route.message_thread_id is not None:
        target += f':{route.message_thread_id}'
    return target
