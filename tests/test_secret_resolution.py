from __future__ import annotations

import pytest

from conftest import load_plugin_pkg
from gateway.config import PlatformConfig


API_KEY = "synthetic-recall-key"
WEBHOOK_SECRET = "whsec_c3ludGhldGlj"
CALLBACK_URL = "https://callback.example"


@pytest.fixture(autouse=True)
def isolated_secret_resolution(monkeypatch):
    import agent.secret_scope as secret_scope
    import gateway.platforms._shared as shared
    from gateway.platform_registry import platform_registry

    prior_multiplex = secret_scope.is_multiplex_active()
    prior_cache = shared._UNSCOPED_PROFILE_SECRETS
    prior_registration = platform_registry.snapshot_registration(
        "zoom_meeting_chat", scope=None
    )
    scope_token = secret_scope.set_secret_scope(None)
    shared._UNSCOPED_PROFILE_SECRETS = None
    monkeypatch.delenv("RECALL_API_KEY", raising=False)
    monkeypatch.delenv("RECALL_WEBHOOK_SECRET", raising=False)
    try:
        yield
    finally:
        current_registration = platform_registry.snapshot_registration(
            "zoom_meeting_chat", scope=None
        )
        assert platform_registry.restore_registration(
            "zoom_meeting_chat",
            current_registration,
            prior_registration,
            scope=None,
        )
        shared._UNSCOPED_PROFILE_SECRETS = prior_cache
        secret_scope.reset_secret_scope(scope_token)
        secret_scope.set_multiplex_active(prior_multiplex)


def _platform_config() -> PlatformConfig:
    return PlatformConfig(
        enabled=True,
        extra={"callback_public_base_url": CALLBACK_URL},
    )


def _registered_platform(package_name: str):
    load_plugin_pkg(package_name)
    adapter_mod = __import__(f"{package_name}.adapter", fromlist=["register"])
    from gateway.platform_registry import PlatformEntry, platform_registry

    class RegistrationContext:
        platform = None

        def register_platform(self, **kwargs):
            self.platform = kwargs
            platform_registry.register(PlatformEntry(source="builtin", **kwargs))

    ctx = RegistrationContext()
    adapter_mod.register(ctx)
    assert ctx.platform is not None
    return ctx.platform


def _install_external_snapshot(monkeypatch, secrets):
    import agent.secret_scope as secret_scope

    calls = []

    def fake_build(home):
        calls.append(home)
        return dict(secrets)

    monkeypatch.setattr(secret_scope, "build_profile_secret_scope", fake_build)
    return calls


def test_registered_startup_gate_validation_and_construction_use_external_snapshot(
    monkeypatch,
):
    import agent.secret_scope as secret_scope

    secret_scope.set_multiplex_active(True)
    calls = _install_external_snapshot(
        monkeypatch,
        {
            "RECALL_API_KEY": f"  {API_KEY}  ",
            "RECALL_WEBHOOK_SECRET": f"  {WEBHOOK_SECRET}  ",
        },
    )
    platform = _registered_platform("zoom_secret_primary")
    pconfig = _platform_config()

    assert platform["check_fn"]() is True
    assert platform["validate_config"](pconfig) is True
    adapter = platform["adapter_factory"](pconfig)
    assert adapter.zconfig.api_key == API_KEY
    assert adapter.zconfig.webhook_secret == WEBHOOK_SECRET
    assert platform["check_fn"]() is True
    assert len(calls) == 1, "the external-secret snapshot must be process-cached"


def test_environment_credentials_take_precedence_without_building_snapshot(monkeypatch):
    import agent.secret_scope as secret_scope

    secret_scope.set_multiplex_active(True)
    monkeypatch.setenv("RECALL_API_KEY", f"  {API_KEY}-env  ")
    monkeypatch.setenv("RECALL_WEBHOOK_SECRET", "  whsec_ZW52  ")
    calls = _install_external_snapshot(
        monkeypatch,
        {
            "RECALL_API_KEY": f"{API_KEY}-external",
            "RECALL_WEBHOOK_SECRET": "whsec_ZXh0ZXJuYWw",
        },
    )
    platform = _registered_platform("zoom_secret_env")
    adapter = platform["adapter_factory"](_platform_config())

    assert platform["check_fn"]() is True
    assert adapter.zconfig.api_key == f"{API_KEY}-env"
    assert adapter.zconfig.webhook_secret == "whsec_ZW52"
    assert calls == []


