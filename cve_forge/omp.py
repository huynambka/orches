"""Async wrapper around the ``omp -p`` (print-mode) CLI."""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


@dataclass(slots=True)
class OmpResult:
    output: str
    stderr: str
    returncode: int
    duration_s: float

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and bool(self.output.strip())


class OmpClient:
    """Spawn one-shot ``omp -p`` processes."""

    def __init__(
        self,
        omp_bin: str = "omp",
        default_model: str | None = None,
    ) -> None:
        resolved = shutil.which(omp_bin)
        if resolved is None:
            raise FileNotFoundError(f"omp binary not found: {omp_bin!r}")
        self.omp_bin = resolved
        self.default_model = default_model

    # ------------------------------------------------------------------ #
    async def run(
        self,
        prompt: str,
        *,
        append_system_prompt: str | None = None,
        cwd: str | Path | None = None,
        model: str | None = None,
        thinking: str = "medium",
        tools: list[str] | None = None,
        context_files: list[str | Path] | None = None,
        timeout: int = 300,
        extra_args: list[str] | None = None,
    ) -> OmpResult:
        args = [self.omp_bin, "-p"]

        # context files via @file syntax (before the text prompt)
        if context_files:
            for f in context_files:
                args.append(f"@{f}")

        args.append(prompt)

        if append_system_prompt:
            args.extend(["--append-system-prompt", append_system_prompt])

        model = model or self.default_model
        if model:
            args.extend(["--model", model])

        args.extend(["--thinking", thinking])

        if tools:
            args.extend(["--tools", ",".join(tools)])

        # always ephemeral, non-interactive
        args.extend(["--no-session", "--auto-approve", "--no-title"])

        if extra_args:
            args.extend(extra_args)

        log.debug("spawn: %s", " ".join(args[:6]) + " …")
        start = time.monotonic()

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd) if cwd else None,
        )

        try:
            stdout_raw, stderr_raw = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            elapsed = time.monotonic() - start
            log.warning("agent timed out after %.0fs", elapsed)
            return OmpResult(
                output="",
                stderr=f"Agent timed out after {timeout}s",
                returncode=-1,
                duration_s=elapsed,
            )

        elapsed = time.monotonic() - start
        out = _strip_ansi(stdout_raw.decode("utf-8", errors="replace"))
        err = stderr_raw.decode("utf-8", errors="replace")

        log.debug("done (%.1fs, rc=%s, %d chars)", elapsed, proc.returncode, len(out))

        return OmpResult(
            output=out,
            stderr=err,
            returncode=proc.returncode or 0,
            duration_s=elapsed,
        )
