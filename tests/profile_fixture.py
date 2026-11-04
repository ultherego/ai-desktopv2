"""Default settings for controller tests that do not exercise profile storage.

The real configuration, selection and invalid-profile behavior have their own
filesystem and management integration tests. Other synthetic CLI harnesses must
never read a developer's or CI runner's private agent configuration.
"""

DEFAULT_PROFILE = {"provider": "claude", "model": "default", "effort": "default", "instructions": "",
                   "profile": "default", "access": "changes"}
