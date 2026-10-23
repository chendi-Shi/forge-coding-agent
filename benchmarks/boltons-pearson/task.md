# Repair Pearson family classification at an exact boundary

The `Stats.pearson_type` property can raise `ZeroDivisionError` for exact
skewness/kurtosis boundary values instead of classifying them.

A deterministic reproduction uses the public cached-statistic attributes:

```python
from boltons.statsutils import Stats

stats = Stats([0])
stats.skewness = 2.0
stats.kurtosis = 3.0
print(stats.pearson_type)
```

This boundary should classify as Pearson Type I (the integer `1`). Mirroring
the skewness should give the same family rather than crashing.

Fix the classifier in `boltons/statsutils.py`. Preserve existing Normal,
Gamma, symmetric-family, and Type IV/V/VI behavior, including the configured
rounding rules. Add deterministic regression coverage and avoid random sampling
or assertions that accept any family number.

Run the repository's visible statistics tests:

```sh
python -m pytest -q tests/test_statsutils.py
```

The supplied workspace is the pinned upstream version exhibiting the boundary
bug. Keep the change focused on the regression and its tests.

Existing test files are protected and must remain byte-for-byte unchanged.
Place any additional regression tests in a new test file.
