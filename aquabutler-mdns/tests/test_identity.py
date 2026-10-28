"""Tests for identity resolution.

The identity values are what a technician reads off the mDNS record to
know *which* unit they are looking at. They must be stable (not the
address), readable (not a 64-char hex blob in the host name), and
absent rather than wrong when they cannot be resolved.
"""
from __future__ import annotations

import pytest

from aquabutler_mdns.identity import (
    DEFAULT_CUSTOMER_ID_PATH,
    DEVICE_UUID_ENV_VARS,
    Advertisement,
    is_valid_unit_id,
    read_customer_id,
    resolve_sw_version,
    resolve_unit_id,
    short_unit_id,
)


def test_customer_id_path_is_the_provisioners_path():
    """The provisioner writes the customer ID to exactly this path."""
    assert DEFAULT_CUSTOMER_ID_PATH == "/mnt/data/customer_id"


def test_read_customer_id(tmp_path):
    path = tmp_path / "customer_id"
    path.write_text("acme-pool-01\n", encoding="utf-8")
    assert read_customer_id(path) == "acme-pool-01"


def test_read_customer_id_missing_file(tmp_path):
    assert read_customer_id(tmp_path / "nope") == ""


def test_read_customer_id_strips_whitespace(tmp_path):
    path = tmp_path / "customer_id"
    path.write_text("  acme-pool-01  \n", encoding="utf-8")
    assert read_customer_id(path) == "acme-pool-01"


def test_resolve_unit_id_prefers_the_explicit_override():
    env = {
        "AQUABUTLER_UNIT_ID": "aqb0007abcd",
        "BALENA_DEVICE_UUID": "balena-uuid",
    }
    assert resolve_unit_id(env) == "aqb0007abcd"


def test_resolve_unit_id_falls_back_to_the_balena_device_uuid():
    env = {"BALENA_DEVICE_UUID": "cb6f09d18ab4c08556f54a5bd7cfd353"}
    assert resolve_unit_id(env) == "cb6f09d18ab4c08556f54a5bd7cfd353"


def test_resolve_unit_id_accepts_the_resin_alias():
    env = {"RESIN_DEVICE_UUID": "cb6f09d18ab4"}
    assert resolve_unit_id(env) == "cb6f09d18ab4"


def test_resolve_unit_id_absent():
    assert resolve_unit_id({}) == ""


def test_device_uuid_env_vars_are_the_documented_ones():
    assert "BALENA_DEVICE_UUID" in DEVICE_UUID_ENV_VARS
    assert "AQUABUTLER_UNIT_ID" in DEVICE_UUID_ENV_VARS


def test_resolve_sw_version():
    assert resolve_sw_version({"AQUABUTLER_SW_VERSION": "1.2.3"}) == "1.2.3"
    assert resolve_sw_version({}) == ""


def test_short_unit_id_truncates_a_balena_uuid():
    uuid = "cb6f09d18ab4c08556f54a5bd7cfd353d4907c4a61998ba8a54cd9f2abc5ee"
    short = short_unit_id(uuid)
    assert short == "cb6f09d18ab4"
    assert len(short) == 12


def test_short_unit_id_leaves_a_short_id_alone():
    assert short_unit_id("aqb0007abcd") == "aqb0007abcd"


def test_short_unit_id_empty():
    assert short_unit_id("") == ""
    assert short_unit_id(None) == ""


def test_is_valid_unit_id():
    assert is_valid_unit_id("aqb0007abcd")
    assert is_valid_unit_id("cb6f09d18ab4c08556f54a5bd7cfd353")
    assert not is_valid_unit_id("")
    assert not is_valid_unit_id(None)
    assert not is_valid_unit_id("-leading-dash")
    assert not is_valid_unit_id("trailing-dash-")
    assert not is_valid_unit_id("has space")
    assert not is_valid_unit_id("has_underscore")
    assert not is_valid_unit_id("x" * 64)


def test_advertisement_txt_records_preserve_the_canonical_order():
    advertisement = Advertisement(
        unit_id="u",
        customer_id="c",
        sw_version="v",
        transport="eth",
        ip="1.2.3.4",
    )
    assert [k for k, _ in advertisement.txt_records()] == [
        "unit_id",
        "customer_id",
        "sw_version",
        "transport",
        "ip",
    ]


def test_advertisement_omits_empty_values():
    advertisement = Advertisement(unit_id="u", transport="", ip="")
    assert advertisement.txt_records() == [("unit_id", "u")]
