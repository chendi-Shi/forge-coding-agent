"""Independent, deterministic checks for the Pearson-classification regression.

Exact-moment fixtures use the same public attribute override technique as the
upstream maintainer's regression tests; they are not claimed to be sampled data.
The ordinary sample vectors below exercise the existing IV/V/VI classifications.
"""

import pytest
from boltons.statsutils import Stats


@pytest.mark.parametrize("skewness,kurtosis", [
    (2.0, 3.0),
    (-2.0, 3.0),
    (4.0, 12.0),
    (-4.0, 12.0),
    (8.0, 48.0),
])
def test_exact_boundary_classifies_without_division_by_zero(skewness, kurtosis):
    stats = Stats([0])
    stats.skewness = skewness
    stats.kurtosis = kurtosis
    assert stats.pearson_type == 1
    # Access through the cached property must remain stable too.
    assert stats.pearson_type == 1


@pytest.mark.parametrize("skewness,kurtosis,expected", [
    (0.0, 3.0, 0),
    (2.0, 9.0, 3),
    (-2.0, 9.0, 3),
    (2.0, 9.1, 3),
    (0.0, 0.0, 2),
    (0.0, 2.0, 2),
    (0.0, 6.0, 7),
])
def test_existing_special_family_and_rounding_precedence(skewness, kurtosis, expected):
    stats = Stats([0])
    stats.skewness = skewness
    stats.kurtosis = kurtosis
    assert stats.pearson_type == expected


@pytest.mark.parametrize("sample,expected", [
    ([8, 4, 4, 4, 5, 1, 6, 5], 4),
    ([5, 5, 2, 4, 5, 9, 5, 5], 5),
    ([0, 0, 0, 0, 0, 0, -11, 17], 6),
])
def test_concrete_nonnegative_criterion_families(sample, expected):
    assert Stats(sample).pearson_type == expected
    assert Stats([-value for value in sample]).pearson_type == expected


def test_other_statistics_remain_unchanged():
    stats = Stats(range(20))
    assert stats.mean == 9.5
    assert stats.variance == 33.25
    assert stats.median == 9.5
    assert stats.skewness == pytest.approx(0)
    # Exact fourth centered sum divided by (n - 1) * variance**2 for range(20).
    assert stats.kurtosis == pytest.approx(4772 / 2527)
