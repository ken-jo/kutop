"""Regression: a sidebar panel checkbox must toggle in ONE click.

The sidebar re-syncs its controls on every 5s refresh (``update_state`` sets
``_syncing`` while it writes widget values). The panel checkboxes used to gate
``on_checkbox_changed`` on ``_syncing``, so a user click that landed in that
window was dropped — the panel only toggled on the second press. Programmatic
writes now suppress their Changed echo via ``prevent()``, so the handler no
longer needs (or uses) the ``_syncing`` gate and every real click counts.
"""

from __future__ import annotations

import asyncio

from textual.widgets import Checkbox, DataTable

from kutop.config import Config
from kutop.model import Pod, Snapshot
from kutop.render.app import TopApp
from kutop.render.sidebar import SidebarPanel


def _toggle_takes_effect_during_sync(chk_id: str, read) -> None:
    async def drive() -> None:
        app = TopApp(["default"], discover_namespaces=False, auto_refresh=False)
        async with app.run_test(size=(120, 44)) as pilot:
            await pilot.pause()
            await pilot.pause()
            sidebar = app.query_one("#sidebar", SidebarPanel)
            cb = app.query_one(f"#{chk_id}", Checkbox)
            before = read(app)
            # simulate the refresh-driven re-sync window being active right when
            # the user clicks: the old code dropped the click here
            sidebar._syncing = True
            cb.toggle()
            await pilot.pause()
            await pilot.pause()
            assert read(app) == (not before), (
                f"{chk_id}: one click during a sync did not toggle the panel "
                f"(before={before}, after={read(app)})"
            )
            await pilot.exit(None)

    asyncio.run(drive())


def test_alerts_checkbox_toggles_in_one_click_during_sync() -> None:
    _toggle_takes_effect_during_sync("chk_alerts", lambda a: a.show_alerts)


def test_pvc_checkbox_toggles_in_one_click_during_sync() -> None:
    _toggle_takes_effect_during_sync("chk_pvc", lambda a: a.show_pvc)


def test_events_checkbox_toggles_in_one_click_during_sync() -> None:
    _toggle_takes_effect_during_sync("chk_events", lambda a: a.show_events)


def test_programmatic_set_checkbox_emits_no_changed_echo() -> None:
    """_set_checkbox must not re-enter on_checkbox_changed (prevent() echo),
    otherwise dropping the _syncing gate would cause a feedback loop."""
    async def drive() -> None:
        app = TopApp(["default"], discover_namespaces=False, auto_refresh=False)
        async with app.run_test(size=(120, 44)) as pilot:
            await pilot.pause()
            await pilot.pause()
            sidebar = app.query_one("#sidebar", SidebarPanel)
            seen = []
            orig = sidebar.on_checkbox_changed
            sidebar.on_checkbox_changed = lambda e: (seen.append(e.checkbox.id),
                                                     orig(e))[1]
            # flip the live state and re-sync: the programmatic write must NOT
            # surface as a user Changed
            app.show_alerts = not app.show_alerts
            sidebar._set_checkbox("chk_alerts", app.show_alerts)
            await pilot.pause()
            await pilot.pause()
            assert "chk_alerts" not in seen, (
                f"_set_checkbox emitted a Changed echo: {seen}"
            )
            await pilot.exit(None)

    asyncio.run(drive())


def test_pvc_panel_on_by_default() -> None:
    """A fresh launch shows every panel — PVC included."""
    assert Config().show_pvc is True


def test_namespace_change_repaints_cached_pods_before_fetch() -> None:
    """Deselected namespaces disappear before the network refresh finishes."""
    async def drive() -> None:
        cfg = Config(namespaces=["ns-a", "ns-b"])
        app = TopApp(
            ["ns-a", "ns-b"], config=cfg,
            discover_namespaces=False, auto_refresh=False,
        )
        async with app.run_test(size=(120, 44)) as pilot:
            await pilot.pause()
            snap = Snapshot()
            snap.pods = [
                Pod(name="pod-a", namespace="ns-a"),
                Pod(name="pod-b", namespace="ns-b"),
            ]
            app._apply_snapshot(snap)
            await pilot.pause()

            refreshes: list[bool] = []
            app._persist_state = lambda: None  # type: ignore[method-assign]
            app._request_refresh = (  # type: ignore[method-assign]
                lambda: refreshes.append(True)
            )

            app.set_namespaces(["ns-b"])

            table = app.query_one("#main_table", DataTable)
            keys = {str(key.value) for key in table.rows}
            assert "pod:ns-a/pod-a" not in keys
            assert "pod:ns-b/pod-b" in keys
            assert app.fetcher.namespaces == ["ns-b"]
            assert refreshes == [True]
            await pilot.exit(None)

    asyncio.run(drive())
