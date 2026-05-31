"""Optional, domain-specific kutop plugins + the generic plugin seam.

The kutop core (``fetch.py`` + ``render/app.py``) is workload-agnostic. Some
features — like scraping a workload's metrics endpoint and rendering extracted
fields — are too domain-specific to live in the generic OSS core. Those features
live here as self-contained, optional plugins.

The seam the core understands is intentionally tiny. A plugin is any object that
satisfies :class:`KutopPlugin`:

  * ``panel_id``          — the widget id its panel is mounted under (so the app
                            can show/hide it generically).
  * ``is_enabled(config)``— True when the plugin's config is present (e.g. a
                            profile contributed its activating settings).
  * ``fetch(fetcher, snapshot)`` — best-effort populate the snapshot off the UI
                            thread (called inside the existing fetch worker). It
                            MUST NOT raise — the core wraps it but a plugin owns
                            its own robustness.
  * ``make_panel()``      — construct the panel widget (mounted by the app).
  * ``render(panel, snapshot)`` — update the mounted panel from the latest
                            snapshot on the UI thread.

Discovery is import-guarded: the registry is built by importing each known
plugin module inside a ``try``/``except`` so the core still imports and runs even
if a plugin module is missing/deleted. The core NEVER imports a plugin module by
name for its logic — it only iterates :func:`iter_plugins` / :func:`iter_enabled`.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class KutopPlugin(Protocol):
    """The minimal seam the core uses to drive an optional feature generically."""

    #: widget id the plugin's panel is mounted under (used for show/hide).
    panel_id: str

    def is_enabled(self, config: Any) -> bool:
        """True when this plugin's activating config is present in ``config``."""
        ...

    def fetch(self, fetcher: Any, snapshot: Any) -> None:
        """Populate ``snapshot`` off the UI thread. Best-effort; never raises."""
        ...

    def make_panel(self) -> Any:
        """Build the panel :class:`~textual.widget.Widget` the app mounts."""
        ...

    def render(self, panel: Any, snapshot: Any) -> None:
        """Update the mounted panel from ``snapshot``. Best-effort; never raises."""
        ...


# ── registry ───────────────────────────────────────────────────────────────
#
# Built once, lazily, by importing each KNOWN plugin module under a guard. If a
# plugin module is absent (deleted) or fails to import, it is simply skipped so
# the core keeps running. The core treats this list opaquely.
_REGISTRY: "Optional[list[KutopPlugin]]" = None

# Module paths of the built-in optional plugins. Each must expose ``PLUGIN``
# (a KutopPlugin instance). Adding a plugin = appending its module path here.
_BUILTIN_PLUGIN_MODULES = (
    "kutop.plugins.health",
)


def _discover() -> "list[KutopPlugin]":
    """Import each known plugin module under a guard; collect its ``PLUGIN``.

    Robust by contract: a missing/broken plugin module is skipped (logged to
    nowhere) so the core never hard-depends on any single plugin existing.
    """
    import importlib

    found: list[KutopPlugin] = []
    for mod_path in _BUILTIN_PLUGIN_MODULES:
        try:
            mod = importlib.import_module(mod_path)
        except Exception:
            continue  # plugin module missing/broken -> core runs without it
        plugin = getattr(mod, "PLUGIN", None)
        if plugin is None:
            continue
        found.append(plugin)
    return found


def iter_plugins() -> "list[KutopPlugin]":
    """Return all discovered plugins (cached after the first call)."""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _discover()
    return list(_REGISTRY)


def iter_enabled(config: Any) -> "Iterable[KutopPlugin]":
    """Yield the plugins whose activating config is present in ``config``.

    Each ``is_enabled`` is called under a guard so a misbehaving plugin can never
    break the core's iteration.
    """
    for plugin in iter_plugins():
        try:
            if plugin.is_enabled(config):
                yield plugin
        except Exception:
            continue


def reset_registry() -> None:
    """Forget the cached registry so the next call re-discovers (tests/hot-reload)."""
    global _REGISTRY
    _REGISTRY = None
