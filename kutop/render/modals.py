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

__all__ = ["LogViewerModal", "DescribeModal", "YamlViewModal", "EventDetailModal"]

class LogViewerModal(ModalScreen):
    """Asynchronous log streaming (`kubectl logs`) with crashloop forensics.

    Beyond the live ``-f`` stream: ``p`` toggles ``--previous`` (the CRASHED
    container's logs — for a CrashLoopBackOff pod the live stream is empty or
    seconds-young, the previous one holds the actual crash), and ``c`` cycles
    the target container on multi-container pods. The header shows the active
    container/mode plus the pod's last termination reason and exit code.
    """

    def __init__(self, pod_name: str, ns: str, tail: int, context: Optional[str],
                 containers: "Optional[list[str]]" = None,
                 status_line: str = "") -> None:
        super().__init__()
        self.pod_name = pod_name
        self.ns = ns
        self.tail = tail
        self.context = context
        # spec-order container names; empty -> let kubectl pick its default
        self.containers = [c for c in (containers or []) if c]
        self.status_line = status_line
        self._container_idx = 0
        self._previous = False
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.log_task: Optional[asyncio.Task] = None

    @property
    def container(self) -> Optional[str]:
        """The targeted container name, or None for kubectl's default."""
        if not self.containers:
            return None
        return self.containers[self._container_idx % len(self.containers)]

    def _logs_cmd(self) -> "list[str]":
        """argv for the current mode. --previous logs are static, so no -f."""
        cmd = ["kubectl"]
        if self.context:
            cmd += ["--context", self.context]
        cmd += ["logs", "-n", self.ns, self.pod_name, f"--tail={self.tail}"]
        if self.container is not None:
            cmd += ["-c", self.container]
        if self._previous:
            cmd.append("--previous")
        else:
            cmd.append("-f")
        return cmd

    def _header_text(self) -> str:
        mode = "PREVIOUS (crashed)" if self._previous else "live"
        ctr = f" · ctr {self.container}" if self.container else ""
        status = f" · {self.status_line}" if self.status_line else ""
        keys = "q close · p previous"
        if len(self.containers) > 1:
            keys += " · c container"
        return f"Logs: {self.pod_name} [{self.ns}]{ctr} · {mode}{status} — {keys}"

    def compose(self) -> ComposeResult:
        with Vertical(id="log_box"):
            # Text(), never a markup str: the header embeds cluster-controlled
            # names (pod / namespace / container) inside literal '[...]', which
            # the Textual markup parser would eat or choke on.
            yield Label(Text(self._header_text()), id="log_hdr")
            yield RichLog(id="log_content", highlight=True, max_lines=2000)

    async def on_mount(self) -> None:
        self.log_task = asyncio.create_task(self._stream())

    async def _stream(self) -> None:
        log = self.query_one("#log_content", RichLog)
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *self._logs_cmd(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            assert self.proc.stdout is not None
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    break
                log.write(line.decode("utf-8", errors="ignore").rstrip())
            if self._previous:
                log.write("[end of previous container log]")
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.write(f"[error] {exc}")

    async def _restart_stream(self) -> None:
        """Stop the running kubectl and stream again with the new mode/target.

        The old task is cancelled AND awaited, and the old process terminated
        AND reaped, before ``_stream`` overwrites ``self.proc`` — otherwise the
        superseded ``kubectl logs -f`` kept running as an orphan with nothing
        left holding its handle. ``self.proc`` is only cleared once the process
        really exited, so a failed terminate never drops the reference.
        """
        task, self.log_task = self.log_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        proc = self.proc
        if proc is not None:
            try:
                if proc.returncode is None:
                    proc.terminate()
                await proc.wait()
            except Exception:
                pass
            if proc.returncode is not None:
                self.proc = None
        try:
            self.query_one("#log_hdr", Label).update(Text(self._header_text()))
            self.query_one("#log_content", RichLog).clear()
        except Exception:
            pass
        self.log_task = asyncio.create_task(self._stream())

    async def on_key(self, event) -> None:
        if event.key in ("escape", "q"):
            # stop the event so the close key never leaks into app bindings
            # (q would arm the quit hint, escape would clear the search filter)
            event.stop()
            await self._close()
        elif event.key == "p":
            event.stop()
            self._previous = not self._previous
            await self._restart_stream()
        elif event.key == "c" and len(self.containers) > 1:
            event.stop()
            self._container_idx += 1
            await self._restart_stream()

    def _teardown(self) -> None:
        """Synchronous half of the close path: cancel the reader, kill kubectl.

        Safe to call from ``on_unmount`` (which runs while the app is tearing
        down and cannot await), and idempotent so ``_close`` -> unmount runs it
        twice without effect.
        """
        if self.log_task:
            self.log_task.cancel()
        proc = self.proc
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
            except Exception:
                pass

    async def _close(self) -> None:
        self._teardown()
        proc = self.proc
        if proc is not None:
            try:
                await proc.wait()
            except Exception:
                pass
        self.dismiss()

    def on_unmount(self) -> None:
        # Quitting the app with the modal open dismisses nothing, so without
        # this the `kubectl logs -f` child outlived kutop.
        self._teardown()


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
        self._proc = None

    def compose(self) -> ComposeResult:
        owner_suffix = f" ({self.owner})" if self.owner else ""
        with Vertical(id="desc_box"):
            # Text(), not markup: '[{ns}]' must survive verbatim (see
            # LogViewerModal.compose).
            yield Label(
                Text(f"Describe: {self.pod_name}{owner_suffix} "
                     f"[{self.ns}] — ESC/q to close"),
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
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await self._proc.communicate()
            log.clear()
            if out:
                log.write(out.decode("utf-8", errors="ignore"))
            if err:
                log.write(f"\n[stderr]\n{err.decode('utf-8', errors='ignore')}")
        except Exception as exc:
            log.write(f"[error] {exc}")

    def _kill_proc(self) -> None:
        """Don't leave kubectl running after an early close — terminate the
        in-flight process so communicate() returns and the orphan exits.
        Mirrors :meth:`YamlViewModal._kill_proc`."""
        proc = self._proc
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
            except Exception:
                pass

    def on_key(self, event) -> None:
        if event.key in ("escape", "q"):
            event.stop()  # see LogViewerModal.on_key: never leak the close key
            self._kill_proc()
            self.dismiss()

    def on_unmount(self) -> None:
        self._kill_proc()


class YamlViewModal(ModalScreen):
    """`kubectl get pod -o yaml` viewer.

    Models DescribeModal: same async on_mount runner (no new subprocess path),
    same close-key handling that stops the event so q/escape never leak into the
    app bindings. The argv is factored into a static helper so it can be unit
    tested without spinning up the screen.
    """

    def __init__(self, pod_name: str, ns: str, context: Optional[str]) -> None:
        super().__init__()
        self.pod_name = pod_name
        self.ns = ns
        self.context = context
        self._proc = None

    @staticmethod
    def _yaml_cmd(pod_name: str, ns: str, context: Optional[str]) -> "list[str]":
        """argv for `kubectl [--context X] get pod <name> -n <ns> -o yaml`."""
        cmd = ["kubectl"]
        if context:
            cmd += ["--context", context]
        cmd += ["get", "pod", pod_name, "-n", ns, "-o", "yaml"]
        return cmd

    def compose(self) -> ComposeResult:
        # Reuses DescribeModal's styled ids (#desc_box/#desc_hdr/#desc_content)
        # so the YAML viewer inherits the same modal layout without a new TCSS
        # rule. Only one of these modals is ever on screen at a time.
        with Vertical(id="desc_box"):
            yield Label(
                Text(f"YAML: {self.pod_name} [{self.ns}] — ESC/q to close"),
                id="desc_hdr",
            )
            yield RichLog(id="desc_content", highlight=True)

    async def on_mount(self) -> None:
        log = self.query_one("#desc_content", RichLog)
        log.write("Loading kubectl get pod -o yaml...")
        cmd = self._yaml_cmd(self.pod_name, self.ns, self.context)
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await self._proc.communicate()
            log.clear()
            if out:
                log.write(out.decode("utf-8", errors="ignore"))
            if err:
                log.write(f"\n[stderr]\n{err.decode('utf-8', errors='ignore')}")
        except Exception as exc:
            log.write(f"[error] {exc}")

    def _kill_proc(self) -> None:
        """Don't leave kubectl running after an early close — terminate the
        in-flight process so communicate() returns and the orphan exits."""
        proc = self._proc
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
            except Exception:
                pass

    def on_key(self, event) -> None:
        if event.key in ("escape", "q"):
            event.stop()  # see LogViewerModal.on_key: never leak the close key
            self._kill_proc()
            self.dismiss()

    def on_unmount(self) -> None:
        self._kill_proc()


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

    @staticmethod
    def _field(label: str, value: str) -> Text:
        """One `label: value` line, styled without a markup round-trip.

        Event names/reasons/messages are cluster-controlled text: a message like
        ``unable to mount volume [/data/pvc-1]`` fed through
        ``Text.from_markup`` raises MarkupError ("nothing to close").
        """
        line = Text()
        line.append(label, style="bold yellow")
        line.append(value)
        return line

    def on_mount(self) -> None:
        log = self.query_one("#ev_content", RichLog)
        log.write(self._field("Object:  ", self._name))
        log.write(self._field("Reason:  ", self._reason))
        log.write(self._field("Message:\n", self._message))

    def on_key(self, event) -> None:
        if event.key in ("escape", "q"):
            event.stop()  # see LogViewerModal.on_key: never leak the close key
            self.dismiss()
