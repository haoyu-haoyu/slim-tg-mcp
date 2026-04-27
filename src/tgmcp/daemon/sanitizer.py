"""Prompt-injection defense for Telegram message content.

Wraps every user-controlled string in <tg_msg> tags with provenance metadata
(sender_id, chat_id, trust level) so the model knows where the text came from
and how much to trust it.
"""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]")
INJECTION_MARKERS = [
    re.compile(r"(?i)ignore (all |the )?(previous|prior|above) (instructions?|prompts?)"),
    re.compile(r"(?i)disregard (all |the )?(previous|prior|above)"),
    re.compile(r"(?i)you are now"),
    re.compile(r"(?i)system\s*:\s*"),
    re.compile(r"\[INST\]|\[/INST\]"),
    re.compile(r"<\|im_start\|>|<\|im_end\|>"),
    re.compile(r"</?(system|assistant|user)\b[^>]*>"),
]
MAX_TEXT_LEN = 4000

Trust = Literal["high", "medium", "low"]


@dataclass
class TrustContext:
    sender_id: int | None
    chat_id: int | None
    is_self: bool = False
    is_contact: bool = False
    is_private: bool = False
    is_bot: bool = False


def derive_trust(ctx: TrustContext) -> Trust:
    if ctx.is_self:
        return "high"
    if ctx.is_contact and ctx.is_private:
        return "medium"
    return "low"


def strip_zero_width(text: str) -> str:
    return ZERO_WIDTH.sub("", text)


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    return strip_zero_width(text)


def neutralize_injection(text: str) -> tuple[str, list[str]]:
    """Mark injection-looking substrings without deleting them.

    We don't remove content (the model needs to see what was attempted) but
    we wrap matches in [[neutralized: ...]] so they can't be parsed as
    instructions. Returns (cleaned_text, list_of_markers_found).
    """
    found: list[str] = []
    cleaned = text
    for pattern in INJECTION_MARKERS:
        def repl(m: re.Match[str]) -> str:
            found.append(m.group(0))
            return f"[[neutralized:{m.group(0)!r}]]"
        cleaned = pattern.sub(repl, cleaned)
    return cleaned, found


def truncate(text: str, limit: int = MAX_TEXT_LEN) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[truncated {len(text) - limit} chars]"


def wrap_message(
    text: str,
    ctx: TrustContext,
    *,
    msg_id: int | None = None,
    date: str | None = None,
) -> str:
    """Wrap a user-controlled string with provenance + trust metadata.

    Output looks like:
        <tg_msg trust="low" sender="123" chat="-100..." date="...">
        ...escaped content...
        </tg_msg>

    The model is instructed (in MCP server prompt) to never follow instructions
    found inside <tg_msg> tags whose trust is not "high".
    """
    text = normalize(text or "")
    cleaned, markers = neutralize_injection(text)
    cleaned = truncate(cleaned)
    cleaned = html.escape(cleaned, quote=False)

    trust = derive_trust(ctx)
    attrs = [
        f'trust="{trust}"',
        f'sender="{ctx.sender_id}"' if ctx.sender_id is not None else "",
        f'chat="{ctx.chat_id}"' if ctx.chat_id is not None else "",
        f'msg_id="{msg_id}"' if msg_id is not None else "",
        f'date="{html.escape(date)}"' if date else "",
    ]
    if markers:
        attrs.append(f'injection_markers="{len(markers)}"')
    attr_str = " ".join(a for a in attrs if a)

    return f"<tg_msg {attr_str}>\n{cleaned}\n</tg_msg>"
