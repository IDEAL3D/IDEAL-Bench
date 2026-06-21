"""Resolve ``CUDA_VISIBLE_DEVICES`` from CLI flags, ``IDEAL_GPU``, or auto-selection."""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Optional


def _pick_least_loaded_gpu(timeout_s: float = 10.0) -> Optional[str]:
    """Return physical GPU index with lowest score, or None if ``nvidia-smi`` is unavailable."""
    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,utilization.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        return None
    best_id: Optional[str] = None
    best_score = float("inf")
    for line in proc.stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4:
            continue
        try:
            idx = parts[0]
            util = float(parts[1])
            mem_used = float(parts[2])
            mem_total = float(parts[3])
        except ValueError:
            continue
        mem_pct = (mem_used / mem_total * 100.0) if mem_total > 0 else 0.0
        score = util + 0.01 * mem_pct
        if score < best_score:
            best_score = score
            best_id = idx
    return best_id


def _normalize_device_list(spec: str) -> Optional[str]:
    """Return comma-separated GPU indices, or None if invalid."""
    chunks = [c.strip() for c in spec.split(",") if c.strip()]
    if not chunks:
        return None
    for c in chunks:
        if not c.isdigit():
            return None
    return ",".join(chunks)


def resolve_cuda_visible_devices(cli_gpu: str | None) -> str:
    """Pick the value to assign to ``CUDA_VISIBLE_DEVICES``.

    Precedence:

    1. Non-empty ``cli_gpu`` (e.g. ``--gpu 2`` or ``--gpu 0,1``).
    2. Non-empty ``IDEAL_GPU`` when ``cli_gpu`` is omitted/blank.
    3. Existing non-empty ``CUDA_VISIBLE_DEVICES`` (shell or parent process), unchanged.
    4. ``auto`` or empty: query ``nvidia-smi`` for the least loaded GPU; on failure use ``\"0\"``.

    ``auto`` may appear in ``cli_gpu`` or ``IDEAL_GPU``.
    """
    if cli_gpu is not None and str(cli_gpu).strip() != "":
        spec = str(cli_gpu).strip()
    else:
        ide = os.environ.get("IDEAL_GPU", "").strip()
        if ide != "":
            spec = ide
        else:
            cur = os.environ.get("CUDA_VISIBLE_DEVICES", "").strip()
            if cur != "":
                return cur
            spec = ""

    if spec.lower() == "auto" or spec == "":
        picked = _pick_least_loaded_gpu()
        return picked if picked is not None else "0"

    normalized = _normalize_device_list(spec)
    if normalized is not None:
        return normalized

    print(
        f"[gpu_select] invalid GPU spec {spec!r}; falling back to auto or device 0",
        file=sys.stderr,
    )
    picked = _pick_least_loaded_gpu()
    return picked if picked is not None else "0"
