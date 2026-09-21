"""Opt-in mailbox services: configuration selects reviewed implementations."""
from .hw.ccci_mailbox import MailboxDispatcher
from .hw.ccci_swtp import LEGACY_MD1_SWTP, FixedTxPowerState, SoftwareTxPowerEndpoint


def _legacy_swtp(entry):
    if set(entry) != {"kind", "mode"}:
        raise ValueError("SWTP service requires only kind and explicit mode")
    endpoint = SoftwareTxPowerEndpoint(LEGACY_MD1_SWTP, FixedTxPowerState(entry["mode"]))
    abi = endpoint.abi
    return [(abi.request_channel, abi.request_id, endpoint)], {
        "kind": entry["kind"], "mode": entry["mode"],
        "request_channel": abi.request_channel, "request_id": abi.request_id,
        "response_channel": abi.response_channel, "response_id": abi.response_id,
        "state_source": "explicit-synthetic-pin-state", "rf_transmission_supported": False,
    }


SERVICE_FACTORIES = {"legacy-md1-swtp-analysis/v1": _legacy_swtp}


def build_mailbox_profile(profile):
    if not isinstance(profile, dict) or set(profile) != {"schema", "profile", "source", "services"}:
        raise ValueError("Invalid mailbox profile fields")
    if profile["schema"] != "firmwire.ccci-mailbox-profile/v1":
        raise ValueError("Unsupported mailbox profile schema")
    for name in ("profile", "source"):
        if not isinstance(profile[name], str) or not profile[name].strip():
            raise ValueError("Mailbox profile requires identity and provenance")
    entries = profile["services"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= 16:
        raise ValueError("Mailbox profile requires 1..16 services")
    routes, services = [], []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("kind"), str):
            raise ValueError("Invalid mailbox service")
        factory = SERVICE_FACTORIES.get(entry["kind"])
        if factory is None:
            raise ValueError("Unsupported mailbox service kind")
        new_routes, facts = factory(entry)
        routes.extend(new_routes)
        services.append(facts)
    dispatcher = MailboxDispatcher(routes)
    dispatcher.validate_channels({0, 0xe, 0x20, 0x22})
    return dispatcher, {"schema": "firmwire.ccci-mailbox/v1", "profile": profile["profile"],
                        "source": profile["source"], "services": services,
                        "analysis_only": True, "peer_connected": False,
                        "application_verified": False, "boot_verified": False,
                        "guest_delivery_verified": False}
