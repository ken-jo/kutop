"""The kutop control sidebar: SidebarState + SidebarPanel.

Split out of render/app.py along its class seams. The panel mirrors app state
handed to it as a :class:`SidebarState`; user input is forwarded back through
``self.app`` (set_namespaces / set_profile / set_context / toggles).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.widgets import Button, Checkbox, Label, Select, Static

from ..config import REFRESH_INTERVAL_SECS, SORTABLE_KEYS

__all__ = ["SidebarState", "SidebarPanel"]

@dataclass
class SidebarState:
    """Everything the sidebar mirrors from the app, as one value object.

    Replaces the 19-21 keyword arguments that were previously spelled out in
    four hand-maintained copies (SidebarPanel ``__init__``/``on_mount``/
    ``update_state`` and TopApp ``compose``/``_sync_sidebar_state``) — adding a
    sidebar field now means touching this dataclass and the place that uses it.
    """

    selected: list = field(default_factory=list)
    show_events: bool = True
    show_pvc: bool = True
    show_summary: bool = True
    show_trends: bool = True
    show_alerts: bool = True
    show_health: bool = True
    show_keys: bool = True
    sort_key: str = "priority"
    sort_desc: bool = False
    group_by_node: bool = False
    allow_delete: bool = False
    profile_name: str = "generic"
    remember_profile: bool = False
    interval: float = REFRESH_INTERVAL_SECS
    context: str = ""
    name_filter: str = ""
    key_context: str = "DASHBOARD"
    key_rows: list = field(default_factory=list)


class SidebarPanel(Vertical):
    """Collapsible control sidebar: ns checkboxes, sort mode, panel toggles.

    The NAMESPACE section renders one :class:`Checkbox` per known namespace; the
    user ticks any combination and the dashboard shows the pods of every ticked
    namespace COMBINED (the Fetcher already accepts and merges a namespace list).
    The namespace checkboxes are the primary selector — the Options modal mirrors
    the same ticked set via ``apply_config``.

    The controls live inside a ``VerticalScroll`` so that on short terminals
    the lower sections (NAMESPACE / PANELS) remain reachable instead of being
    clipped off the bottom of the viewport. The
    namespace checkbox list itself sits in its own bounded ``VerticalScroll`` so
    that a cluster with many namespaces never overflows the lower controls.
    """

    #: each namespace checkbox carries this class so the change handler can tell
    #: them apart from the panel-toggle checkboxes (which have stable ids).
    NS_CLASS = "ns-checkbox"

    # Esc inside the sidebar hands focus back to the pod table (issue #2): the
    # sidebar is a pinned layout panel, not a popup, so escape restores focus
    # instead of dismissing anything.
    BINDINGS = [("escape", "focus_main", "Table")]

    def __init__(
        self,
        ns_options: list[str],
        state: "Optional[SidebarState]" = None,
        profile_options: "Optional[list[str]]" = None,
        context_options: "Optional[list[str]]" = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._ns_options = list(ns_options)
        self._profile_options = list(profile_options or [])
        self._context_options = list(context_options or [])
        self._ingest(state or SidebarState(selected=list(ns_options)))
        self._syncing = False
        self._ready_for_input = False
        # last (options, value) actually written to the CONTEXT Select, so the
        # every-refresh state sync can skip the rebuild when nothing changed —
        # set_options() closes an open dropdown, which made the CONTEXT picker
        # unusable while the 5s refresh was running.
        self._ctx_select_applied: "Optional[tuple]" = None

    def compose(self) -> ComposeResult:
        yield Static("", id="side_status")
        with VerticalScroll(id="side_scroll"):
            yield Label("PROFILE", classes="side_section")
            yield Select(
                [(p, p) for p in self._profile_options] or [("generic", "generic")],
                value=self._profile_name,
                id="side_profile",
                allow_blank=False,
            )
            yield Checkbox("Remember for this context", value=self._remember_profile,
                           id="chk_remember_profile", compact=True)
            yield Label("CONTEXT", classes="side_section side_section_spaced")
            ctx_opts = self._context_options or [self._context_name or ""]
            yield Select(
                [(c or "(current)", c) for c in ctx_opts],
                value=(self._context_name if self._context_name in ctx_opts
                       else ctx_opts[0]),
                id="side_context",
                allow_blank=False,
            )
            yield Label("NAMESPACES", id="side_ns_title",
                        classes="side_section side_section_spaced")
            with VerticalScroll(id="side_ns_box"):
                yield from self._ns_checkboxes()
            yield Label("SORT", classes="side_section side_section_spaced")
            yield Select(
                [(k, k) for k in SORTABLE_KEYS],
                value=self._sort_key,
                id="side_sort",
                allow_blank=False,
            )
            yield Checkbox("Descending", value=self._sort_desc, id="chk_sort_desc",
                           compact=True)
            yield Checkbox("Group by node", value=self._group_by_node, id="chk_group",
                           compact=True)
            yield Label("PANELS", classes="side_section side_section_spaced")
            yield Checkbox("Summary", value=self._show_summary, id="chk_summary",
                           compact=True)
            yield Checkbox("Trends", value=self._show_trends, id="chk_trends",
                           compact=True)
            yield Checkbox("Warning Events", value=self._show_events, id="chk_events",
                           compact=True)
            yield Checkbox("PVC Storage", value=self._show_pvc, id="chk_pvc",
                           compact=True)
            yield Checkbox("Alerts", value=self._show_alerts, id="chk_alerts",
                           compact=True)
            yield Checkbox("Health", value=self._show_health, id="chk_health",
                           compact=True)
            yield Checkbox("Keys", value=self._show_keys, id="chk_keys",
                           compact=True)
            yield Label("ACTIONS", classes="side_section side_section_spaced")
            yield Checkbox("Allow delete/restart (x/X)", value=self._allow_delete,
                           id="chk_allow_delete", compact=True)
            # MENU absorbs the old hamburger popup (issue #2): the header icon
            # routes here, so the global actions share the sidebar surface.
            # It sits with the other scroll-reachable lower sections so the
            # leading PROFILE/CONTEXT/NAMESPACES stay in the default viewport;
            # focus_menu() scrolls it into view. Order matters — the hamburger
            # lands on the FIRST menu button.
            yield Label("MENU", classes="side_section side_section_spaced")
            yield Button("Options", id="side_menu_options",
                         classes="side_menu_btn", compact=True)
            yield Button("Keys", id="side_menu_keys",
                         classes="side_menu_btn", compact=True)
            yield Button("Screenshot", id="side_menu_screenshot",
                         classes="side_menu_btn", compact=True)
            yield Button("Quit", id="side_menu_quit",
                         classes="side_menu_btn", compact=True)
        with Vertical(id="side_keys_box"):
            yield Label(
                "KEYS",
                classes="side_section",
                id="side_keys_title",
            )
            yield Static("", id="side_keys_body")

    def _ingest(self, state: "SidebarState") -> None:
        """Adopt a SidebarState into the panel's working attributes."""
        self._state = state
        self._selected = set(state.selected)
        self._show_events = state.show_events
        self._show_pvc = state.show_pvc
        self._show_summary = state.show_summary
        self._show_trends = state.show_trends
        self._show_alerts = state.show_alerts
        self._show_health = state.show_health
        self._show_keys = state.show_keys
        self._sort_key = (state.sort_key if state.sort_key in SORTABLE_KEYS
                          else "priority")
        self._sort_desc = state.sort_desc
        self._group_by_node = state.group_by_node
        self._allow_delete = state.allow_delete
        self._profile_name = state.profile_name or "generic"
        self._remember_profile = state.remember_profile
        self._interval = state.interval
        self._context_name = state.context or ""
        self._name_filter = state.name_filter
        self._key_context = state.key_context or "DASHBOARD"
        self._key_rows = list(state.key_rows or [])

    def on_mount(self) -> None:
        self.border_title = "SIDEBAR"
        self.update_state(self._state)
        try:
            self.call_after_refresh(self._enable_input_events)
        except Exception:
            self._ready_for_input = True

    def _enable_input_events(self) -> None:
        self._ready_for_input = True

    def focus_menu(self) -> None:
        """Land focus on the first MENU control (hamburger on a visible sidebar)."""
        try:
            self.query_one("#side_menu_options", Button).focus()
        except Exception:
            pass

    def action_focus_main(self) -> None:
        """Esc hands focus back to the pod table. Settle a pending quit hint
        first: this Esc replaces the app-level clear_search Esc (which is the
        base quit-cancel path), so without this the cancel would be swallowed
        and the next Enter on the table would exit the app."""
        try:
            self.app._cancel_pending_quit()
        except Exception:
            pass
        try:
            self.app.query_one("#main_table").focus()
        except Exception:
            pass

    def _ns_checkboxes(self):
        """One Checkbox per known namespace; the namespace is stored in ``name``."""
        for ns in self._ns_options:
            yield Checkbox(ns, value=ns in self._selected, name=ns,
                           classes=self.NS_CLASS, compact=True)

    def set_ns_status(self, status: str = "") -> None:
        """Annotate the NAMESPACES header with the live discovery state.

        ``""`` restores the plain header. Anything else is appended after a
        separator ("NAMESPACES · loading…"), so a cluster that is slow to list
        its namespaces — or cannot be listed at all — never looks like a
        cluster that simply has one namespace.
        """
        try:
            label = self.query_one("#side_ns_title", Label)
        except Exception:
            return
        text = Text("NAMESPACES")
        if status:
            text.append(" · ", style="dim")
            text.append(status, style="dim")
        label.update(text)

    def rebuild_namespaces(self, ns_options: list[str], selected: list[str]) -> None:
        """Repopulate the namespace checkbox list (live discovery / config sync).

        Mounts a fresh Checkbox per namespace inside ``#side_ns_box`` reflecting
        the given ticked ``selected`` set. Best effort: silently no-ops if the
        container is not mounted yet (first synchronous compose handles that).

        The removal is AWAITED before the mount. ``Widget.remove()`` only
        schedules the prune, so the old "remove then mount immediately" pair
        left both generations in the DOM until the next frame — and any
        :meth:`ns_checkbox_state` read in that window returned the OLD ticked
        namespaces UNIONED with the new ones, which the app then adopted as the
        watched scope. The swap runs as one ``call_next`` callback so the
        intermediate state is never observable: a read before it lands sees the
        old set, a read after sees the new one, never a mixture.
        """
        self._ns_options = list(ns_options)
        self._selected = set(selected)
        try:
            box = self.query_one("#side_ns_box", VerticalScroll)
        except Exception:
            return

        async def _swap() -> None:
            await box.remove_children(Checkbox)
            await box.mount(*self._ns_checkboxes())

        if self.is_running:
            self.call_next(_swap)
        else:
            # no message pump to drain the swap (headless construction): fall
            # back to the direct, best-effort rebuild
            for existing in list(box.query(Checkbox)):
                existing.remove()
            box.mount(*self._ns_checkboxes())

    def rebuild_contexts(self, options: list[str], current: str) -> None:
        """Repopulate the CONTEXT Select from discovered kubeconfig contexts.

        Mirrors :meth:`rebuild_namespaces`: context discovery runs after mount, so
        the Select starts with just the current context and is refilled here.
        Keeps the active context selected; best-effort if not mounted yet.
        """
        self._context_options = list(options)
        self._context_name = (current or "").strip()
        self._syncing = True
        try:
            self._apply_context_to_select()
        finally:
            # Keep _syncing armed across the dispatch of any queued Changed echo
            # (delivered on a later event-loop turn): clear it on the next frame
            # rather than in a synchronous finally so on_select_changed still sees
            # the guard and set_context cannot re-fire in a feedback loop.
            self._disarm_syncing_next_frame()

    def _apply_context_to_select(self) -> None:
        """Write the CONTEXT Select options + value from the cached state in one
        place, so the widget options and the selected value can never diverge.

        Single funnel shared by :meth:`rebuild_contexts` and :meth:`update_state`:
        rebuild the options, then select the desired value, suppressing the
        programmatic ``Select.Changed`` echo. Best-effort if the Select is not
        mounted yet. Callers must arm/disarm ``_syncing`` around this.
        """
        try:
            sel = self.query_one("#side_context", Select)
        except Exception:
            return
        ctx_opts = self._context_options or [self._context_name or ""]
        pairs = [(c or "(current)", c) for c in ctx_opts]
        desired = (self._context_name if self._context_name in ctx_opts
                   else ctx_opts[0])
        state = (tuple(pairs), desired)
        if self._ctx_select_applied == state and sel.value == desired:
            return  # nothing changed: don't reset (and close) an open dropdown
        try:
            # prevent() suppresses the Select.Changed echo from the programmatic
            # set_options/value writes — the reactive posts Changed as a queued
            # message dispatched after a synchronous _syncing reset, so the
            # _syncing guard in on_select_changed could not catch it and
            # set_context would re-fire in an unbounded loop. We keep BOTH guards:
            # prevent() blocks the echo, and _disarm_syncing_next_frame() re-arms
            # _syncing so a queued echo that slips past prevent() is still ignored.
            with sel.prevent(Select.Changed):
                sel.set_options(pairs)
                # set_options() already reset value to ctx_opts[0]; only re-assign
                # when the desired value differs, so no extra Changed is generated.
                if sel.value != desired:
                    sel.value = desired
            self._ctx_select_applied = state
        except Exception:
            pass

    def _disarm_syncing_next_frame(self) -> None:
        """Clear ``_syncing`` on the next frame, after queued Changed echoes drain.

        Programmatic ``Select.set_options``/``value`` writes post a ``Changed``
        message that is dispatched on a *later* event-loop turn. Clearing
        ``_syncing`` synchronously would expose that echo to ``on_select_changed``
        and re-enter ``set_context``; deferring the reset keeps the guard live
        until the echo has drained. Falls back to a synchronous reset if no app
        loop is available (e.g. teardown), so the flag never sticks.
        """
        try:
            self.app.call_after_refresh(lambda: setattr(self, "_syncing", False))
        except Exception:
            self._syncing = False

    def ns_checkbox_state(self) -> "list[str]":
        """The currently-ticked namespaces, in display order."""
        out: list[str] = []
        try:
            for cb in self.query_one("#side_ns_box", VerticalScroll).query(Checkbox):
                if cb.value and cb.name:
                    out.append(cb.name)
        except Exception:
            pass
        return out

    def update_state(self, state: "SidebarState") -> None:
        """Refresh compact status text and control values from the app state."""
        self._ingest(state)
        try:
            ns_count = len([n for n in state.selected if n])
            ctx = self._context_name or "current"
            # long contexts (e.g. EKS ARNs) hold the useful name at the end —
            # show the last path segment, then tail-truncate, not the prefix
            if len(ctx) > 24:
                ctx = ctx.rsplit("/", 1)[-1] if "/" in ctx else ctx
                if len(ctx) > 24:
                    ctx = "…" + ctx[-23:]
            direction = "desc" if self._sort_desc else "asc"
            status = Text()
            status.append("ns=", style="dim")
            status.append(str(ns_count), style="bold green")
            status.append(" | refresh=", style="dim")
            status.append(f"{self._interval:g}s", style="bold cyan")
            status.append("\nctx=", style="dim")
            status.append(ctx[:24], style="bold")
            status.append("\nsort=", style="dim")
            status.append(self._sort_key, style="bold magenta")
            status.append(" | dir=", style="dim")
            status.append(direction, style="bold yellow")
            # only show the filter line when a filter is active, so the common
            # case stays 3 lines and leaves more room for the panel toggles
            if self._name_filter:
                status.append("\nfilter=", style="dim")
                status.append(self._name_filter[:22], style="bold")
            self.query_one("#side_status", Static).update(status)
        except Exception:
            pass
        self._syncing = True
        try:
            self._set_checkbox("chk_summary", self._show_summary)
            self._set_checkbox("chk_trends", self._show_trends)
            self._set_checkbox("chk_events", self._show_events)
            self._set_checkbox("chk_pvc", self._show_pvc)
            self._set_checkbox("chk_alerts", self._show_alerts)
            self._set_checkbox("chk_health", self._show_health)
            self._set_checkbox("chk_keys", self._show_keys)
            self._set_checkbox("chk_sort_desc", self._sort_desc)
            self._set_checkbox("chk_group", self._group_by_node)
            self._set_checkbox("chk_allow_delete", self._allow_delete)
            self._set_checkbox("chk_remember_profile", self._remember_profile)
            # Same treatment as _apply_context_to_select / _set_checkbox: a
            # programmatic write posts a QUEUED Select.Changed that is dispatched
            # after the synchronous _syncing reset, so prevent() (not the flag)
            # is what actually keeps set_sort_key/set_profile from re-firing.
            self._set_select("side_sort", self._sort_key)
            self._set_select("side_profile", self._profile_name)
            # Funnel the context Select through the SAME helper rebuild_contexts
            # uses, so the widget options and the desired value can never diverge
            # (avoids a stale display or an InvalidSelectValueError when the live
            # context isn't yet in the Select's options).
            if self._context_name and self._context_name not in self._context_options:
                # keep the live context selectable even before discovery lists it
                self._context_options = [self._context_name, *self._context_options]
            self._apply_context_to_select()
            self._render_keys_panel()
        finally:
            # Re-arm _syncing across the next frame so any queued Changed echo from
            # the programmatic Select writes above drains while still guarded.
            self._disarm_syncing_next_frame()

    def _set_select(self, widget_id: str, value: str) -> None:
        """Write a Select value programmatically, suppressing the Changed echo."""
        try:
            sel = self.query_one(f"#{widget_id}", Select)
            if sel.value != value:
                with sel.prevent(Select.Changed):
                    sel.value = value
        except Exception:
            pass

    def _render_keys_panel(self) -> None:
        # update_state runs on every 5s refresh and can race a teardown/rebuild
        # where these ids are momentarily absent; a NoMatches here would abort
        # the whole state sync (leaving the status text and controls stale).
        try:
            box = self.query_one("#side_keys_box", Vertical)
            title = self.query_one("#side_keys_title", Label)
            body = self.query_one("#side_keys_body", Static)
        except NoMatches:
            return
        box.set_class(not self._show_keys, "-hidden")
        title.set_class(not self._show_keys, "-hidden")
        body.set_class(not self._show_keys, "-hidden")
        if not self._show_keys:
            return
        title.update(f"KEYS · {self._key_context}")
        if not self._key_rows:
            # every context ships rows; keep a quiet fallback for safety
            body.update("no keys for this context")
            return
        text = Text()
        for index, (key, label) in enumerate(self._key_rows):
            if index:
                text.append("\n")
            text.append(f"{key:<5}", style="bold cyan")
            text.append(label)
        body.update(text)

    def _set_checkbox(self, widget_id: str, value: bool) -> None:
        try:
            cb = self.query_one(f"#{widget_id}", Checkbox)
            if cb.value != value:
                # Suppress the Changed echo from this programmatic write (like
                # the Select sync below). With every sync write prevented,
                # on_checkbox_changed only ever sees genuine user clicks and no
                # longer needs the _syncing gate — which used to DROP a click
                # that raced a 5s refresh-driven re-sync, the "two presses to
                # toggle a panel" bug.
                with cb.prevent(Checkbox.Changed):
                    cb.value = value
        except Exception:
            pass

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        """Route the MENU buttons through the app (the old hamburger actions)."""
        bid = event.button.id or ""
        if bid == "side_menu_options":
            self.app.action_open_options()  # type: ignore[attr-defined]
        elif bid == "side_menu_keys":
            await self.app.run_action("app.show_help_panel")
        elif bid == "side_menu_screenshot":
            await self.app.run_action("app.screenshot")
        elif bid == "side_menu_quit":
            # issue #2 decision: an explicit menu selection needs no two-step
            # confirmation — take the direct quit path, not action_quit_hint.
            self.app.action_quit()  # type: ignore[attr-defined]

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        # Only genuine user clicks reach here: programmatic syncs go through
        # _set_checkbox, which prevents the Changed echo. The _syncing flag is
        # intentionally NOT checked — gating on it dropped a real click that
        # raced a refresh-driven re-sync (the "press twice to toggle" bug).
        if not self._ready_for_input:
            return
        app = self.app  # type: ignore[assignment]
        cb = event.checkbox
        if cb.has_class(self.NS_CLASS):
            # a namespace was ticked/unticked -> hand the app the full ticked set
            app.set_namespaces(self.ns_checkbox_state())  # type: ignore[attr-defined]
        elif cb.id == "chk_summary":
            app.cfg.show_summary = event.value  # type: ignore[attr-defined]
            app.apply_panel_visibility(persist=True)  # type: ignore[attr-defined]
        elif cb.id == "chk_trends":
            app.cfg.show_trends = event.value  # type: ignore[attr-defined]
            app.apply_panel_visibility(persist=True)  # type: ignore[attr-defined]
        elif cb.id == "chk_events":
            app.show_events = event.value  # type: ignore[attr-defined]
            app.apply_panel_visibility(persist=True)  # type: ignore[attr-defined]
        elif cb.id == "chk_pvc":
            app.show_pvc = event.value  # type: ignore[attr-defined]
            app.apply_panel_visibility(persist=True)  # type: ignore[attr-defined]
        elif cb.id == "chk_alerts":
            app.show_alerts = event.value  # type: ignore[attr-defined]
            app.apply_panel_visibility(persist=True)  # type: ignore[attr-defined]
        elif cb.id == "chk_health":
            app.show_health = event.value  # type: ignore[attr-defined]
            app.apply_panel_visibility(persist=True)  # type: ignore[attr-defined]
        elif cb.id == "chk_keys":
            app.cfg.show_keys = event.value  # type: ignore[attr-defined]
            app.apply_panel_visibility(persist=True)  # type: ignore[attr-defined]
        elif cb.id == "chk_sort_desc":
            app.cfg.sort_desc = event.value  # type: ignore[attr-defined]
            app._persist_state()  # type: ignore[attr-defined]
            app._restamp_sort_header()  # type: ignore[attr-defined]
            if app._loaded:  # type: ignore[attr-defined]
                app._render_main_table()  # type: ignore[attr-defined]
        elif cb.id == "chk_group":
            app.cfg.group_by_node = event.value  # type: ignore[attr-defined]
            app._persist_state()  # type: ignore[attr-defined]
            if app._loaded:  # type: ignore[attr-defined]
                app._render_main_table()  # type: ignore[attr-defined]
        elif cb.id == "chk_allow_delete":
            app.set_allow_destructive(event.value)  # type: ignore[attr-defined]
        elif cb.id == "chk_remember_profile":
            app.set_remember_profile_per_context(event.value)  # type: ignore[attr-defined]

    def on_select_changed(self, event: Select.Changed) -> None:
        # NOT gated on _syncing: every programmatic Select write goes through
        # prevent(Select.Changed), so a Changed that reaches here is the USER's.
        # Gating it used to drop a real pick whenever a sidebar sync (e.g. the
        # focus change from the closing dropdown) landed between the pick and
        # its dispatch — the context switch then only "took" on the second try.
        if not self._ready_for_input:
            return
        # Idempotency guards (same rationale as side_context below): a Changed
        # echo that slips past prevent()/_syncing carries the value we just
        # wrote, so acting on it would re-enter set_sort_key/set_profile —
        # and set_profile reloads the config and re-syncs the sidebar, i.e. a
        # feedback loop.
        if event.select.id == "side_sort" and event.value is not Select.BLANK:
            if str(event.value) != self._sort_key:
                self.app.set_sort_key(str(event.value))  # type: ignore[attr-defined]
        elif event.select.id == "side_profile" and event.value is not Select.BLANK:
            if str(event.value) != self._profile_name:
                self.app.set_profile(str(event.value))  # type: ignore[attr-defined]
        elif event.select.id == "side_context" and event.value is not Select.BLANK:
            # Idempotency guard: only act on a real change. set_options() resets the
            # Select to its first option and posts a Changed echo; if that echo ever
            # slips past _syncing/prevent() it would carry the CURRENT context and
            # must be a no-op rather than re-entering set_context in a feedback loop.
            new_context = str(event.value)
            current = (self.app.context or "").strip()  # type: ignore[attr-defined]
            if new_context.strip() != current:
                self.app.set_context(new_context)  # type: ignore[attr-defined]
