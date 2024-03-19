from __future__ import annotations
import time
from contextlib import contextmanager
import polars as pl
import numpy as np
from polars_ols import pl_least_squares_from_formula, pl_least_squares
import statsmodels.formula.api as smf
from sklearn.linear_model import ElasticNet

@contextmanager
def timer(msg: str | None = None, precision: int = 3) -> float:
    start = time.perf_counter()
    end = start
    yield lambda: end - start
    msg = f"{msg or 'Took'}: {time.perf_counter() - start:.{precision}f} seconds"
    print(msg)
    end = time.perf_counter()


def _make_data(n: int = 10_000):
    array = np.random.normal(size=(n, 2))
    return pl.DataFrame(
        {
            "y": array.sum(axis=1) + np.random.normal(size=n, scale=0.1),
            "x1": array[:, 0],
            "x2": array[:, 1],
        }
    ).cast(pl.Float32)


def test_ols():
    df = _make_data()
    # compute OLS solution w/ polars (via QR in rust) [2.331s]
    with timer("OLS rust"):
        for _ in range(1_000):
            expr = pl_least_squares(pl.col("y"), pl.col("x1"), pl.col("x2")).alias("predictions")
            df = df.lazy().with_columns(expr).collect()

    # compute OLS w/ lstsq numpy [4.583s]
    with timer("OLS numpy"):
        for _ in range(1_000):
            x, y = df.select("x1", "x2").to_numpy(), df.select("y").to_numpy().flatten()
            coef = np.linalg.lstsq(x, y, rcond=None)[0]
            df = df.with_columns(predictions2=pl.lit(x @ coef).flatten())

    assert np.allclose(df["predictions"], df["predictions2"], atol=1.0e-4, rtol=1.0e-4)


def test_ols_coefficients():
    df = _make_data()
    coef = df.select(
        pl.col("y").least_squares.from_formula("x1 + x2 -1", mode="coefficients")
    ).to_numpy()
    assert np.allclose(coef, [1.0, 1.0], atol=1.0e-2, rtol=1.0e-2)


def test_ols_residuals():
    df = _make_data()
    residuals = df.select(
        pl.col("y").least_squares.from_formula("x1 + x2 -1", mode="residuals")
    ).to_numpy()
    x, y = df.select("x1", "x2").to_numpy(), df["y"].to_numpy()
    coef = np.linalg.lstsq(x, y, rcond=None)[0]
    assert np.allclose(residuals.flatten(), y - x @ coef, rtol=1.0e-4, atol=1.0e-4)


def test_ols_intercept():
    df = _make_data()
    expr = pl_least_squares(pl.col("y"), pl.col("x1"), pl.col("x2"), add_intercept=True).alias(
        "predictions"
    )
    y_hat = df.select(expr).to_numpy().flatten()
    expected = smf.ols(formula="y ~ x1 + x2", data=df).fit().predict(df).to_numpy()
    assert np.allclose(y_hat, expected, atol=1.0e-4, rtol=1.0e-4)


def test_least_squares_from_formula():
    weights = np.random.uniform(0, 1, size=10_000)
    weights /= weights.mean()
    df = _make_data().with_columns(sample_weights=pl.lit(weights)).cast(pl.Float32)

    expr = pl_least_squares_from_formula(
        "y ~ x1 + x2",  # patsy includes intercept by default
        sample_weights=pl.col("sample_weights"),
    ).alias("predictions")

    expected = (
        smf.wls("y ~ x1 + x2", data=df, weights=df["sample_weights"].to_numpy())
        .fit()
        .predict(df)
        .to_numpy()
    )
    assert np.allclose(df.select(expr).to_numpy().flatten(), expected, rtol=1.0e-4, atol=1.0e-4)


def test_ridge():
    df = _make_data()
    alpha: float = 0.01

    with timer("ridge python"):
        for _ in range(1_000):
            x = df.select("x1", "x2").to_numpy()
            y = df.select("y").to_numpy().flatten()
            coef_expected = np.linalg.solve((x.T @ x) + np.eye(x.shape[1]) * alpha, x.T @ y)
            expected = x @ coef_expected

    with timer("ridge rust"):
        for _ in range(1_000):
            expr = pl_least_squares(
                pl.col("y"),
                pl.col("x1"),
                pl.col("x2"),
                alpha=alpha,
                ridge_solve_method="solve",
            ).alias("predictions")
            df = df.lazy().with_columns(expr).collect()

    assert np.allclose(df["predictions"].to_numpy(), expected, rtol=1.0e-4, atol=1.0e-4)


def test_wls():
    array = np.random.normal(size=(10_000, 2))
    df = pl.DataFrame(
        {
            "y": array.sum(axis=1)
            + np.hstack(
                [np.random.normal(size=8_000, scale=10.0), np.random.normal(size=2_000, scale=0.1)]
            ),
            "x1": array[:, 0],
            "x2": array[:, 1],
        }
    ).cast(pl.Float32)

    weights = np.hstack([np.ones(8_000) * 1.0 / 10**2, np.ones(2_000) * 1.0 / 0.1**2])
    weights /= weights.mean()

    df = df.with_columns(sample_weight=weights).cast(pl.Float32)

    expr_wls = pl_least_squares(
        pl.col("y"),
        pl.col("x1"),
        pl.col("x2"),
        sample_weights=pl.col("sample_weight"),
    ).alias("predictions_wls")
    expr_ols = pl_least_squares(
        pl.col("y"),
        pl.col("x1"),
        pl.col("x2"),
    ).alias("predictions_ols")
    df = df.lazy().with_columns(expr_wls, expr_ols).collect()

    y_hat_wls = smf.wls(data=df, formula="y ~ x1 + x2 -1", weights=weights).fit().predict()
    y_hat_ols = smf.ols(data=df, formula="y ~ x1 + x2 -1").fit().predict()

    assert np.allclose(df["predictions_ols"].to_numpy(), y_hat_ols, rtol=1.0e-4, atol=1.0e-4)
    assert np.allclose(df["predictions_wls"].to_numpy(), y_hat_wls, rtol=1.0e-4, atol=1.0e-4)


def test_least_squares_namespace():
    df = _make_data().with_columns(sample_weight=pl.lit(1.0))
    alpha: float = 0.0
    ols = pl.col("y").least_squares.ols(pl.col("x1"), pl.col("x2")).alias("ols")
    ridge = pl.col("y").least_squares.ridge(pl.col("x1"), pl.col("x2"), alpha=alpha).alias("ridge")
    wls = (
        pl.col("y")
        .least_squares.wls(pl.col("x1"), pl.col("x2"), sample_weights=pl.col("sample_weight"))
        .alias("wls")
    )
    formula = pl.col("y").least_squares.from_formula("x1 + x2 - 1").alias("formula")
    df = df.lazy().select(ols, ridge, wls, formula).collect()

    # ensure all of the above are equivalent
    assert np.allclose(df.corr(), 1.0)


def test_elastic_net():
    df = _make_data()
    mdl = ElasticNet(fit_intercept=False, alpha=0.1, l1_ratio=0.5, max_iter=1_000, tol=0.0001)
    mdl.fit(df.select(pl.all().exclude("y")), df.select("y"))

    coef = df.lazy().select(
        pl.col("y").least_squares.from_formula("x1 + x2 -1",
                                               mode="coefficients",
                                               l1_ratio=0.5,
                                               alpha=0.1,
                                               max_iter=1_000,
                                               tol=0.0001,
                                               ).alias("predictions")

    ).collect().to_numpy().flatten()
    assert np.allclose(mdl.coef_, coef, rtol=1.e-4, atol=1.e-4)
