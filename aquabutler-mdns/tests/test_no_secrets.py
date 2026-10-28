"""The no-secrets guard for the mDNS TXT payload.

mDNS is unauthenticated and broadcast to every host on the customer's
segment. Anything in the TXT payload leaks to the customer's
neighbours. The policy (`docs/Ethernet-deployment-policy.md` §2) names
three things that must never appear:

* MQTT credentials,
* the OpenBalena device token,
* the customer's WiFi passphrase.

This test enforces the rule mechanically — not by review — by
rendering a payload that *deliberately* contains every secret-shaped
value the container could plausibly pick up from its environment, and
asserting that none of it reaches the advertised TXT record.

The env-var names below are the ones that actually exist on the
device: the balena supervisor injects `BALENA_API_KEY` and
`BALENA_SUPERVISOR_API_KEY` into every container, and the fleet's
per-customer MQTT credentials are injected as `MQTT_USER_<ID>` /
`MQTT_PASS_<ID>` by the provisioner.
"""
from __future__ import annotations

import os

import pytest

from aquabutler_mdns.identity import (
    EXPECTED_TXT_KEYS,
    Advertisement,
    resolve_sw_version,
    resolve_unit_id,
)
from aquabutler_mdns.avahi_service import parse_service_file, render_service_file


# Every environment variable that could plausibly hold a secret on the
# device. The publisher must never read any of these into the payload.
SECRET_ENV_VARS = (
    "BALENA_API_KEY",
    "RESIN_API_KEY",
    "BALENA_SUPERVISOR_API_KEY",
    "MQTT_BROKER_URL",
    "MQTT_USER",
    "MQTT_PASS",
    "MQTT_PASSWORD",
    "MQTT_USERNAME",
    "MQTT_TOKEN",
    "OPENBALENA_API_KEY",
    "OPENBALENA_TOKEN",
    "SUPABASE_KEY",
    "SUPABASE_SERVICE_KEY",
    "WIFI_PASS",
    "WIFI_PASSWORD",
    "WIFI_PSK",
    "WIFI_KEY",
    "WIFI_SSID",
    "PASSPHRASE",
    "PASSWORD",
    "SECRET",
    "TOKEN",
)

# The values planted in the environment for this test. None of them may
# appear in the rendered advertisement.
SECRET_SENTINELS = (
    "s3cr3t-mqtt-password",
    "s3cr3t-balena-token",
    "s3cr3t-wifi-passphrase",
    "s3cr3t-supabase-key",
)


def _plant_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in SECRET_ENV_VARS:
        monkeypatch.setenv(name, "s3cr3t-mqtt-password")
    # A couple of names that carry the sentinels verbatim so a naive
    # "contains a secret" assertion cannot pass by accident.
    monkeypatch.setenv("MQTT_PASS", "s3cr3t-mqtt-password")
    monkeypatch.setenv("BALENA_API_KEY", "s3cr3t-balena-token")
    monkeypatch.setenv("WIFI_PASS", "s3cr3t-wifi-passphrase")
    monkeypatch.setenv("SUPABASE_KEY", "s3cr3t-supabase-key")


def test_txt_key_set_is_exactly_the_expected_set(monkeypatch: pytest.MonkeyPatch):
    """The payload may carry exactly the five policy-named keys.

    This is the "TXT keys are exactly the expected set" acceptance
    criterion. A new key cannot be added without failing this test.
    """
    _plant_secrets(monkeypatch)
    monkeypatch.setenv("AQUABUTLER_UNIT_ID", "aqb0007abcd")
    monkeypatch.setenv("AQUABUTLER_SW_VERSION", "1.2.3")

    advertisement = Advertisement(
        unit_id="aqb0007abcd",
        customer_id="acme-pool-01",
        sw_version="1.2.3",
        transport="eth",
        ip="192.168.1.42",
    )
    keys = advertisement.txt_keys()
    assert keys == EXPECTED_TXT_KEYS
    assert keys == {"unit_id", "customer_id", "sw_version", "transport", "ip"}