@pytest.mark.parametrize(
    ("environment", "external", "expected"),
    [
        ({}, {}, False),
        ({}, {"RECALL_API_KEY": API_KEY}, False),
        ({}, {"RECALL_WEBHOOK_SECRET": WEBHOOK_SECRET}, False),
        (
            {},
            {
                "RECALL_API_KEY": API_KEY,
                "RECALL_WEBHOOK_SECRET": "not-a-webhook-secret",
            },
            False,
        ),
        (
            {},
            {"RECALL_API_KEY": API_KEY, "RECALL_WEBHOOK_SECRET": "whsec_"},
            False,
        ),
        (
            {},
            {"RECALL_API_KEY": API_KEY, "RECALL_WEBHOOK_SECRET": "whsec_%%%"},
            False,
        ),
        (
            {"RECALL_API_KEY": "", "RECALL_WEBHOOK_SECRET": ""},
            {"RECALL_API_KEY": API_KEY, "RECALL_WEBHOOK_SECRET": WEBHOOK_SECRET},
            False,
        ),
    ],
)
def test_startup_gate_fails_closed_for_missing_blank_or_malformed_credentials(
    monkeypatch, environment, external, expected
):
    import agent.secret_scope as secret_scope

    secret_scope.set_multiplex_active(True)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    _install_external_snapshot(monkeypatch, external)
    platform = _registered_platform("zoom_secret_invalid")

    assert platform["check_fn"]() is expected
    assert platform["validate_config"](_platform_config()) is expected


def test_external_snapshot_build_failure_fails_closed(monkeypatch):
    import agent.secret_scope as secret_scope

    secret_scope.set_multiplex_active(True)

    def fail_build(_home):
        raise RuntimeError("external secret source unavailable")

    monkeypatch.setattr(secret_scope, "build_profile_secret_scope", fail_build)
    platform = _registered_platform("zoom_secret_source_failure")

    assert platform["check_fn"]() is False
    assert platform["validate_config"](_platform_config()) is False


@pytest.mark.parametrize(
    ("secondary", "secondary_ready"),
    [
        (
            {
                "RECALL_API_KEY": "secondary-key",
                "RECALL_WEBHOOK_SECRET": "whsec_c2Vjb25kYXJ5",
            },
            True,
        ),
        ({}, False),
        ({"RECALL_API_KEY": "secondary-key"}, False),
        ({"RECALL_WEBHOOK_SECRET": "whsec_c2Vjb25kYXJ5"}, False),
        ({"RECALL_API_KEY": "", "RECALL_WEBHOOK_SECRET": ""}, False),
    ],
)
def test_primary_secondary_primary_secret_isolation(
    monkeypatch, secondary, secondary_ready
):
    import agent.secret_scope as secret_scope

    secret_scope.set_multiplex_active(True)
    calls = _install_external_snapshot(
        monkeypatch,
        {"RECALL_API_KEY": API_KEY, "RECALL_WEBHOOK_SECRET": WEBHOOK_SECRET},
    )
    platform = _registered_platform("zoom_secret_isolation")
    pconfig = _platform_config()

    primary_before = platform["adapter_factory"](pconfig).zconfig
    assert platform["check_fn"]() is True
    assert len(calls) == 1

    monkeypatch.setenv("RECALL_API_KEY", "primary-environment-key")
    monkeypatch.setenv(
        "RECALL_WEBHOOK_SECRET", "whsec_cHJpbWFyeS1lbnZpcm9ubWVudA"
    )

    secondary_token = secret_scope.set_secret_scope(secondary, profile_home="/secondary")
    try:
        assert platform["check_fn"]() is secondary_ready
        assert platform["validate_config"](pconfig) is secondary_ready
        secondary_config = platform["adapter_factory"](pconfig).zconfig
        if secondary_ready:
            assert secondary_config.api_key == secondary["RECALL_API_KEY"]
            assert secondary_config.webhook_secret == secondary["RECALL_WEBHOOK_SECRET"]
        else:
            with pytest.raises(ValueError):
                secondary_config.validate_ready()
            assert secondary_config.api_key == secondary.get("RECALL_API_KEY", "").strip()
            assert secondary_config.webhook_secret == secondary.get(
                "RECALL_WEBHOOK_SECRET", ""
            ).strip()
        assert len(calls) == 1, "a scoped profile must never consult the primary fallback"
    finally:
        secret_scope.reset_secret_scope(secondary_token)

    primary_after = platform["adapter_factory"](pconfig).zconfig
    assert primary_before.api_key == API_KEY
    assert primary_before.webhook_secret == WEBHOOK_SECRET
    assert primary_after.api_key == "primary-environment-key"
    assert primary_after.webhook_secret == "whsec_cHJpbWFyeS1lbnZpcm9ubWVudA"
    assert len(calls) == 1


def test_single_profile_environment_behavior_is_unchanged(monkeypatch):
    import agent.secret_scope as secret_scope

    secret_scope.set_multiplex_active(False)
    monkeypatch.setenv("RECALL_API_KEY", API_KEY)
    monkeypatch.setenv("RECALL_WEBHOOK_SECRET", WEBHOOK_SECRET)
    calls = _install_external_snapshot(monkeypatch, {})
    platform = _registered_platform("zoom_secret_single_profile")

    assert platform["check_fn"]() is True
    adapter = platform["adapter_factory"](_platform_config())
    assert adapter.zconfig.api_key == API_KEY
    assert adapter.zconfig.webhook_secret == WEBHOOK_SECRET
    assert calls == []
