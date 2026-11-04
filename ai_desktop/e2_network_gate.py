"""Trusted E2 integration: approved data + isolated process before any egress.

Credential-free connectivity proof only. No Claude invocation or OAuth access.
"""
from . import e2_broker, e2_boundary, e2_egress


def gated_probe(report, approved_sha256, *, allow_network=False,
                process=e2_boundary.execute, network_probe=e2_egress.probe):
    # Explicit deny first: neither sandbox nor network starts on default path.
    if allow_network is not True:
        raise e2_broker.BrokerDenied("BROKER: REFUSED")
    try:
        intent = e2_broker.prepare(report, approved_sha256, process=process)
        if type(intent) is not dict or intent != {
            "operation": "diagnostic.readonly",
            "target": "claude-analysis",
            "payload_sha256": approved_sha256,
        }:
            raise e2_broker.BrokerDenied("BROKER: REFUSED")
        result = network_probe(allow_network=True)
        if type(result) is not dict or result != {
            "tls": "VERIFIED",
            "endpoint": e2_egress.HOST,
            "authentication": "NOT_TESTED",
        }:
            raise e2_broker.BrokerDenied("BROKER: REFUSED")
        return result
    except (e2_egress.EgressDenied, e2_broker.BrokerDenied,
            OSError, ValueError, TypeError):
        raise e2_broker.BrokerDenied("BROKER: REFUSED") from None
