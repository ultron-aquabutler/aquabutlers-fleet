"""Tests for the rendered avahi service file.

The acceptance criterion is: "a unit test that verifies the service
file parses and the TXT keys are exactly the expected set". These
tests parse the *rendered file* back — not the in-memory objects —
so they catch a malformed render, not just a wrong object.
"""
from __future__ import annotations

import pytest

from aquabutler_mdns.avahi_service import (
    parse_service_file,
    render_service_file,
    sanitize_txt_value,
    xml_escape,
)
from aquabutler_mdns.identity import (
    EXPECTED_TXT_KEYS,
    SERVICE_PORT,
    SERVICE_TYPE,
    Advertisement,
)


def _full_advertisement() -> Advertisement:
    return Advertisement(
        unit_id="cb6f09d18ab4c08556f54a5bd7cfd353d4907c4a61998ba8a54cd9f2abc5ee",
        customer_id="acme-pool-01",
        sw_version="0.1.0",
        transport="eth",
        ip="192.168.1.42",
    )


def test_service_file_parses():
    rendered = render_service_file(_full_advertisement(), host_name="cb6f09d18ab4.local")
    parsed = parse_service_file(rendered)
    assert parsed.service_type == SERVICE_TYPE
    assert parsed.port == SERVICE_PORT
    assert parsed.host_name == "cb6f09d18ab4.local"


def test_txt_keys_are_exactly_the_expected_set():
    rendered = render_service_file(_full_advertisement(), host_name="cb6f09d18ab4.local")
    parsed = parse_service_file(rendered)
    assert parsed.txt_keys == EXPECTED_TXT_KEYS


def test_txt_values_round_trip():
    rendered = render_service_file(_full_advertisement(), host_name="cb6f09d18ab4.local")
    parsed = parse_service_file(rendered)
    assert parsed.txt["unit_id"].startswith("cb6f09d18ab4")
    assert parsed.txt["customer_id"] == "acme-pool-01"
    assert parsed.txt["sw_version"] == "0.1.0"
    assert parsed.txt["transport"] == "eth"
    assert parsed.txt["ip"] == "192.168.1.42"


def test_service_type_is_aquabutler_tcp():
    rendered = render_service_file(_full_advertisement(), host_name="x.local")
    assert SERVICE_TYPE == "_aquabutler._tcp"
    assert "<type>_aquabutler._tcp</type>" in rendered


def test_service_port_is_5150():
    rendered = render_service_file(_full_advertisement(), host_name="x.local")
    assert SERVICE_PORT == 5150
    assert "<port>5150</port>" in rendered


def test_unresolved_values_are_omitted_not_empty():
    """A key whose value could not be resolved is omitted entirely."""
    advertisement = Advertisement(unit_id="aqb0007abcd")
    rendered = render_service_file(advertisement, host_name="aqb0007abcd.local")
    parsed = parse_service_file(rendered)
    assert parsed.txt_keys == {"unit_id"}
    assert "customer_id=" not in rendered
    assert "transport=" not in rendered
    assert "ip=" not in rendered


def test_duplicate_keys_are_rejected():
    """A malformed file with a duplicate key must not parse silently."""
    bad = """<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
  <name>x</name>
  <service protocol="ipv4">
    <type>_aquabutler._tcp</type>
    <port>5150</port>
    <txt-record>ip=1.2.3.4</txt-record>
    <txt-record>ip=5.6.7.8</txt-record>
  </service>
</service-group>
"""
    with pytest.raises(ValueError, match="duplicate"):
        parse_service_file(bad)


def test_malformed_xml_is_rejected():
    with pytest.raises(ValueError):
        parse_service_file("<service-group><service>")


def test_missing_type_is_rejected():
    bad = """<?xml version="1.0"?>
<service-group>
  <name>x</name>
  <service protocol="ipv4"><port>5150</port></service>
</service-group>
"""
    with pytest.raises(ValueError, match="type"):
        parse_service_file(bad)


def test_missing_port_is_rejected():
    bad = """<?xml version="1.0"?>
<service-group>
  <name>x</name>
  <service protocol="ipv4"><type>_aquabutler._tcp</type></service>
</service-group>
"""
    with pytest.raises(ValueError, match="port"):
        parse_service_file(bad)


def test_txt_record_without_equals_is_rejected():
    bad = """<?xml version="1.0"?>
<service-group>
  <name>x</name>
  <service protocol="ipv4">
    <type>_aquabutler._tcp</type><port>5150</port>
    <txt-record>justakey</txt-record>
  </service>
</service-group>
"""
    with pytest.raises(ValueError, match="key=value"):
        parse_service_file(bad)


def test_render_is_stable_for_the_same_payload():
    """The same payload must render byte-identically.

    Stability is what lets the publisher skip the avahi reload when
    nothing changed.
    """
    a = render_service_file(_full_advertisement(), host_name="x.local")
    b = render_service_file(_full_advertisement(), host_name="x.local")
    assert a == b


def test_values_with_xml_special_characters_are_escaped():
    advertisement = Advertisement(unit_id="a&b<c>d", customer_id='q"uote')
    rendered = render_service_file(advertisement, host_name="x.local")
    parsed = parse_service_file(rendered)
    assert parsed.txt["unit_id"] == "a&b<c>d"
    assert parsed.txt["customer_id"] == 'q"uote'


def test_control_characters_are_stripped():
    assert sanitize_txt_value("a\nb") == "ab"
    assert sanitize_txt_value("a\x00b") == "ab"
    assert sanitize_txt_value("ok") == "ok"


def test_xml_escape_handles_the_five_entities():
    assert xml_escape("&") == "&amp;"
    assert xml_escape("<") == "&lt;"
    assert xml_escape(">") == "&gt;"
    assert xml_escape('"') == "&quot;"
