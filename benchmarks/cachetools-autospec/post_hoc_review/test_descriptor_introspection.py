"""Post-hoc compatibility review; deliberately separate from the frozen oracle.

Discovered after the first model candidate passed the original hidden tests.
These checks may be shown to the follow-up agent and are not a fresh holdout.
"""

import inspect
from unittest import mock

import pytest
from cachetools import cachedmethod


class Service:
    def __init__(self):
        self.cache = {}

    @cachedmethod(lambda self: self.cache)
    def cached(self, key):
        return key


def test_raw_cachedmethod_descriptor_accepts_autospec():
    raw = inspect.getattr_static(Service, "cached")
    mock.create_autospec(raw)


@pytest.mark.parametrize(
    "predicate", [inspect.iscoroutinefunction, inspect.isgeneratorfunction]
)
def test_raw_cachedmethod_descriptor_has_ordinary_sync_introspection(predicate):
    raw = inspect.getattr_static(Service, "cached")
    assert predicate(raw) is False
