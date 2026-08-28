"""Ollama Console — safe whitelisted command execution.

Two execution backends:
1. Local CLI (subprocess, array args, no shell) — used only when an `ollama`
   binary exists and the configured OLLAMA_URL points at localhost.
2. API translation — every whitelisted command is mapped to the native Ollama
   HTTP API. Used when the Ollama binary is not available locally or the API
   URL points to a remote host.

The command whitelist is strict: only the commands below are accepted, and
model names are validated against a safe pattern. No shell, no arbitrary args.
"""
import asyncio
import logging
import re
import shutil
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional

from .config import OLLAMA_URL
from .ollama_client import OllamaClient, OllamaError, ProgressEvent

logger = logging.getLogger(__name__)

# Allowed commands (mirrors `ollama --help` where relevant)
ALLOWED_COMMANDS = {
    "list": "List local models",
    "ps": "Show running models",
    "show": "Show model details",
    "pull": "Pull a model",
    "push": "Push a model to a registry",
    "create": "Create a model from a Modelfile",
    "cp": "Copy a model",
    "rm": "Remove a model",
    "stop": "Stop a running model",
    "version": "Show version",
    "help": "Show help",
}

# Validation: model names/tags like "qwen3:8b", "user/model:tag", "user/model"
MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-/]*:[A-Za-z0-9_.\-]+$|^[A-Za-z0-9][A-Za-z0-9_.\-/]*$")
# Restrict source paths for `create -f` to be safe (not used remotely)
FORBIDDEN_CHARS = set(";|&`$<>\\\n\r\t\"'(){}[]")


class ConsoleError(Exception):
    pass


@dataclass
class ConsoleResult:
    command: str
    output: str = ""
    error: str = ""
    exit_code: int = 0
    duration: float = 0.0
    api: bool = False
    job_id: Optional[str] = None


def validate_model_name(name: str) -> str:
    name = name.strip()
    if not name or len(name) > 200:
        raise ConsoleError("invalid model name")
    if any(ch in FORBIDDEN_CHARS for ch in name):
        raise ConsoleError("invalid characters in model name")
    if not MODEL_NAME_RE.match(name):
        raise ConsoleError(f"invalid model name format: {name!r}")
    return name


def parse_command(text: str) -> tuple[str, list[str]]:
    """Parse 'ollama <cmd> [args...]' into (cmd, args). Rejects unknown tokens."""
    # reject raw shell metacharacters (including newlines that could smuggle
    # a second command)
    if any(ch in FORBIDDEN_CHARS for ch in text):
        raise ConsoleError("forbidden characters in command")
    tokens = text.strip().split()
    if not tokens:
        raise ConsoleError("empty command")
    # allow leading "ollama"
    if tokens[0] == "ollama":
        tokens = tokens[1:]
    if not tokens:
        raise ConsoleError("missing command (try 'ollama help')")
    cmd = tokens[0]
    if cmd not in ALLOWED_COMMANDS:
        raise ConsoleError(f"command not allowed: {cmd!r}. Allowed: {', '.join(sorted(ALLOWED_COMMANDS))}")
    # no shell metacharacters anywhere in the args
    for tok in tokens[1:]:
        if any(ch in FORBIDDEN_CHARS for ch in tok):
            raise ConsoleError(f"forbidden characters in argument: {tok!r}")
    return cmd, tokens[1:]


