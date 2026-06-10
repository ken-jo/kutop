"""Pod action modals: live logs, describe, and event detail.

Split out of render/app.py along its class seams. Each modal shells kubectl
itself (argv lists, never a shell) and stops its close key so q/escape never
leak into app-level bindings.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, RichLog

__all__ = ["LogViewerModal", "DescribeModal", "EventDetailModal"]

class LogViewerModal(ModalScreen):
    """Asynchronous live log streaming (`kubectl logs -f`)."""

    def __init__(self, pod_name: str, ns: str, tail: int, context: Optional[str]) -> None:
        super().__init__()
        self.pod_name = pod_name
        self.ns = ns
        self.tail = tail
        self.context = context
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.log_task: Optional[asyncio.Task] = None

    def compose(self) -> ComposeResult:
        with Vertical(id="log_box"):
            yield Label(f"Live Logs: {self.pod_name} [{self.ns}] — ESC/q to close", id="log_hdr")
            yield RichLog(id="log_content", highlight=True, max_lines=2000)

    async def on_mount(self) -> None:
        self.log_task = asyncio.create_task(self._stream())

    async def _stream(self) -> None:
        log = self.query_one("#log_content", RichLog)
        cmd = ["kubectl"]
        if self.context:
            cmd += ["--context", self.context]
        cmd += ["logs", "-n", self.ns, self.pod_name, "-f", f"--tail={self.tail}"]
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            assert self.proc.stdout is not None
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    break
                log.write(line.decode("utf-8", errors="ignore").rstrip())
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.write(f"[error] {exc}")

    async def on_key(self, event) -> None:
        if event.key in ("escape", "q"):
            # stop the event so the close key never leaks into app bindings
            # (q would arm the quit hint, escape would clear the search filter)
            event.stop()
            await self._close()

    async def _close(self) -> None:
        if self.log_task:
            self.log_task.cancel()
        if self.proc:
            try:
                self.proc.terminate()
                await self.proc.wait()
            except Exception:
                pass
        self.dismiss()


class DescribeModal(ModalScreen):
    """`kubectl describe pod` viewer."""

    def __init__(self, pod_name: str, ns: str, context: Optional[str],
                 owner: str = "") -> None:
        super().__init__()
        self.pod_name = pod_name
        self.ns = ns
        self.context = context
        # e.g. "StatefulSet/<name>" — surfaced in the header when known.
        self.owner = owner

    def compose(self) -> ComposeResult:
        owner_suffix = f" ({self.owner})" if self.owner else ""
        with Vertical(id="desc_box"):
            yield Label(
                f"Describe: {self.pod_name}{owner_suffix} [{self.ns}] — ESC/q to close",
                id="desc_hdr",
            )
            yield RichLog(id="desc_content", highlight=True)

    async def on_mount(self) -> None:
        log = self.query_one("#desc_content", RichLog)
        log.write("Loading kubectl describe...")
        cmd = ["kubectl"]
        if self.context:
            cmd += ["--context", self.context]
        cmd += ["describe", "pod", self.pod_name, "-n", self.ns]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await proc.communicate()
            log.clear()
            if out:
                log.write(out.decode("utf-8", errors="ignore"))
            if err:
                log.write(f"\n[stderr]\n{err.decode('utf-8', errors='ignore')}")
        except Exception as exc:
            log.write(f"[error] {exc}")

    def on_key(self, event) -> None:
        if event.key in ("escape", "q"):
            event.stop()  # see LogViewerModal.on_key: never leak the close key
            self.dismiss()


class EventDetailModal(ModalScreen):
    """Full event metadata dialog."""

    def __init__(self, name: str, reason: str, message: str) -> None:
        super().__init__()
        self._name = name
        self._reason = reason
        self._message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="ev_box"):
            yield Label("Event detail — ESC/q to close", id="ev_hdr")
            yield RichLog(id="ev_content")

    def on_mount(self) -> None:
        log = self.query_one("#ev_content", RichLog)
        log.write(Text.from_markup(f"[bold yellow]Object:[/]  {self._name}"))
        log.write(Text.from_markup(f"[bold yellow]Reason:[/]  {self._reason}"))
        log.write(Text.from_markup(f"[bold yellow]Message:[/]\n{self._message}"))

    def on_key(self, event) -> None:
        if event.key in ("escape", "q"):
            event.stop()  # see LogViewerModal.on_key: never leak the close key
            self.dismiss()