def test_no_secret_reaches_the_txt_payload(monkeypatch: pytest.MonkeyPatch):
    """No secret-shaped value may appear in the rendered service file.

    The environment is planted with every secret the device could
    plausibly expose to this container. The rendered advertisement
    must contain none of them.
    """
    _plant_secrets(monkeypatch)
    monkeypatch.setenv("AQUABUTLER_UNIT_ID", "aqb0007abcd")
    monkeypatch.setenv("AQUABUTLER_SW_VERSION", "1.2.3")

    advertisement = Advertisement(
        unit_id=resolve_unit_id(),
        customer_id="acme-pool-01",
        sw_version=resolve_sw_version(),
        transport="eth",
        ip="192.168.1.42",
    )
    rendered = render_service_file(advertisement, host_name="aqb0007abcd.local")

    for sentinel in SECRET_SENTINELS:
        assert sentinel not in rendered, (
            f"secret {sentinel!r} leaked into the mDNS service file:\n{rendered}"
        )

    for name in SECRET_ENV_VARS:
        assert name not in rendered, (
            f"secret env var {name!r} leaked into the mDNS service file:\n{rendered}"
        )


def test_the_advertised_values_are_the_only_values(monkeypatch: pytest.MonkeyPatch):
    """Every TXT value must be one the container actually resolved.

    A value that is not in the resolved allow-list is a leak, even if
    it does not look like a secret — it might be a hostname, a path, or
    a credential under an unexpected name.
    """
    _plant_secrets(monkeypatch)
    monkeypatch.setenv("AQUABUTLER_UNIT_ID", "aqb0007abcd")

    advertisement = Advertisement(
        unit_id="aqb0007abcd",
        customer_id="acme-pool-01",
        sw_version="1.2.3",
        transport="eth",
        ip="192.168.1.42",
    )
    allowed = {
        "aqb0007abcd",
        "acme-pool-01",
        "1.2.3",
        "eth",
        "192.168.1.42",
    }
    for key, value in advertisement.txt_records():
        assert value in allowed, f"TXT {key}={value!r} is not an expected value"


def test_no_environment_variable_is_read_outside_the_allow_list():
    """The resolver must only read the identity-related env vars.

    This guards against a future change quietly widening the set of
    environment variables the publisher consumes.
    """
    from aquabutler_mdns import identity

    # The identity module reads only these env vars (directly or via
    # the transport module's operator override).
    allowed = set(identity.DEVICE_UUID_ENV_VARS) | {
        "AQUABUTLER_SW_VERSION",
        "AQUABUTLER_TRANSPORT",
    }
    src = (identity.__file__ or "")
    assert src.endswith("identity.py")
    # Every env-var literal in the identity module must be allow-listed.
    import re

    text = open(src, encoding="utf-8").read()
    referenced = set(re.findall(r'"([A-Z][A-Z0-9_]{3,})"', text))
    unexpected = {
        name
        for name in referenced
        if name not in allowed
        and not name.startswith(("EXPECTED_", "DEFAULT_", "SERVICE_", "DEVICE_", "SHORT_"))
        and name
        not in ("TXT_RECORDS", "XML_ILLEGAL_RE")
    }
    assert not unexpected, f"identity.py reads unexpected env vars: {unexpected}"


def test_environment_has_no_leakable_values_in_the_default_payload(
    monkeypatch: pytest.MonkeyPatch,
):
    """With a bare environment the payload carries only identity.

    On a device where nothing resolved, the payload must be empty or
    near-empty — never filled with something else.
    """
    for name in list(os.environ):
        if name.startswith(("BALENA_", "RESIN_", "MQTT_", "WIFI_", "SUPABASE_")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AQUABUTLER_UNIT_ID", "aqb0007abcd")

    advertisement = Advertisement(
        unit_id=resolve_unit_id(),
        customer_id="",
        sw_version=resolve_sw_version(),
        transport="",
        ip="",
    )
    assert advertisement.txt_keys() <= {"unit_id"}


def test_service_file_contains_only_txt_records(monkeypatch: pytest.MonkeyPatch):
    """The parsed service file's TXT keys are exactly the expected set."""
    _plant_secrets(monkeypatch)
    advertisement = Advertisement(
        unit_id="aqb0007abcd",
        customer_id="acme-pool-01",
        sw_version="1.2.3",
        transport="wifi",
        ip="192.168.1.42",
    )
    rendered = render_service_file(advertisement, host_name="aqb0007abcd.local")
    parsed = parse_service_file(rendered)
    assert parsed.txt_keys == EXPECTED_TXT_KEYS
