"""Regression tests for the config robustness fixes (0.4.1 audit).

Covers: the saved YAML must stay parseable for ANY user value (a health-probe
regex containing a quote used to corrupt the file and silently reset every
preference), and the profile-owned strip/persist contract must only touch the
fields the active profile actually supplies.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import yaml

from kutop.config import (
    Config,
    Profile,
    _config_for_persist,
    _strip_profile_owned,
    dump_config_yaml,
    load_config,
    save_config,
)


def test_dump_config_survives_hostile_values_and_round_trips(tmp_path: Path) -> None:
    cfg = Config(
        timezone="Asia/Seoul",
        context="arn:aws:eks:ap-northeast-2:123:cluster/x: y",
        alertmanager_url="http://am.local/api/v2/alerts?x=1#frag",
        health_probes=[{
            "name": "seq: uencer",
            "url": "http://h/q?a='1'",
            # the historical corruption case: a single quote inside the regex
            "fields": {"block": r"block='(\d+)'", "p#x": "v: w"},
        }],
        namespaces=["default", "kube-system"],
    )
    text = dump_config_yaml(cfg)
    data = yaml.safe_load(text)  # must parse — corruption here reset all prefs
    assert data["view"]["timezone"] == "Asia/Seoul"
    assert data["cluster"]["context"] == "arn:aws:eks:ap-northeast-2:123:cluster/x: y"
    assert data["probes"]["health_probes"][0]["fields"]["block"] == r"block='(\d+)'"

    # and the full save -> load cycle preserves the values (generic session)
    cfgfile = tmp_path / "config.yaml"
    save_config(cfg, str(cfgfile))
    loaded = load_config(user_path=str(cfgfile))
    assert loaded.timezone == "Asia/Seoul"
    assert loaded.health_probes[0]["fields"]["block"] == r"block='(\d+)'"


def test_strip_profile_owned_only_strips_what_the_profile_supplies() -> None:
    user_layer = {
        "view": {"timezone": "Asia/Seoul", "theme": "nord"},
        "cluster": {"namespaces": ["mine"], "context": "my-ctx"},
        "probes": {"alertmanager_url": "http://mine/alerts"},
        "thresholds": {"cpu_warn": 11},
        "profile": "stale",
    }
    # a profile WITHOUT timezone/namespaces/probes must not erase the user's —
    # _profile_layer drops its empty values precisely so they can't clobber
    bare = Profile(name="bare", context="prof-ctx")
    out = _strip_profile_owned(dict(user_layer), bare)
    assert out["view"]["timezone"] == "Asia/Seoul"
    assert out["cluster"]["namespaces"] == ["mine"]
    assert "context" not in out["cluster"]      # profile supplies it -> stripped
    assert out["probes"]["alertmanager_url"] == "http://mine/alerts"
    assert "thresholds" not in out              # always profile-owned
    assert "profile" not in out

    # a profile WITH those fields strips them all
    full = Profile(name="full", timezone="UTC", namespaces=["p"],
                   context="c", alertmanager_url="http://p/alerts")
    out = _strip_profile_owned(dict(user_layer), full)
    assert "timezone" not in out.get("view", {})
    assert out["view"]["theme"] == "nord"       # UI prefs always survive
    assert "cluster" not in out
    assert "probes" not in out


def test_config_for_persist_keeps_fields_the_profile_does_not_own() -> None:
    cfg = Config(profile_name="bare", timezone="Asia/Seoul",
                 namespaces=["mine"], context="my-ctx", cpu_warn=11)
    bare = Profile(name="bare", context="prof-ctx")  # owns context only
    out = _config_for_persist(cfg, bare)
    assert out.timezone == "Asia/Seoul"   # not owned -> the user's value persists
    assert out.namespaces == ["mine"]
    assert out.context == ""              # owned -> reset to baseline
    assert out.cpu_warn == 75             # thresholds always reset

    # without the live profile (legacy callers) the conservative reset applies
    out = _config_for_persist(cfg, None)
    assert out.timezone == ""
    assert out.namespaces == ["default"]


def test_corrupt_user_file_warns_backs_up_and_loads_defaults(tmp_path: Path) -> None:
    cfgfile = tmp_path / "config.yaml"
    corrupt = "view:\n  theme: [unclosed\n"
    cfgfile.write_text(corrupt, encoding="utf-8")

    cfg = load_config(user_path=str(cfgfile))

    assert cfg.theme == "textual-dark"          # defaults, not a crash
    assert len(cfg.load_warnings) == 1
    assert "could not be parsed" in cfg.load_warnings[0]
    backup = tmp_path / "config.yaml.invalid"
    assert str(backup) in cfg.load_warnings[0]
    # the broken-but-recoverable file was copied aside BEFORE the app's next
    # save_config can clobber the original
    assert backup.read_text(encoding="utf-8") == corrupt
    save_config(cfg, str(cfgfile))
    assert backup.read_text(encoding="utf-8") == corrupt
    reloaded = load_config(user_path=str(cfgfile))
    assert reloaded.load_warnings == []


def test_non_mapping_user_file_warns_and_backs_up(tmp_path: Path) -> None:
    cfgfile = tmp_path / "config.yaml"
    cfgfile.write_text("- not\n- a mapping\n", encoding="utf-8")

    cfg = load_config(user_path=str(cfgfile))

    assert cfg.namespaces == ["default"]
    assert len(cfg.load_warnings) == 1
    assert "top level must be a mapping" in cfg.load_warnings[0]
    assert (tmp_path / "config.yaml.invalid").exists()

    # a missing file stays the silent first-launch default — no warning
    assert load_config(user_path=str(tmp_path / "absent.yaml")).load_warnings == []


def test_every_persistable_config_field_round_trips(tmp_path: Path) -> None:
    """Walk every dataclasses.fields(Config) entry through dump -> load.

    A NEW Config option silently missing from to_dict()/dump_config_yaml()/
    _config_from_dict() must fail here forever: each field gets a non-default
    value (a type-derived flip unless its validator needs a curated one) and
    must survive a dump_config_yaml -> load_config round trip.
    """
    # Fields config.py deliberately keeps out of the persisted file:
    #   interval      - fixed cadence; dump annotates it, load always resets it
    #   sort_mode     - legacy mirror derived from sort_key on load
    #   name_filter   - transient live search; scrubbed by dump AND load
    #   load_warnings - runtime-only load diagnostics (compare=False)
    not_persisted = {"interval", "sort_mode", "name_filter", "load_warnings"}

    # Validated fields need values their load-side validators accept; anything
    # NOT listed falls back to the type-derived flip below, so a future field
    # is still exercised without touching this map.
    curated = {
        "timezone": "Asia/Seoul",
        "sort_key": "mem",                       # must be in SORTABLE_KEYS
        "theme": "nord",
        "summary_style": "tiles",                # must be in SUMMARY_STYLES
        "name_width": 42,                        # within NAME_WIDTH bounds
        "namespaces": ["team-a", "team-b"],
        "context": "ctx-roundtrip",
        "cpu_warn": 51, "cpu_crit": 61,
        "mem_warn": 52, "mem_crit": 62,
        "pvc_warn": 53, "pvc_crit": 63,
        "alertmanager_url": "http://am.local/api/v2/alerts",
        "health_probes": [
            {"name": "svc", "url": "http://h/health",
             "fields": {"ready": "ready=(\\w+)"}},
        ],
        "columns": ["name", "namespace", "cpu", "age"],  # valid registry keys
        "profile_name": "round-trip-prof",
        "profiles_by_context": {"ctx-roundtrip": "round-trip-prof"},
    }

    defaults = Config()
    expected: dict = {}
    for f in dataclasses.fields(Config):
        if f.name in not_persisted:
            continue
        default = getattr(defaults, f.name)
        if f.name in curated:
            value = curated[f.name]
        elif isinstance(default, bool):          # bool before int: bool is int
            value = not default
        elif isinstance(default, (int, float)):
            value = default + 7
        elif isinstance(default, str):
            value = default + "-nondefault"
        elif isinstance(default, list):
            value = list(default) + ["nondefault"]
        elif isinstance(default, dict):
            value = dict(default, nondefault="nondefault")
        else:
            raise AssertionError(
                f"no non-default strategy for new field Config.{f.name}; add a "
                "curated value here AND wire it into to_dict/dump/load")
        assert value != default, f"Config.{f.name}: flip produced the default"
        expected[f.name] = value

    cfgfile = tmp_path / "config.yaml"
    cfgfile.write_text(dump_config_yaml(Config(**expected)), encoding="utf-8")
    loaded = load_config(user_path=str(cfgfile))

    for name, value in expected.items():
        assert getattr(loaded, name) == value, (
            f"Config.{name} did not survive dump_config_yaml -> load_config; "
            "check to_dict()/dump_config_yaml()/_config_from_dict()")


def test_load_warnings_are_runtime_only(tmp_path: Path) -> None:
    cfg = Config(load_warnings=["boom"])
    assert "load_warnings" not in cfg.to_dict()
    assert "boom" not in dump_config_yaml(cfg)
    assert cfg == Config()  # excluded from equality-sensitive persistence paths

    cfgfile = tmp_path / "config.yaml"
    save_config(cfg, str(cfgfile))
    assert "boom" not in cfgfile.read_text(encoding="utf-8")
    assert load_config(user_path=str(cfgfile)).load_warnings == []
