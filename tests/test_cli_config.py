from __future__ import annotations

import re
from pathlib import Path

import kutop
from kutop.cli import _build_parser, _parse_size
from kutop.config import Config, Profile, apply_detail_preset, load_config


def test_version_metadata_matches_package() -> None:
    text = Path("pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert match is not None
    assert match.group(1) == kutop.__version__


def test_cli_accepts_snapshot_view_and_size() -> None:
    args = _build_parser().parse_args(
        [
            "--snapshot",
            "out.svg",
            "--snapshot-view",
            "options-profile",
            "--size",
            "96x30",
        ]
    )
    assert args.snapshot == "out.svg"
    assert args.snapshot_view == "options-profile"
    assert _parse_size(args.size) == (96, 30)
    assert _parse_size("bad") == (140, 40)
    assert _parse_size("5x3") == (20, 10)


def test_detail_presets_adjust_columns_and_panels() -> None:
    wide = apply_detail_preset(Config(), "wide")
    assert wide.summary_style == "compact"
    assert wide.name_width == 20
    assert "namespace" in wide.columns

    full = apply_detail_preset(Config(), "full")
    assert full.show_pvc is True
    assert "storage_pct" in full.columns
    assert "owner_name" in full.columns


def test_load_config_layers_profile_user_and_cli(tmp_path: Path) -> None:
    user_config = tmp_path / "config.yaml"
    user_config.write_text(
        """
view:
  interval: 9
  sort_key: cpu
cluster:
  namespaces: [user-ns]
panels:
  pvc: true
probes:
  health_probes:
    - name: api
      url: /api
      fields:
        ready: "ready=(\\\\w+)"
""".strip(),
        encoding="utf-8",
    )

    profile = Profile(
        namespaces=["profile-ns"],
        timezone="UTC",
        alertmanager_url="http://alerts.example/api/v2/alerts",
    )
    cfg = load_config(
        profile=profile,
        user_path=str(user_config),
        base_overrides={"cluster": {"namespaces": ["base-ns"]}},
        cli_overrides={
            "view": {"interval": 2},
            "cluster": {"namespaces": ["cli-ns"]},
        },
    )

    assert cfg.interval == 2
    assert cfg.namespaces == ["cli-ns"]
    assert cfg.timezone == "UTC"
    assert cfg.sort_key == "cpu"
    assert cfg.show_pvc is True
    assert cfg.alertmanager_url == "http://alerts.example/api/v2/alerts"
    assert cfg.health_probes == [
        {"name": "api", "url": "/api", "fields": {"ready": "ready=(\\w+)"}}
    ]