class OllamaConsole:
    def __init__(self, client: OllamaClient) -> None:
        self.client = client
        self._local_binary: Optional[str] = None
        self._prefer_local = self._detect_local()
        # job submission hook, set by main app
        self.submit_job = None

    def _detect_local(self) -> bool:
        # local CLI only when the API is localhost too
        from urllib.parse import urlparse

        host = urlparse(OLLAMA_URL).hostname
        self._local_binary = shutil.which("ollama")
        return bool(self._local_binary) and host in ("localhost", "127.0.0.1", "0.0.0.0", None)

    async def run(self, text: str) -> ConsoleResult:
        start = time.time()
        try:
            cmd, args = parse_command(text)
        except ConsoleError as exc:
            return ConsoleResult(command=text.strip(), error=str(exc), exit_code=2, duration=time.time() - start)

        # ---- local CLI path -------------------------------------------------------
        if self._prefer_local:
            return await self._run_local(cmd, args, start)

        # ---- API translation path ---------------------------------------------------
        try:
            if cmd == "help":
                return ConsoleResult(command=text.strip(), output=self._help(), api=True, duration=time.time() - start)
            if cmd == "version":
                data = await self.client.version()
                return ConsoleResult(command=text.strip(), output=data.get("version", ""), api=True, duration=time.time() - start)
            if cmd == "list":
                models = await self.client.tags()
                lines = ["NAME\tID\tSIZE\tMODIFIED"]
                for m in models:
                    lines.append(f"{m['name']}\t{m.get('digest','')[:12]}\t{_fmt_bytes(m.get('size',0))}\t{m.get('modified_at','')}")
                return ConsoleResult(command=text.strip(), output="\n".join(lines), api=True, duration=time.time() - start)
            if cmd == "ps":
                running = await self.client.running_models()
                if not running:
                    return ConsoleResult(command=text.strip(), output="no models loaded", api=True, duration=time.time() - start)
                lines = ["NAME\tSIZE\tVRAM\tUNTIL"]
                for m in running:
                    lines.append(f"{m.get('name','')}\t{_fmt_bytes(m.get('size',0))}\t{_fmt_bytes(m.get('size_vram',0))}\t{m.get('expires_at','')}")
                return ConsoleResult(command=text.strip(), output="\n".join(lines), api=True, duration=time.time() - start)
            if cmd in ("show", "pull", "push", "create", "cp", "rm", "stop"):
                # these map to API calls; streamed/job-like ops go through jobs
                return await self._api_operation(cmd, args, text.strip(), start)
        except OllamaError as exc:
            return ConsoleResult(command=text.strip(), error=exc.message, exit_code=1, api=True, duration=time.time() - start)
        except ConsoleError as exc:
            return ConsoleResult(command=text.strip(), error=str(exc), exit_code=2, api=True, duration=time.time() - start)
        return ConsoleResult(command=text.strip(), error="unhandled", exit_code=1, api=True, duration=time.time() - start)

    # ---- local subprocess ----------------------------------------------------------
    async def _run_local(self, cmd: str, args: list[str], start: float) -> ConsoleResult:
        full = [self._local_binary, cmd] + args
        logger.info("console local: %s", " ".join(full))
        proc = await asyncio.create_subprocess_exec(
            *full,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=900)
        except asyncio.TimeoutError:
            proc.kill()
            return ConsoleResult(command="ollama " + " ".join(full[1:]), error="timed out", exit_code=124, duration=time.time() - start)
        return ConsoleResult(
            command="ollama " + " ".join(full[1:]),
            output=stdout.decode(errors="replace"),
            error=stderr.decode(errors="replace"),
            exit_code=proc.returncode or 0,
            duration=time.time() - start,
            api=False,
        )

    # ---- API operations -------------------------------------------------------------
    async def _api_operation(self, cmd: str, args: list[str], text: str, start: float) -> ConsoleResult:
        if cmd == "show":
            if not args:
                raise ConsoleError("usage: ollama show <model>")
            name = validate_model_name(args[0])
            data = await self.client.show(name)
            details = data.get("details", {})
            out = [
                f"Name:            {data.get('model', '')}",
                f"Family:          {details.get('family', '')}",
                f"Parameter size:  {details.get('parameter_size', '')}",
                f"Quantization:    {details.get('quantization_level', '')}",
                f"Format:          {details.get('format', '')}",
                f"Parent model:    {details.get('parent_model', '') or '-'}",
                f"Capabilities:    {', '.join(data.get('capabilities', []) or [])}",
                f"License:         {(data.get('license', '') or '')[:200]}",
            ]
            return ConsoleResult(command=text, output="\n".join(out), api=True, duration=time.time() - start)
        if cmd == "cp":
            if len(args) != 2:
                raise ConsoleError("usage: ollama cp <source> <destination>")
            src = validate_model_name(args[0])
            dst = validate_model_name(args[1])
            await self.client.copy(src, dst)
            return ConsoleResult(command=text, output=f"copied {src} -> {dst}", api=True, duration=time.time() - start)
        if cmd == "rm":
            if not args:
                raise ConsoleError("usage: ollama rm <model>")
            name = validate_model_name(args[0])
            await self.client.delete(name)
            return ConsoleResult(command=text, output=f"deleted {name}", api=True, duration=time.time() - start)
        if cmd == "stop":
            if not args:
                raise ConsoleError("usage: ollama stop <model>")
            name = validate_model_name(args[0])
            await self.client.stop(name)
            return ConsoleResult(command=text, output=f"stopped {name}", api=True, duration=time.time() - start)
        if cmd == "pull":
            if not args:
                raise ConsoleError("usage: ollama pull <model>")
            name = validate_model_name(args[0])
            if self.submit_job:
                job = await self.submit_job("pull", name)
                return ConsoleResult(command=text, output=f"pull started as job {job.id}", api=True, job_id=job.id, duration=time.time() - start)
            await self.client.pull(name, lambda e: None)
            return ConsoleResult(command=text, output="pull complete", api=True, duration=time.time() - start)
        if cmd == "push":
            if not args:
                raise ConsoleError("usage: ollama push <model>")
            name = validate_model_name(args[0])
            if self.submit_job:
                job = await self.submit_job("push", name)
                return ConsoleResult(command=text, output=f"push started as job {job.id}", api=True, job_id=job.id, duration=time.time() - start)
            await self.client.push(name, lambda e: None)
            return ConsoleResult(command=text, output="push complete", api=True, duration=time.time() - start)
        if cmd == "create":
            if not args:
                raise ConsoleError("usage: ollama create <model> [-f modelfile]")
            name = validate_model_name(args[0])
            modelfile = "FROM " + name
            # support `-f` path only if a local file exists
            if "-f" in args:
                idx = args.index("-f")
                if idx + 1 < len(args):
                    path = args[idx + 1]
                    import os
                    if os.path.isfile(path):
                        modelfile = open(path).read()
                    else:
                        raise ConsoleError(f"modelfile not found: {path}")
            if self.submit_job:
                job = await self.submit_job("create", name, modelfile)
                return ConsoleResult(command=text, output=f"create started as job {job.id}", api=True, job_id=job.id, duration=time.time() - start)
            await self.client.create(name, modelfile, lambda e: None)
            return ConsoleResult(command=text, output="create complete", api=True, duration=time.time() - start)
        raise ConsoleError(f"unsupported: {cmd}")

    def _help(self) -> str:
        lines = ["Usage: ollama [command] [args]", "", "Commands:"]
        for k, v in ALLOWED_COMMANDS.items():
            lines.append(f"  {k:<10} {v}")
        return "\n".join(lines)


def _fmt_bytes(n: int) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"
