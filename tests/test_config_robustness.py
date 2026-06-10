"""Regression tests for the config robustness fixes (0.4.1 audit).

Covers: the saved YAML must stay parseable for ANY user value (a health-probe
regex containing a quote used to corrupt the file and silently reset every
preference), and the profile-owned strip/persist contract must only touch the
fields the active profile actually supplies.
"""

from __future__ import annotations

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
