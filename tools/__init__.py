"""Tool implementations split by domain.

Importing this package triggers ``@tool`` registration side-effects in every
submodule. ``bot.py`` does ``import tools`` at runtime in ``_async_main()`` to
populate the global ``registry.TOOLS`` dict before any provider runs.
"""

from .dice import roll_die
from .escalation import escalate_to_big_model
from .heartbeat_tools import (
    cancel_heartbeat_action,
    create_heartbeat_action,
    list_heartbeat_actions,
)
from .mail import (
    add_email_label,
    check_new_mail,
    list_email_labels,
    list_emails,
    mark_emails_reported,
    read_email,
    remove_email_label,
)
from .manual import read_manual
from .memory_tools import (
    add_user_fact,
    add_user_instruction,
    forget_user_memory,
    get_user_memory,
    set_user_preference,
)
from .search import read_note, search_vault
from .trains import train_departures
from .weather import weather
from .wilma import find_wilma_message, list_wilma_messages, read_wilma_message

__all__ = [
    "add_email_label",
    "add_user_fact",
    "add_user_instruction",
    "cancel_heartbeat_action",
    "check_new_mail",
    "create_heartbeat_action",
    "escalate_to_big_model",
    "find_wilma_message",
    "forget_user_memory",
    "get_user_memory",
    "list_email_labels",
    "list_emails",
    "list_heartbeat_actions",
    "list_wilma_messages",
    "mark_emails_reported",
    "read_email",
    "read_manual",
    "read_note",
    "read_wilma_message",
    "remove_email_label",
    "roll_die",
    "search_vault",
    "set_user_preference",
    "train_departures",
    "weather",
]
