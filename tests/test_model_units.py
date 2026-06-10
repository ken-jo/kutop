"""Direct unit tests for model.py quantity parsing and formatting helpers.

These feed every number the renderer shows; they previously had no direct
coverage (the 0.4.1 audit's biggest test gap).
"""

from __future__ import annotations

from kutop.model import fmt_age, fmt_cpu, fmt_mem, pct, to_mcpu, to_mi


def test_to_mcpu_standard_forms() -> None:
    assert to_mcpu("250m") == 250
    assert to_mcpu("1") == 1000
    assert to_mcpu("1.5") == 1500
    assert to_mcpu("0") == 0


def test_to_mcpu_rare_but_valid_suffixes() -> None:
    # nano/micro appear in API quantities even if kubectl top never emits them
    assert to_mcpu("1500000000n") == 1500
    assert to_mcpu("2500000u") == 2500
    assert to_mcpu("1e3m") == 1000


def test_to_mcpu_garbage_is_zero() -> None:
    assert to_mcpu("") == 0
    assert to_mcpu("-") == 0
    assert to_mcpu("<none>") == 0
    assert to_mcpu("oops") == 0


def test_to_mi_binary_and_decimal_suffixes() -> None:
    assert to_mi("256Mi") == 256
    assert to_mi("1Gi") == 1024
    assert to_mi("1024Ki") == 1
    assert to_mi("1Ti") == 1024 * 1024
    # decimal SI: 1G = 10^9 bytes = ~953 MiB
    assert to_mi("1G") == 953
    assert to_mi("1000k") == 0  # 1,000,000 bytes < 1 MiB
    assert to_mi("500M") == 476


def test_to_mi_exponent_form_round_trips_from_api() -> None:
    # the API preserves DecimalExponent serialization: memory: "1e9" stays "1e9"
    assert to_mi("1e9") == 953
    assert to_mi("2E6") == 1
    assert to_mi("1048576") == 1  # bare bytes


def test_to_mi_garbage_is_zero() -> None:
    assert to_mi("") == 0
    assert to_mi("-") == 0
    assert to_mi("<none>") == 0
    assert to_mi("12parsecs") == 0


def test_fmt_helpers() -> None:
    assert fmt_cpu(250) == "250m"
    assert fmt_cpu(1000) == "1"
    assert fmt_cpu(1500) == "1.5"
    assert fmt_mem(256) == "256Mi"
    assert fmt_mem(7680) == "7.5Gi"
    assert fmt_mem(2048) == "2Gi"
    assert pct(50, 200) == 25
    assert pct(1, 0) == 0  # unlimited -> 0, never a ZeroDivisionError
    assert fmt_age(None) == "-"
    assert fmt_age(90) == "1m"
    assert fmt_age(5400) == "1h"
    assert fmt_age(90061) == "1d"
