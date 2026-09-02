"""通用工具函数（无副作用、无业务状态）。"""

from __future__ import annotations

import numpy as np


def round2(x: float | None) -> float | None:
    """空安全 / NaN 安全的保留两位小数。"""
    return None if x is None or not np.isfinite(x) else round(float(x), 2)
