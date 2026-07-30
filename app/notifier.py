import logging
from abc import ABC, abstractmethod

import httpx

logger = logging.getLogger(__name__)


class Notifier(ABC):
    """Base class for notifiers."""

    MAX_MESSAGE_LENGTH = 4000

    @abstractmethod
    def send(self, text: str) -> None:
        ...

    def send_highlight_card(self, highlights: list) -> bool:
        return False

    def send_long(self, text: str) -> None:
        """Send text, splitting into multiple messages if too long.

        Each chunk retains the digest header and a X/Y part indicator.
        Splitting occurs at line boundaries to avoid cutting titles
        or URLs mid-string.
        """
        if len(text) <= self.MAX_MESSAGE_LENGTH:
            self.send(text)
            return

        lines = text.split("\n")
        header = lines[0] if lines else "【台湾新闻监测】"

        # Split body lines into chunks
        chunks: list[list[str]] = []
        current: list[str] = []
        cur_len = 0
        # Reserve ~60 chars for the part header
        reserve = len(header) + 60

        for line in lines[1:]:
            line_len = len(line) + 1
            if cur_len + line_len > self.MAX_MESSAGE_LENGTH - reserve and current:
                chunks.append(current)
                current = []
                cur_len = 0
            # If a single line exceeds the limit on its own, truncate it
            if line_len > self.MAX_MESSAGE_LENGTH - reserve:
                truncated = self._truncate_line(line, self.MAX_MESSAGE_LENGTH - reserve)
                current.append(truncated)
                cur_len += len(truncated) + 1
            else:
                current.append(line)
                cur_len += line_len

        if current:
            chunks.append(current)

        for i, chunk_lines in enumerate(chunks):
            prefix = f"{header}\n第{i + 1}/{len(chunks)}部分\n\n"
            self.send(prefix + "\n".join(chunk_lines))

    @staticmethod
    def _truncate_line(line: str, max_len: int) -> str:
        """Truncate a single long line, preserving the URL if present."""
        if len(line) <= max_len:
            return line
        url_pos = line.find("http")
        if url_pos >= 0:
            url_part = line[url_pos:]
            # If URL alone fits within max_len, prefix it with indicator
            if len(url_part) < max_len - 10:
                prefix_len = max_len - len(url_part) - 4
                if prefix_len > 0:
                    return line[:prefix_len] + "..." + url_part
            # URL too long, truncate it too
            return url_part[:max_len - 3] + "..."
        return line[:max_len - 3] + "..."


class ConsoleNotifier(Notifier):
    """Print digest to console (no size limit, no splitting)."""

    MAX_MESSAGE_LENGTH = 10_000_000  # effectively unlimited

    def send(self, text: str) -> None:
        try:
            print(text)
        except UnicodeEncodeError:
            _sys.stdout.buffer.write(text.encode("utf-8"))
            _sys.stdout.buffer.write(b"\n")
            _sys.stdout.buffer.flush()
            logger.warning(
                "Console UTF-8 fallback (stdout encoding=%s)",
                _sys.stdout.encoding,
            )


