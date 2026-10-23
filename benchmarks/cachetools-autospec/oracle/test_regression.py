"""Independent black-box oracle for cachetools issue443.

Kept outside project.zip. It contains no reference implementation or patch.
"""

from unittest import mock

import pytest
from cachetools import cachedmethod


class Service:
    def __init__(self):
        self.cache = {}
        self.calls = 0

    def plain(self, key):
        return key

    @cachedmethod(lambda self: self.cache)
    def cached(self, key):
        self.calls += 1
        return (key, self.calls)


@pytest.mark.parametrize("class_mock", [False, True], ids=["instance-autospec", "class-autospec"])
@pytest.mark.parametrize("keyword", [False, True], ids=["positional", "keyword"])
def test_decorated_autospec_calls_accept_business_argument(class_mock, keyword):
    service = mock.create_autospec(Service).return_value if class_mock else mock.create_autospec(Service, instance=True)
    service.cached.return_value = "from-mock"
    service.plain("item")  # The equivalent undecorated method is callable.
    if keyword:
        assert service.cached(key="item") == "from-mock"
        service.cached.assert_called_once_with(key="item")
    else:
        assert service.cached("item") == "from-mock"
        service.cached.assert_called_once_with("item")


def test_real_instance_still_caches_values():
    service = Service()
    assert service.cached("item") == ("item", 1)
    assert service.cached("item") == ("item", 1)
    assert service.calls == 1
    assert service.cached("other") == ("other", 2)


def test_class_call_uses_the_supplied_instance_cache():
    service = Service()
    assert Service.cached(service, "item") == ("item", 1)
    assert service.cached("item") == ("item", 1)
    assert Service.cached is Service.cached
    with pytest.raises(TypeError):
        Service.cached()


def test_inherited_class_and_instance_calls_share_the_same_cache():
    class Derived(Service):
        pass

    service = Derived()
    assert Derived.cached(service, key="item") == ("item", 1)
    assert service.cached(key="item") == ("item", 1)
    assert service.calls == 1
