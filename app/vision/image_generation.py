"""Lightweight chart / diagram generation using matplotlib.

Produces PNG files suitable for embedding in PDF reports or displaying
via the API.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime

logger = logging.getLogger(__name__)


def generate_chart(
    chart_type: str,
    data: list[dict],
    output_path: str,
    *,
    title: str = "",
    xlabel: str = "",
    ylabel: str = "",
) -> str:
    """Generate a chart and save it as PNG.

    Parameters
    ----------
    chart_type : str
        One of ``"bar"``, ``"line"``, ``"pie"``.
    data : list[dict]
        Each dict must have ``"label"`` and ``"value"`` keys.
    output_path : str
        Destination .png path.

    Returns
    -------
    str
        Path to the generated image.
    """
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as exc:  # noqa: BLE001
        logger.warning("matplotlib unavailable: %s", exc)
        return ""

    labels = [d.get("label", "") for d in data]
    values = [d.get("value", 0) for d in data]

    fig, ax = plt.subplots(figsize=(8, 5))

    if chart_type == "bar":
        ax.bar(labels, values, color="#4a90d9")
    elif chart_type == "line":
        ax.plot(labels, values, marker="o", color="#4a90d9")
    elif chart_type == "pie":
        ax.pie(values, labels=labels, autopct="%1.1f%%", startangle=140)
    else:
        logger.warning("unsupported chart_type=%s", chart_type)
        plt.close(fig)
        return ""

    if title:
        ax.set_title(title)
    if xlabel and chart_type != "pie":
        ax.set_xlabel(xlabel)
    if ylabel and chart_type != "pie":
        ax.set_ylabel(ylabel)

    plt.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)
    return output_path
