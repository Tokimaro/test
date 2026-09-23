"""Корреляции доходностей между инструментами — для лимита коррелированных позиций."""

import numpy as np
import pandas as pd


def correlation_matrix(closes: dict[str, pd.Series], window: int = 200) -> pd.DataFrame:
    """Корреляция лог-доходностей за последние window баров (ряды выравниваются по времени)."""
    frame = pd.DataFrame(closes).sort_index().tail(window + 1).astype("float64")
    rets = frame.apply(np.log).diff()
    return rets.corr(min_periods=max(20, window // 4))


def correlations_for(matrix: pd.DataFrame, symbol: str) -> dict[str, float]:
    if symbol not in matrix.columns:
        return {}
    row = matrix[symbol].drop(labels=[symbol]).dropna()
    return {str(k): float(v) for k, v in row.items()}
