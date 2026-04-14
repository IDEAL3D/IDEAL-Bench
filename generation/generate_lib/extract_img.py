#!/usr/bin/env python3
"""
Compute image-level statistics from ``color.png`` for a scene.

All metrics are derived purely from the rendered image (no bpy / blend required).

Metrics
-------
mean_saturation : float
    Average HSV saturation across all pixels. Low → scene is mostly grey/white.
hue_std : float
    Circular standard deviation of hue, computed over ALL pixels regardless of
    saturation. High → diverse hues; low → colours concentrated in one hue.
    For scenes with very low saturation (near-grey), hue_std will naturally be
    high due to noise in the hue channel — this is the correct continuous signal
    (low saturation + high hue_std = achromatic scene).
mean_value : float
    Average HSV value (brightness) across all pixels.
    High → overexposed / washed-out scene; low → underexposed.
mean_gradient : float
    Mean Sobel gradient magnitude on the greyscale image, normalised to [0, 1].
    Low → object boundaries are weak / indistinct.
entropy : float
    Shannon entropy (bits) of the greyscale pixel histogram.
    Low → pixel values concentrated in a narrow range (flat or blown-out image).
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_hsv_array(img_rgb: Image.Image) -> np.ndarray:
    """Return float32 HSV array shaped (H, W, 3) with values in [0, 1]."""
    hsv = img_rgb.convert("HSV")
    arr = np.array(hsv, dtype=np.float32) / 255.0  # PIL HSV: uint8
    return arr  # [..., 0]=H, [..., 1]=S, [..., 2]=V


def _load_grey_array(img_rgb: Image.Image) -> np.ndarray:
    """Return float32 greyscale array shaped (H, W) with values in [0, 1]."""
    grey = img_rgb.convert("L")
    return np.array(grey, dtype=np.float32) / 255.0


def _sobel_magnitude(grey: np.ndarray) -> np.ndarray:
    """Compute per-pixel Sobel gradient magnitude (same shape as input)."""
    p = np.pad(grey, 1, mode="edge")
    gx = (
        -p[:-2, :-2] + p[:-2, 2:]
        - 2 * p[1:-1, :-2] + 2 * p[1:-1, 2:]
        - p[2:, :-2] + p[2:, 2:]
    ) / 8.0
    gy = (
        -p[:-2, :-2] - 2 * p[:-2, 1:-1] - p[:-2, 2:]
        + p[2:, :-2] + 2 * p[2:, 1:-1] + p[2:, 2:]
    ) / 8.0
    return np.sqrt(gx ** 2 + gy ** 2)


def _circular_std_hue(hue: np.ndarray) -> float:
    """Circular standard deviation of hue values in [0, 1], computed over all pixels."""
    angles = hue.ravel() * 2.0 * math.pi
    c_bar = float(np.mean(np.cos(angles)))
    s_bar = float(np.mean(np.sin(angles)))
    r = math.sqrt(c_bar ** 2 + s_bar ** 2)
    r = min(r, 1.0)  # numerical safety
    return float(math.sqrt(-2.0 * math.log(r))) if r > 0 else float("inf")


def _entropy_grey(grey: np.ndarray) -> float:
    """Shannon entropy (bits) of greyscale pixel histogram."""
    counts = np.bincount((grey * 255).astype(np.uint8).ravel(), minlength=256)
    probs = counts / counts.sum()
    nonzero = probs[probs > 0]
    return float(-np.sum(nonzero * np.log2(nonzero)))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_image_stats(color_png: Path) -> dict[str, Any]:
    """Return image statistics dict for ``color_png``.

    Parameters
    ----------
    color_png:
        Path to the rendered ``color.png`` for a scene.

    Returns
    -------
    dict with keys: mean_saturation, hue_std, mean_value, mean_gradient, entropy.
    All floats are rounded to 4 decimal places. hue_std is always a float.
    """
    img = Image.open(color_png).convert("RGB")

    hsv = _load_hsv_array(img)
    grey = _load_grey_array(img)

    H = hsv[..., 0]
    S = hsv[..., 1]
    V = hsv[..., 2]

    mean_saturation = round(float(np.mean(S)), 4)
    mean_value = round(float(np.mean(V)), 4)
    hue_std = round(_circular_std_hue(H), 4)

    grad = _sobel_magnitude(grey)
    mean_gradient = round(float(np.mean(grad)), 4)

    entropy = round(_entropy_grey(grey), 4)

    return {
        "mean_saturation": mean_saturation,
        "hue_std": hue_std,
        "mean_value": mean_value,
        "mean_gradient": mean_gradient,
        "entropy": entropy,
    }


# ---------------------------------------------------------------------------
# CLI (for manual inspection)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) < 2:
        print("Usage: python extract_img.py <color.png>", file=sys.stderr)
        sys.exit(1)

    stats = compute_image_stats(Path(sys.argv[1]))
    print(json.dumps(stats, indent=2))