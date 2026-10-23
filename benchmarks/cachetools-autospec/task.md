# 修复 cachedmethod 与 unittest.mock autospec 的回归

项目：cachetools。现象来自公开 issue https://github.com/tkem/cachetools/issues/443 。

一个类同时有普通实例方法与 `@cachedmethod` 装饰的方法。用 `unittest.mock.create_autospec` 为该类创建实例 mock 后，普通方法可用一个业务参数调用；装饰方法却报 `TypeError: missing a required argument`。这在此前的版本中可以正常工作。

可用如下最小结构复现：

```python
from unittest import mock
from cachetools import cachedmethod

class Service:
    def __init__(self):
        self.cache = {}

    def plain(self, key):
        return key

    @cachedmethod(lambda self: self.cache)
    def cached(self, key):
        return key

service = mock.create_autospec(Service).return_value
service.plain("key")
service.cached("key")  # 当前失败
```

请读取现有实现与测试，复现问题并修复。`instance=True` 创建方式、位置参数和关键字参数调用都应可用；真实实例上的缓存行为、类访问调用和继承行为应保持正常。现有测试文件受保护，不得修改或删除；新增回归测试请放在新的测试文件。请执行相关测试并报告实际结果。

现有测试命令：`python -m pytest -q -o pythonpath=src tests/test_cachedmethod.py`。
