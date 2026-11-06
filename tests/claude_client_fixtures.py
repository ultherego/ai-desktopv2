"""Synthetic offline adapter replies; no system executable or profile access."""
from ai_desktop.analysis_contract import CLIENT_VERSION
from ai_desktop.client_compat import FIRST_INVALID_OPTION, INVALID_OPTION


def compatibility_results(version=CLIENT_VERSION):
    return [
        (0, (version + " (Claude Code)").encode(), b""),
        (1, b"", ("error: unknown option '" + INVALID_OPTION + "'\n").encode()),
        (1, b"", ("error: unknown option '" + INVALID_OPTION + "'\n").encode()),
        (1, b"", ("error: unknown option '" + FIRST_INVALID_OPTION + "'\n").encode()),
        (1, b'{"loggedIn":false,"authMethod":"none","configDirectory":"/home/client/profile"}', b""),
    ]
