SAMPLE_PROMPT = "修复订单金额计算模块，使全部现有测试通过。先阅读代码和测试、复现失败，再修改实现。不要删除或放宽测试；运行完整测试并总结修复原因。"
SAMPLE_TEST = "python -m unittest discover -s tests -v"
SAMPLE_FILES = {
    "README.md": "# Order totals\n\nAmounts use integer cents. Decimal percentage discounts use ROUND_HALF_UP.\nShipping is free at 10000 cents after discount. Reject negative price/quantity and discounts outside [0, 100].\n\nRun: python -m unittest discover -s tests -v\n",
    "orders.py": '''"""Order calculation; all amounts returned as integer cents."""
from decimal import Decimal

def total_cents(items, discount_percent=0, shipping_cents=500):
    subtotal = sum(price * quantity for price, quantity in items)
    discounted = int(Decimal(subtotal) * (1 - Decimal(str(discount_percent)) / 100))
    return discounted + (0 if subtotal >= 10000 else shipping_cents)
''',
    "tests/test_orders.py": '''import unittest
from orders import total_cents

class OrderTests(unittest.TestCase):
    def test_half_up_rounding(self):
        self.assertEqual(total_cents([(101, 1)], 50, 0), 51)
    def test_shipping_after_discount(self):
        self.assertEqual(total_cents([(10000, 1)], 10), 9500)
    def test_free_shipping_boundary(self):
        self.assertEqual(total_cents([(10000, 1)]), 10000)
    def test_multiple_items(self):
        self.assertEqual(total_cents([(1000, 2), (2500, 1)], 10), 4550)
    def test_invalid_inputs(self):
        for items, discount in [([(-1, 1)], 0), ([(1, -2)], 0), ([(100, 1)], -1), ([(100, 1)], 101)]:
            with self.subTest(items=items, discount=discount):
                with self.assertRaises(ValueError):
                    total_cents(items, discount)

if __name__ == "__main__":
    unittest.main()
''',
    ".gitignore": "__pycache__/\n*.pyc\n.pytest_cache/\n",
}
