"""The Options modal and the theme-preview widgets.

Split out of render/widgets.py: widgets.py keeps reusable presentation
primitives (gauges, sliders, panels, search, trends); these are full settings
screens. All edits flow through a working copy of Config and are applied live
via ``app.apply_config()``. The old hamburger ThemeMenuModal is gone — its
actions (Options / Keys / Screenshot / Quit) live in the sidebar MENU section
(issue #2 unification).
"""

from __future__ import annotations

from typing import Optional

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    Input,
    Label,
    OptionList,
    Select,
    TabbedContent,
    TabPane,
)
from textual.widgets.option_list import Option

from ..config import (
    Config,
    SORTABLE_KEYS,
    _DEFAULT_COLUMN_ORDER,
    SUMMARY_STYLES,
    build_column_registry,
)
from ._compat import SelectCurrent, SelectOverlay
from .widgets import DualThresholdSlider

__all__ = ["ThemePreviewOverlay", "ThemePreviewSelect", "OptionsModal"]

class ThemePreviewOverlay(SelectOverlay):
    """Select overlay that exposes highlight/escape as theme preview events."""

    class Preview(Message):
        def __init__(self, overlay: "ThemePreviewOverlay", option_index: int) -> None:
            super().__init__()
            self.overlay = overlay
            self.option_index = option_index

    class Restore(Message):
        pass

    def on_option_list_option_highlighted(
        self, event: OptionList.OptionHighlighted
    ) -> None:
        event.stop()
        self.post_message(self.Preview(self, event.option_index))

    def watch__mouse_hovering_over(self, option_index: Optional[int]) -> None:
        if option_index is None:
            return
        if 0 <= option_index < len(self.options):
            option = self.options[option_index]
            if not option.disabled:
                self.post_message(self.Preview(self, option_index))

    def action_dismiss(self) -> None:
        self.post_message(self.Restore())
        super().action_dismiss()


class ThemePreviewSelect(Select):
    """A normal Select whose open menu previews highlighted theme rows."""

    def compose(self) -> ComposeResult:
        yield SelectCurrent(self.prompt)
        yield ThemePreviewOverlay(type_to_search=self._type_to_search).data_bind(
            compact=Select.compact
        )


