"""Small BrowserGym stub used when browser tools are disabled.

OpenHands imports the BrowsingAgent unconditionally during agent registration.
The PoC-generation harness does not expose browsing tools, but the import still
requires BrowserGym symbols.  Installing the real browser stack pulls Playwright,
which is not available for the musl Python used in this environment.
"""

from __future__ import annotations


class HighLevelActionSet:
    def __init__(self, *args, **kwargs) -> None:
        self.args = args
        self.kwargs = kwargs
        self.action_set = {}

    def describe(self, *args, **kwargs) -> str:
        return "Browser actions are disabled in this benchmark harness."
