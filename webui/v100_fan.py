"""V100 fan control state reader.

The Tesla V100 fan is governed by the existing `v100-fan.service` (ESP32-based
controller) — NOT by NVML `nvmlDeviceGetFanSpeed()`. WebOllama only DISPLAYS the
actual state of that system; it never controls the fan.

Sources (in priority order):
1. A state/status file if `V100_FAN_STATE_FILE` is configured and exists.
2. `journalctl -u v100-fan.service` — parsed tail, cached and polled at most
   once per POLL_INTERVAL (the service only logs PWM changes, so the last
   matching line is the current state).

Log lines look like:
    V100: 56.0 C Target: 63% PWM: 63%
    V100: 57.0 C Target: 66% PWM: 65%

Parsed state:
    temperature = 56.0
    fan_target   = 63
    fan_pwm      = 63

When neither source is available the reader returns
{"available": false, ...} — never fabricated values.
"""
import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Matches "V100: 56.0 C Target: 63% PWM: 63%" (and minor spacing variants)
LOG_LINE_RE = re.compile(
    r"V100:\s*([\d.]+)\s*C.*?Target:\s*([\d.]+)\s*%?\s*PWM:\s*([\d.]+)\s*%?"
)

STATE_FILE_ENV = "V100_FAN_STATE_FILE"
POLL_INTERVAL_ENV = "V100_FAN_POLL_INTERVAL"
JOURNAL_LINES = 300  # lines to tail from journalctl


def _get_poll_interval() -> float:
    """Get poll interval from env or config with fallback."""
    try:
        from .config import V100_FAN_POLL_INTERVAL
        return V100_FAN_POLL_INTERVAL
    except Exception:
        return float(os.environ.get(POLL_INTERVAL_ENV, "5.0"))


def _get_state_file() -> str:
    try:
        from .config import V100_FAN_STATE_FILE
        return V100_FAN_STATE_FILE
    except Exception:
        return os.environ.get(STATE_FILE_ENV, "")


class V100FanReader:
    def __init__(
        self,
        state_file: str = "",
        poll_interval: float = 0.0,
        journal_lines: int = JOURNAL_LINES,
    ) -> None:
        self.state_file = state_file or _get_state_file()
        self.poll_interval = poll_interval or _get_poll_interval()
        self.journal_lines = journal_lines
        self._last_poll = 0.0
        self._cache: dict = {
            "available": False,
            "reason": "not checked yet",
            "temperature": None,
            "fan_target": None,
            "fan_pwm": None,
            "source": None,
            "ts": None,
        }

    # ---- parsing ------------------------------------------------------------
    @staticmethod
    def parse_line(line: str) -> Optional[dict]:
        """Parse one v100-fan log line.

        "V100: 56.0 C Target: 63% PWM: 63%" →
            {"temperature": 56.0, "fan_target": 63, "fan_pwm": 63}
        """
        if not line:
            return None
        m = LOG_LINE_RE.search(line)
        if not m:
            return None
        try:
            return {
                "temperature": float(m.group(1)),
                "fan_target": int(float(m.group(2))),
                "fan_pwm": int(float(m.group(3))),
            }
        except (TypeError, ValueError):
            return None

    def _parse_state_text(self, text: str) -> Optional[dict]:
        """Parse state from a file: try log lines first, then JSON."""
        # log-format lines
        state = None
        for line in text.splitlines():
            parsed = self.parse_line(line)
            if parsed:
                state = parsed
        if state:
            return state
        # JSON (e.g. {"temperature": 56, "fan_target": 63, "fan_pwm": 63})
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                keys = {"temperature", "fan_target", "fan_pwm"}
                if keys.issubset(obj):
                    return {
                        "temperature": float(obj["temperature"]),
                        "fan_target": int(obj["fan_target"]),
                        "fan_pwm": int(obj["fan_pwm"]),
                    }
        except (ValueError, TypeError):
            pass
        return None

    # ---- sources --------------------------------------------------------------
    @staticmethod
    def _has_group(group_name: str) -> bool:
        """Check supplementary-group membership via /proc/self/status."""
        try:
            import grp
            gid = grp.getgrnam(group_name).gr_gid
            for line in Path("/proc/self/status").read_text(errors="replace").splitlines():
                if line.startswith("Groups:"):
                    return gid in {int(x) for x in line.split(":", 1)[1].split()}
        except Exception:
            pass
        return False

    async def read_state(self, force: bool = False) -> dict:
        """Return the current fan state (cached between polls)."""
        now = time.monotonic()
        if not force and now - self._last_poll < self.poll_interval:
            return self._cache

        # 1) state file (if configured)
        if self.state_file:
            try:
                path = Path(self.state_file)
                if path.exists():
                    text = path.read_text(errors="replace")
                    parsed = self._parse_state_text(text)
                    if parsed:
                        self._cache = {
                            "available": True,
                            "reason": None,
                            **parsed,
                            "source": "state-file",
                            "ts": time.time(),
                        }
                        self._last_poll = now
                        return self._cache
            except Exception as exc:
                logger.debug("v100-fan state file read failed: %s", exc)

        # 2) journalctl (cached, single subprocess per poll)
        try:
            lines = await self._journal_tail()
            state = None
            for line in lines:
                parsed = self.parse_line(line)
                if parsed:
                    state = parsed  # last matching line = current state
            if state is not None:
                self._cache = {
                    "available": True,
                    "reason": None,
                    **state,
                    "source": "journalctl",
                    "ts": time.time(),
                }
            else:
                reason = "no v100-fan state lines in journal"
                # root can always read the journal; a non-root user needs the
                # systemd-journal group to see other services' logs
                if os.geteuid() != 0 and not self._has_group("systemd-journal"):
                    reason += (
                        " (WebOllama user is not in the 'systemd-journal' group — "
                        "journal may be unreadable; run: usermod -aG systemd-journal "
                        "ollama-web, then restart ollama-web)"
                    )
                self._cache = {
                    "available": False,
                    "reason": reason,
                    "temperature": None,
                    "fan_target": None,
                    "fan_pwm": None,
                    "source": "journalctl",
                    "ts": None,
                }
        except Exception as exc:
            self._cache = {
                "available": False,
                "reason": f"journalctl unavailable: {exc}",
                "temperature": None,
                "fan_target": None,
                "fan_pwm": None,
                "source": None,
                "ts": None,
            }
        self._last_poll = now
        return self._cache

    async def _journal_tail(self) -> list[str]:
        proc = await asyncio.create_subprocess_exec(
            "journalctl", "-u", "v100-fan.service", "--no-pager",
            "-n", str(self.journal_lines),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError("journalctl timed out")
        stderr_text = stderr.decode(errors="replace")
        if proc.returncode not in (0, 1):
            raise RuntimeError(f"journalctl exited {proc.returncode}: {stderr_text[:200]}")
        # journald hints about restricted access are printed to stderr even on
        # exit code 0/1 when the caller lacks the systemd-journal group
        if "systemd-journal" in stderr_text or "Permission denied" in stderr_text:
            raise RuntimeError(
                "permission denied reading journal — add the WebOllama user to "
                "the 'systemd-journal' group (e.g. 'usermod -aG systemd-journal "
                "ollama-web' + restart the service)"
            )
        return stdout.decode(errors="replace").splitlines()


# singleton
_reader: Optional[V100FanReader] = None


def get_v100_fan_reader() -> V100FanReader:
    global _reader
    if _reader is None:
        _reader = V100FanReader()
    return _reader


def set_v100_fan_reader(reader: V100FanReader) -> None:
    global _reader
    _reader = reader
