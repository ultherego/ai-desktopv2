"""Synthetic offline adapter replies; no system executable or profile access."""
from ai_desktop.analysis_contract import CLIENT_VERSION


def compatibility_results(version=CLIENT_VERSION):
    return [
        (0, (version + " (Claude Code)").encode(), b""),
        (1, b'{"loggedIn":false,"authMethod":"none","configDirectory":"/home/client/profile"}', b""),
        (1, b'{"loggedIn":false,"authMethod":"none","configDirectory":"/home/client/profile"}', b""),
        (2, b"", b"unknown option --ai-desktop-invalid-compatibility-option"),
        (1, b'{"loggedIn":false,"authMethod":"none","configDirectory":"/home/client/profile"}', b""),
    ]
