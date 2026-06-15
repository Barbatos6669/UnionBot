import os
import sys
import discord


# ── Log rotation config ─────────────────────────────────────────────────────
_LOG_PATH = "data/bot.log"
_CONN_LOG_PATH = "data/connection.log"
_MAX_BYTES = 5 * 1024 * 1024   # 5 MB per file
_BACKUP_COUNT = 3              # keep bot.log + bot.log.1 .. bot.log.3


def _get_timestamp() -> str:
    return discord.utils.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def _safe_print(line: str) -> None:
    """Print without crashing on Windows consoles that use cp1252."""
    try:
        print(line)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "utf-8"
        sys.stdout.write(line.encode(enc, errors="replace").decode(enc, errors="replace") + "\n")
        sys.stdout.flush()


def _rotate_if_needed(path: str) -> None:
    """Rotate ``path`` once it exceeds _MAX_BYTES. Best-effort; never raises."""
    try:
        if not os.path.exists(path):
            return
        if os.path.getsize(path) < _MAX_BYTES:
            return
        for i in range(_BACKUP_COUNT, 0, -1):
            src = path if i == 1 else f"{path}.{i - 1}"
            dst = f"{path}.{i}"
            if os.path.exists(src):
                if os.path.exists(dst):
                    os.remove(dst)
                os.replace(src, dst)
    except OSError:
        pass


def _write_to(path: str, line: str) -> None:
    try:
        _rotate_if_needed(path)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        # Never let a logging failure crash the bot.
        pass


def _log(level: str, message: str) -> None:
    timestamp = _get_timestamp()
    prefix = f"{level}: " if level else ""
    line = f"[{timestamp}] {prefix}{message}"
    _safe_print(line)
    _write_to(_LOG_PATH, line)


def connection_log(event: str, message: str = "") -> None:
    """Write a connection-lifecycle event to ``data/connection.log`` AND the main log.

    ``event`` is a short tag like ``CONNECT``, ``DISCONNECT``, ``RESUMED``,
    ``READY``, ``SHUTDOWN``. ``message`` is optional extra context (e.g.
    outage duration). Connection events also go to the main log so timelines
    stay correlated.
    """
    timestamp = _get_timestamp()
    suffix = f" — {message}" if message else ""
    line = f"[{timestamp}] {event}{suffix}"
    _safe_print(line)
    _write_to(_CONN_LOG_PATH, line)
    _write_to(_LOG_PATH, f"[{timestamp}] CONN: {event}{suffix}")


def trace_log(message: str) -> None:    _log("", message)
def debug_log(message: str) -> None:    _log("DEBUG", message)
def info_log(message: str) -> None:     _log("INFO", message)
def warning_log(message: str) -> None:  _log("WARNING", message)
def error_log(message: str) -> None:    _log("ERROR", message)
def critical_log(message: str) -> None: _log("CRITICAL", message)