class FeishuNotifier(Notifier):
    """Send digest to Feishu webhook."""

    MAX_MESSAGE_LENGTH = 30000

    def __init__(self, webhook_url: str, app_id: str = "", app_secret: str = "", chat_id: str = ""):
        if not webhook_url:
            raise ValueError("FEISHU_WEBHOOK_URL is not set")
        self._webhook_url = webhook_url
        self._app_id = app_id
        self._app_secret = app_secret
        self._chat_id = chat_id

    def send(self, text: str) -> None:
        payload = {
            "msg_type": "text",
            "content": {"text": text},
        }
        try:
            resp = httpx.post(
                self._webhook_url,
                json=payload,
                timeout=10,
            )
            resp.raise_for_status()
            logger.info("Feishu notification sent (length=%d)", len(text))
        except httpx.HTTPError as e:
            logger.error("Feishu send failed: %s", e)

    def send_highlight_card(self, highlights: list) -> bool:
        """Send an interactive highlight card to Feishu group chat.

        Args:
            highlights: list of (article, ImportanceResult) tuples sorted
                        by the shared select_highlights() function.

        Returns:
            True if card was sent successfully, False otherwise.
        """
        if not self._app_id or not self._app_secret or not self._chat_id:
            logger.warning("Feishu credentials not configured, skipping highlight card")
            return False

        critical_count = sum(1 for _, r in highlights if r.level == "critical")
        important_count = sum(1 for _, r in highlights if r.level == "important")
        total = len(highlights)
        has_critical = critical_count > 0

        # Build card JSON
        title_text = "????????"
        header_template = "red" if has_critical else "orange"
        item_lines = []

        for article, result in highlights:
            pfx = "????" if result.level == "critical" else "????"
            safe_title = article.title.replace("\n", " ").replace("\r", "")
            item_lines.append(pfx + safe_title)

        content_md = "\n".join(item_lines)

        # Bottom summary
        summary_parts = []
        if critical_count > 0:
            summary_parts.append(f"\u91cd\u5927{critical_count}\u6761")
        if important_count > 0:
            summary_parts.append(f"\u91cd\u70b9{important_count}\u6761")
        summary_line = "\u3001".join(summary_parts) + "\uff0c\u8be6\u60c5\u89c1\u672c\u671fWord\u7b80\u62a5\u3002"

        # Build Feishu interactive card structure
        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title_text},
                "template": header_template,
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": content_md},
                },
                {"tag": "hr"},
                {
                    "tag": "note",
                    "elements": [
                        {"tag": "plain_text", "content": summary_line}
                    ],
                },
            ],
        }

        try:
            from .feishu import send_card as _send_card
            _send_card(card, self._app_id, self._app_secret, self._chat_id)
            logger.info("Highlight card sent (critical=%d, important=%d)", critical_count, important_count)
            return True
        except Exception as e:
            logger.error("Highlight card send failed: %s", e)
            return False


class TelegramNotifier(Notifier):
    """Send digest to Telegram bot."""

    MAX_MESSAGE_LENGTH = 4000

    def __init__(self, bot_token: str, chat_id: str):
        if not bot_token or not chat_id:
            raise ValueError("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is not set")
        self._chat_id = chat_id
        self._api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    def send(self, text: str) -> None:
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        try:
            resp = httpx.post(
                self._api_url,
                json=payload,
                timeout=10,
            )
            resp.raise_for_status()
            logger.info("Telegram notification sent (length=%d)", len(text))
        except httpx.HTTPError as e:
            logger.error("Telegram send failed: %s", e)


def create_notifier() -> Notifier:
    """Factory to create notifier based on env config.

    Raises SystemExit with a clear message if feishu or telegram is
    selected but required env vars are missing.
    """
    import os

    from dotenv import load_dotenv

    load_dotenv()

    notifier_type = os.getenv("NOTIFIER", "console").strip().lower()

    if notifier_type == "feishu":
        webhook = os.getenv("FEISHU_WEBHOOK_URL", "").strip()
        if not webhook:
            logger.error(
                "NOTIFIER=feishu but FEISHU_WEBHOOK_URL is not set. "
                "Set FEISHU_WEBHOOK_URL in .env or change NOTIFIER=console."
            )
            raise SystemExit(1)
        fs_id = os.getenv("FEISHU_APP_ID", "").strip()
        fs_secret = os.getenv("FEISHU_APP_SECRET", "").strip()
        fs_chat = os.getenv("FEISHU_CHAT_ID", "").strip()
        return FeishuNotifier(webhook, app_id=fs_id, app_secret=fs_secret, chat_id=fs_chat)

    if notifier_type == "telegram":
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if not token or not chat_id:
            logger.error(
                "NOTIFIER=telegram but TELEGRAM_BOT_TOKEN or "
                "TELEGRAM_CHAT_ID is not set. "
                "Set both in .env or change NOTIFIER=console."
            )
            raise SystemExit(1)
        return TelegramNotifier(token, chat_id)

    # Default: console
    return ConsoleNotifier()