class OptionsModal(ModalScreen):
    """The full configurable-option skeleton: every setting, current value, live.

    Organised into topic TABS (``TabbedContent`` / ``TabPane``): View, Columns,
    Panels, Thresholds, Cluster, Profile. Each pane scrolls internally so no tab
    overflows on a short (~80x24) terminal; the modal header and the footer action
    row (Export / Close) stay OUTSIDE the tabs and are always visible. Editing a
    control applies immediately (via the app's ``apply_config`` callbacks) and
    persists to ``~/.config/kutop/config.yaml``. An "Export config" button writes
    the complete config and reports the path.

    The app passes its live :class:`Config`; this modal mutates a working copy
    and calls back into the app so the dashboard re-renders without a relaunch.
    """

    BINDINGS = [
        ("escape", "close", "Close"),
        ("o", "close", "Close"),
    ]

    def __init__(
        self,
        cfg: Config,
        discovered_ns: "Optional[list[str]]" = None,
        context_names: "Optional[list[str]]" = None,
        themes: "Optional[list[str]]" = None,
    ) -> None:
        super().__init__()
        self._cfg = cfg
        self._reg = build_column_registry()
        self._themes = list(themes or [cfg.theme])
        if cfg.theme not in self._themes:
            self._themes.insert(0, cfg.theme)
        self._committed_theme = cfg.theme
        self._preview_theme_name = cfg.theme
        self._theme_preview_dirty = False
        self._ready_for_input = False
        # All namespaces we know about for the multi-select: the live cluster
        # discovery (if any), unioned with whatever is currently selected so a
        # config-only namespace is never dropped from the list.
        ns_all = list(discovered_ns or [])
        for ns in cfg.namespaces:
            if ns not in ns_all:
                ns_all.append(ns)
        self._ns_all = ns_all
        contexts = list(context_names or [])
        if cfg.context and cfg.context not in contexts:
            contexts.insert(0, cfg.context)
        self._context_options = [("", "current context"), *[(ctx, ctx) for ctx in contexts]]

    # ── compose ──────────────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        c = self._cfg
        with Vertical(id="opt_box"):
            yield Label("OPTIONS / SETTINGS  —  the full config skeleton (ESC/o to close)",
                        id="opt_hdr")
            with TabbedContent(id="opt_tabs"):
                # View ──────────────────────────────────────────────────────
                with TabPane("View", id="opt_tab_view"):
                    with VerticalScroll(classes="opt_pane"):
                        with Vertical(classes="opt_field"):
                            yield Label("sort_key", classes="opt_label")
                            yield Select(
                                [(m, m) for m in SORTABLE_KEYS],
                                value=c.sort_key, id="opt_sort", allow_blank=False,
                                compact=True,
                            )
                            yield Label("sort by any column; 's' cycles in-app",
                                        classes="opt_hint")
                        yield Checkbox("Sort descending (▼)", value=c.sort_desc,
                                       id="opt_sort_desc", classes="opt_check",
                                       compact=True)
                        with Vertical(classes="opt_field"):
                            yield Label("theme", classes="opt_label")
                            yield ThemePreviewSelect(
                                [(name, name) for name in self._themes],
                                value=c.theme, id="opt_theme", allow_blank=False,
                                compact=True,
                            )
                            yield Label("Up/Down preview, Enter apply, Esc restore",
                                        classes="opt_hint")
                        yield Checkbox("Fill panel backgrounds",
                                       value=c.panel_backgrounds,
                                       id="opt_panel_backgrounds",
                                       classes="opt_check", compact=True)
                        with Vertical(classes="opt_field"):
                            yield Label("summary_style", classes="opt_label")
                            yield Select(
                                [(s, s) for s in SUMMARY_STYLES],
                                value=c.summary_style, id="opt_summary_style",
                                allow_blank=False, compact=True,
                            )
                            yield Label("top header layout", classes="opt_hint")
                        yield Checkbox("Group pods by node (topology)",
                                       value=c.group_by_node, id="opt_group_by_node",
                                       classes="opt_check", compact=True)

                        # Filters live alongside View (which pods the table shows).
                        # Name search is intentionally runtime-only via '/' and
                        # is not exposed here so it cannot look like saved config.
                        yield Label("FILTERS", classes="opt_section")
                        yield Checkbox("Hide completed (Succeeded/Completed) pods",
                                       value=c.hide_completed, id="opt_hide_completed",
                                       classes="opt_check", compact=True)
                        yield Checkbox("Only problems (non-Running / restarts>0 / oom)",
                                       value=c.only_problems, id="opt_only_problems",
                                       classes="opt_check", compact=True)

                # Columns ───────────────────────────────────────────────────
                with TabPane("Columns", id="opt_tab_columns"):
                    with VerticalScroll(classes="opt_pane"):
                        yield Label("Toggle visible; ↑/↓ + [ / ] to reorder",
                                    classes="opt_hint")
                        yield OptionList(id="opt_columns")
                        with Horizontal(classes="opt_btnrow"):
                            yield Button("Toggle (space)", id="opt_col_toggle",
                                         classes="opt_btn")
                            yield Button("Up [", id="opt_col_up", classes="opt_btn")
                            yield Button("Down ]", id="opt_col_down", classes="opt_btn")

                # Panels ─────────────────────────────────────────────────────
                with TabPane("Panels", id="opt_tab_panels"):
                    with VerticalScroll(classes="opt_pane"):
                        yield Checkbox("Summary bar", value=c.show_summary,
                                       id="opt_p_summary", classes="opt_check",
                                       compact=True)
                        yield Checkbox("Trend graphs", value=c.show_trends,
                                       id="opt_p_trends", classes="opt_check",
                                       compact=True)
                        yield Checkbox("Pod table", value=c.show_podtable,
                                       id="opt_p_podtable", classes="opt_check",
                                       compact=True)
                        yield Checkbox("Warning events", value=c.show_events,
                                       id="opt_p_events", classes="opt_check",
                                       compact=True)
                        yield Checkbox("PVC storage", value=c.show_pvc, id="opt_p_pvc",
                                       classes="opt_check", compact=True)
                        yield Checkbox("Alerts (AlertManager)", value=c.show_alerts,
                                       id="opt_p_alerts", classes="opt_check",
                                       compact=True)
                        yield Checkbox("Health row (workload probes)",
                                       value=c.show_health, id="opt_p_health",
                                       classes="opt_check", compact=True)
                        yield Checkbox("Keys sidebar panel", value=c.show_keys,
                                       id="opt_p_keys", classes="opt_check",
                                       compact=True)

                # Thresholds ─────────────────────────────────────────────────
                with TabPane("Thresholds", id="opt_tab_thresholds"):
                    with VerticalScroll(classes="opt_pane"):
                        yield Label(
                            "Drag the two handles (warn / crit). Keyboard: "
                            "←/→ move, [ / ] switch handle.",
                            classes="opt_hint",
                        )
                        yield DualThresholdSlider(
                            "cpu", c.cpu_warn, c.cpu_crit, label="CPU",
                            id="opt_slider_cpu", classes="opt_slider",
                        )
                        yield DualThresholdSlider(
                            "mem", c.mem_warn, c.mem_crit, label="MEM",
                            id="opt_slider_mem", classes="opt_slider",
                        )
                        yield DualThresholdSlider(
                            "pvc", c.pvc_warn, c.pvc_crit, label="PVC (per-pod storage)",
                            id="opt_slider_pvc", classes="opt_slider",
                        )

                # Cluster ────────────────────────────────────────────────────
                with TabPane("Cluster", id="opt_tab_cluster"):
                    with VerticalScroll(classes="opt_pane"):
                        yield Label("  namespaces (space toggles; multi-select)",
                                    classes="opt_hint")
                        yield OptionList(id="opt_namespaces")
                        with Vertical(classes="opt_field"):
                            yield Label("context", classes="opt_label")
                            yield Select(
                                [(label, value) for value, label in self._context_options],
                                value=c.context, id="opt_context", allow_blank=False,
                                compact=True,
                            )
                            yield Label("kubeconfig context; current = default",
                                        classes="opt_hint")

                # Profile identity + cluster-linked probe config ─────────────
                with TabPane("Profile", id="opt_tab_profile"):
                    with VerticalScroll(classes="opt_pane"):
                        yield Label(f"  active profile: {c.profile_name}  (name only)",
                                    classes="opt_hint", id="opt_profile_lbl")
                        yield Label("PROBES  (cluster-linked; opt-in)",
                                    classes="opt_section")
                        with Vertical(classes="opt_field"):
                            yield Label("alertmanager_url", classes="opt_label")
                            yield Input(value=c.alertmanager_url, id="opt_alertmanager_url",
                                        classes="opt_input",
                                        placeholder="http://…/api/v2/alerts",
                                        compact=True)
                            yield Label("blank = alerts panel hidden", classes="opt_hint")
                        hp_n = len(c.health_probes)
                        yield Label(
                            f"  health_probes: {hp_n} configured "
                            "(edit in config.yaml / profile)",
                            classes="opt_hint", id="opt_health_probes_lbl",
                        )

            with Horizontal(id="opt_footer"):
                yield Button("Export config", id="opt_export", variant="primary")
                yield Button("Close", id="opt_close", variant="default")
            yield Label("", id="opt_status")

    def on_mount(self) -> None:
        self._rebuild_columns()
        self._rebuild_namespaces()
        self.call_after_refresh(self._enable_input)

    def _enable_input(self) -> None:
        self._ready_for_input = True

    # ── namespace multi-select ────────────────────────────────────────────
    def _rebuild_namespaces(self) -> None:
        ol = self.query_one("#opt_namespaces", OptionList)
        sel = ol.highlighted
        ol.clear_options()
        chosen = set(self._cfg.namespaces)
        for ns in self._ns_all:
            on = ns in chosen
            mark = "[green]●[/]" if on else "[grey37]○[/]"
            ol.add_option(Option(f"{mark} {ns}", id=f"ns::{ns}"))
        if sel is not None and sel < ol.option_count:
            ol.highlighted = sel

    def _toggle_ns(self) -> None:
        ol = self.query_one("#opt_namespaces", OptionList)
        if ol.highlighted is None:
            return
        opt = ol.get_option_at_index(ol.highlighted)
        oid = opt.id or ""
        if not oid.startswith("ns::"):
            return
        ns = oid.split("::", 1)[1]
        chosen = list(self._cfg.namespaces)
        if ns in chosen:
            if len(chosen) > 1:           # keep at least one namespace
                chosen.remove(ns)
        else:
            chosen.append(ns)
        self._cfg.namespaces = chosen or ["default"]
        self._rebuild_namespaces()
        self._apply()

    # ── columns list (visible + hidden, ordered) ──────────────────────────
    def _rebuild_columns(self) -> None:
        ol = self.query_one("#opt_columns", OptionList)
        sel = ol.highlighted
        ol.clear_options()
        visible = self._cfg.visible_columns()
        # visible (in order) first, then remaining hidden columns
        ordered = list(visible) + [k for k in _DEFAULT_COLUMN_ORDER if k not in visible]
        for key in ordered:
            spec = self._reg[key]
            on = key in visible
            mark = "[green]●[/]" if on else "[grey37]○[/]"
            ol.add_option(Option(f"{mark} {spec.label}  ({key})", id=f"col::{key}"))
        if sel is not None and sel < ol.option_count:
            ol.highlighted = sel

    def _current_col_key(self) -> Optional[str]:
        ol = self.query_one("#opt_columns", OptionList)
        if ol.highlighted is None:
            return None
        opt = ol.get_option_at_index(ol.highlighted)
        oid = opt.id or ""
        return oid.split("::", 1)[1] if oid.startswith("col::") else None

    def _toggle_col(self) -> None:
        key = self._current_col_key()
        if not key:
            return
        cols = list(self._cfg.visible_columns())
        if key in cols:
            if len(cols) > 1:               # keep at least one column
                cols.remove(key)
        else:
            cols.append(key)
        self._cfg.columns = cols
        self._rebuild_columns()
        self._apply()

    def _move_col(self, delta: int) -> None:
        key = self._current_col_key()
        cols = list(self._cfg.visible_columns())
        if not key or key not in cols:
            return
        i = cols.index(key)
        j = i + delta
        if 0 <= j < len(cols):
            cols[i], cols[j] = cols[j], cols[i]
            self._cfg.columns = cols
            self._rebuild_columns()
            self._apply()

    # ── apply + persist ───────────────────────────────────────────────────
    def _apply(self) -> None:
        """Push the working config to the app (live re-render + persist)."""
        if self._theme_preview_dirty:
            preview_theme = self._cfg.theme
            self._cfg.theme = self._committed_theme
            self.app.apply_config(self._cfg)  # type: ignore[attr-defined]
            self._cfg.theme = preview_theme
            self.app.preview_theme(preview_theme)  # type: ignore[attr-defined]
            return
        self.app.apply_config(self._cfg)  # type: ignore[attr-defined]

    def _set_theme_select(self, theme: str) -> None:
        try:
            self.query_one("#opt_theme", Select).value = theme
        except Exception:
            pass

    def _preview_theme(self, theme: str) -> None:
        if theme not in self._themes:
            return
        self._preview_theme_name = theme
        self._theme_preview_dirty = theme != self._committed_theme
        self.app.preview_theme(theme)  # type: ignore[attr-defined]
        if self._theme_preview_dirty:
            self._set_status("theme preview: Enter to apply, Esc to restore")
        else:
            self._set_status("")

    def _commit_theme_preview(self) -> None:
        if not self._theme_preview_dirty:
            return
        self._cfg.theme = self._preview_theme_name
        self.app.commit_theme(self._cfg.theme)  # type: ignore[attr-defined]
        self._committed_theme = self._cfg.theme
        self._theme_preview_dirty = False
        self._set_status("theme applied")

    def _restore_theme_preview(self) -> None:
        if not self._theme_preview_dirty:
            return
        self._preview_theme_name = self._committed_theme
        self._theme_preview_dirty = False
        self._set_theme_select(self._committed_theme)
        self.app.preview_theme(self._committed_theme)  # type: ignore[attr-defined]
        self._set_status("theme restored")

    def _set_status(self, msg: str) -> None:
        try:
            self.query_one("#opt_status", Label).update(msg)
        except Exception:
            pass

    # ── events ──────────────────────────────────────────────────────────
    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if not self._ready_for_input:
            return
        cid = event.checkbox.id or ""
        mapping = {
            "opt_p_summary": "show_summary", "opt_p_trends": "show_trends",
            "opt_p_podtable": "show_podtable", "opt_p_events": "show_events",
            "opt_p_pvc": "show_pvc",
            "opt_p_alerts": "show_alerts", "opt_p_health": "show_health",
            "opt_p_keys": "show_keys",
            "opt_sort_desc": "sort_desc",
            "opt_panel_backgrounds": "panel_backgrounds",
            "opt_group_by_node": "group_by_node",
            "opt_hide_completed": "hide_completed",
            "opt_only_problems": "only_problems",
        }
        attr = mapping.get(cid)
        if attr:
            setattr(self._cfg, attr, event.value)
            self._apply()

    def on_select_changed(self, event: Select.Changed) -> None:
        if not self._ready_for_input:
            return
        if event.value is Select.BLANK:
            return
        if event.select.id == "opt_sort":
            # sort_key supersedes sort_mode; keep the legacy mirror in sync.
            self._cfg.sort_key = str(event.value)
            self._cfg.sort_mode = (str(event.value)
                                   if str(event.value) in ("priority", "cpu", "mem", "name")
                                   else "priority")
        elif event.select.id == "opt_theme":
            self._preview_theme_name = str(event.value)
            self._commit_theme_preview()
            event.stop()
            return
        elif event.select.id == "opt_context":
            self._cfg.context = str(event.value)
        elif event.select.id == "opt_summary_style":
            self._cfg.summary_style = str(event.value)
        self._apply()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if not self._ready_for_input:
            return
        self._consume_input(event.input)

    def on_input_changed(self, event: Input.Changed) -> None:
        if not self._ready_for_input:
            return
        # apply numeric/text inputs on change (validated/coerced in app)
        self._consume_input(event.input)

    def _consume_input(self, inp: Input) -> None:
        iid = inp.id or ""
        val = inp.value
        try:
            if iid == "opt_alertmanager_url":
                self._cfg.alertmanager_url = val.strip()
            # thresholds are edited via DualThresholdSlider (Thresholds tab),
            # not numeric inputs — see on_dual_threshold_slider_threshold_changed.
        except (TypeError, ValueError):
            return
        self._apply()

    def on_dual_threshold_slider_threshold_changed(
        self, event: "DualThresholdSlider.ThresholdChanged"
    ) -> None:
        """Live-apply a dragged threshold slider into the working config.

        Maps the slider's metric -> the matching ``<metric>_warn/_crit`` config
        fields, then calls ``apply_config`` so the table gauges re-color
        immediately and the change persists to the user config file.
        """
        if not self._ready_for_input:
            return
        m = event.metric
        if m in ("cpu", "mem", "pvc"):
            setattr(self._cfg, f"{m}_warn", int(event.warn))
            setattr(self._cfg, f"{m}_crit", int(event.crit))
            self._apply()
        event.stop()

    def on_option_list_option_highlighted(
        self, event: OptionList.OptionHighlighted
    ) -> None:
        pass  # highlight only; toggle/move via buttons or keys

    def on_theme_preview_overlay_preview(
        self, event: ThemePreviewOverlay.Preview
    ) -> None:
        select = event.overlay.parent
        if select is None or select.id != "opt_theme":
            return
        try:
            theme = str(select._options[event.option_index][1])  # type: ignore[attr-defined]
        except Exception:
            return
        self._preview_theme(theme)

    def on_theme_preview_overlay_restore(
        self, event: ThemePreviewOverlay.Restore
    ) -> None:
        if self._theme_preview_dirty:
            self._restore_theme_preview()
            event.stop()

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        # Enter on a namespace row toggles its membership (multi-select).
        if event.option_list.id == "opt_namespaces":
            self._toggle_ns()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid == "opt_col_toggle":
            self._toggle_col()
        elif bid == "opt_col_up":
            self._move_col(-1)
        elif bid == "opt_col_down":
            self._move_col(1)
        elif bid == "opt_export":
            path = self.app.export_config(self._cfg)  # type: ignore[attr-defined]
            self._set_status(f"exported -> {path}" if path
                             else "export failed (PyYAML missing?)")
        elif bid in ("opt_close",):
            self.action_close()

    def on_key(self, event) -> None:
        # column reorder/toggle shortcuts when the column list has focus
        focused = self.focused
        if focused is not None and focused.id == "opt_columns":
            if event.key == "space":
                self._toggle_col()
                event.stop()
            elif event.key == "left_square_bracket":
                self._move_col(-1)
                event.stop()
            elif event.key == "right_square_bracket":
                self._move_col(1)
                event.stop()
        elif focused is not None and focused.id == "opt_namespaces":
            if event.key == "space":
                self._toggle_ns()
                event.stop()

    def action_close(self) -> None:
        self._restore_theme_preview()
        self.dismiss()
