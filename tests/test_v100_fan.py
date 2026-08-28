"""Tests for the V100 fan control reader (v100-fan.service parser)."""
import asyncio

import pytest

from webui.v100_fan import V100FanReader


# --------------------------------------------------------------------------- #
#  Parser tests (exact requirements)
# --------------------------------------------------------------------------- #
def test_parse_line_basic():
    r = V100FanReader.parse_line("V100: 56.0 C Target: 63% PWM: 63%")
    assert r == {"temperature": 56.0, "fan_target": 63, "fan_pwm": 63}


def test_parse_line_second_sample():
    r = V100FanReader.parse_line("V100: 58.0 C Target: 68% PWM: 67%")
    assert r == {"temperature": 58.0, "fan_target": 68, "fan_pwm": 67}


def test_parse_line_spacing_variants():
    assert V100FanReader.parse_line("V100: 57.0 C Target: 66% PWM: 65%") == {
        "temperature": 57.0, "fan_target": 66, "fan_pwm": 65}
    assert V100FanReader.parse_line("V100:57.0C Target:66% PWM:65%") == {
        "temperature": 57.0, "fan_target": 66, "fan_pwm": 65}


def test_parse_line_no_match():
    assert V100FanReader.parse_line("") is None
    assert V100FanReader.parse_line("hello world") is None
    assert V100FanReader.parse_line("V100: abc C Target: xx% PWM: yy%") is None


def test_parse_last_line_wins():
    """The service logs only PWM changes — the last matching line is current."""
    text = """\
V100: 56.0 C Target: 63% PWM: 63%
V100: 57.0 C Target: 66% PWM: 65%
V100: 58.0 C Target: 68% PWM: 67%
"""
    state = None
    for line in text.splitlines():
        p = V100FanReader.parse_line(line)
        if p:
            state = p
    assert state == {"temperature": 58.0, "fan_target": 68, "fan_pwm": 67}


# --------------------------------------------------------------------------- #
#  Reader tests (state file + journalctl)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_reader_state_file(tmp_path):
    f = tmp_path / "fan_state"
    f.write_text("V100: 56.0 C Target: 63% PWM: 63%\n")
    reader = V100FanReader(state_file=str(f), poll_interval=0)
    state = await reader.read_state(force=True)
    assert state["available"] is True
    assert state["source"] == "state-file"
    assert state["fan_target"] == 63
    assert state["fan_pwm"] == 63
    assert state["temperature"] == 56.0


@pytest.mark.asyncio
async def test_reader_state_file_json(tmp_path):
    f = tmp_path / "fan_state"
    f.write_text('{"temperature": 57.0, "fan_target": 66, "fan_pwm": 65}')
    reader = V100FanReader(state_file=str(f), poll_interval=0)
    state = await reader.read_state(force=True)
    assert state["available"] is True
    assert state["fan_target"] == 66
    assert state["fan_pwm"] == 65


@pytest.mark.asyncio
async def test_reader_journalctl(monkeypatch):
    reader = V100FanReader(poll_interval=0)

    async def fake_journal():
        return ["V100: 56.0 C Target: 63% PWM: 63%",
                "V100: 57.0 C Target: 66% PWM: 65%"]

    monkeypatch.setattr(reader, "_journal_tail", fake_journal)
    state = await reader.read_state(force=True)
    assert state["available"] is True
    assert state["source"] == "journalctl"
    assert state["fan_target"] == 66  # last line wins
    assert state["fan_pwm"] == 65


@pytest.mark.asyncio
async def test_reader_journalctl_unavailable(monkeypatch):
    reader = V100FanReader(poll_interval=0)

    async def bad_journal():
        raise RuntimeError("unit v100-fan.service not found")

    monkeypatch.setattr(reader, "_journal_tail", bad_journal)
    state = await reader.read_state(force=True)
    assert state["available"] is False
    assert state["fan_target"] is None
    assert state["fan_pwm"] is None
    assert "not found" in state["reason"]


@pytest.mark.asyncio
async def test_reader_cache_no_repeat(monkeypatch):
    """Reading again within the poll interval must NOT re-run journalctl."""
    reader = V100FanReader(poll_interval=5.0)
    calls = {"n": 0}

    async def fake_journal():
        calls["n"] += 1
        return ["V100: 56.0 C Target: 63% PWM: 63%"]

    monkeypatch.setattr(reader, "_journal_tail", fake_journal)
    await reader.read_state(force=True)
    await reader.read_state()  # cached — should not call again
    assert calls["n"] == 1
    assert reader._cache["available"] is True


# --------------------------------------------------------------------------- #
#  Permission / journal-access diagnostics
# --------------------------------------------------------------------------- #
def test_has_group(monkeypatch):
    reader = V100FanReader()
    # simulate supplementary groups listing
    import webui.v100_fan as vf

    def fake_read_text(_self, *a, **kw):
        return "Name:\ttest\nGroups:\t1 999 1000\nUid:\t0"
    monkeypatch.setattr(vf.Path, "read_text", fake_read_text)
    monkeypatch.setattr("os.geteuid", lambda: 0)
    import grp as grp_mod
    monkeypatch.setattr(grp_mod, "getgrnam", lambda name: type("g", (), {"gr_gid": 999})())
    assert reader._has_group("systemd-journal") is True


@pytest.mark.asyncio
async def test_reader_journal_permission_denied(monkeypatch):
    """journalctl permission hint → clear actionable reason."""
    reader = V100FanReader(poll_interval=0)

    async def denied_journal():
        raise RuntimeError(
            "permission denied reading journal — add the WebOllama user to "
            "the 'systemd-journal' group"
        )

    monkeypatch.setattr(reader, "_journal_tail", denied_journal)
    state = await reader.read_state(force=True)
    assert state["available"] is False
    assert "systemd-journal" in state["reason"]


@pytest.mark.asyncio
async def test_reader_group_hint_when_no_lines(monkeypatch):
    """No state lines + user not in systemd-journal group → hint in reason."""
    reader = V100FanReader(poll_interval=0)
    monkeypatch.setattr(reader, "_has_group", lambda g: False)
    monkeypatch.setattr("os.geteuid", lambda: 1000)  # non-root

    async def empty_journal():
        return []

    monkeypatch.setattr(reader, "_journal_tail", empty_journal)
    state = await reader.read_state(force=True)
    assert state["available"] is False
    assert "systemd-journal" in state["reason"]
