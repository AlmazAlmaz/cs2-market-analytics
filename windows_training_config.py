"""Safely update only the local training-accelerator settings."""

from __future__ import annotations

import argparse
import os
import re
import stat
import tempfile
from collections.abc import Mapping
from pathlib import Path

TRAINING_KEYS = (
    "CS2_TRAINING_DEVICE",
    "CS2_TRAINING_REQUIRE_GPU",
    "CS2_CUDA_DEVICES",
)
_ASSIGNMENT = re.compile(
    r"^\s*(?:export\s+)?(?P<key>[A-Za-z][A-Za-z0-9_]*)\s*="
)


def training_settings(device: str, cuda_devices: str = "0") -> dict[str, str]:
    selected = str(device).strip().lower()
    if selected not in {"cpu", "cuda"}:
        raise ValueError("device must be either 'cpu' or 'cuda'")
    devices = str(cuda_devices).strip() or "0"
    return {
        "CS2_TRAINING_DEVICE": selected,
        "CS2_TRAINING_REQUIRE_GPU": "true" if selected == "cuda" else "false",
        "CS2_CUDA_DEVICES": devices,
    }


def render_config(original: str, settings: Mapping[str, str]) -> str:
    """Replace accelerator keys while preserving every unrelated line verbatim."""

    newline = "\r\n" if "\r\n" in original else "\n"
    output: list[str] = []
    seen: set[str] = set()
    for line in original.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        ending = line[len(body) :]
        match = _ASSIGNMENT.match(body.lstrip("\ufeff"))
        key = match.group("key") if match else None
        if key not in settings:
            output.append(line)
            continue
        if key not in seen:
            output.append(f"{key}={settings[key]}{ending}")
            seen.add(key)

    missing = [key for key in TRAINING_KEYS if key not in seen]
    if missing:
        if output and not output[-1].endswith(("\n", "\r")):
            output[-1] += newline
        if output and output[-1].strip():
            output.append(newline)
        output.extend(f"{key}={settings[key]}{newline}" for key in missing)
    return "".join(output)


def update_training_config(
    path: str | Path,
    *,
    device: str,
    cuda_devices: str = "0",
) -> Path:
    """Atomically update accelerator keys without reading or printing secrets."""

    config_path = Path(path).expanduser().resolve()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    had_bom = False
    original_mode: int | None = None
    if config_path.exists():
        raw = config_path.read_bytes()
        had_bom = raw.startswith(b"\xef\xbb\xbf")
        original = raw.decode("utf-8-sig")
        original_mode = stat.S_IMODE(config_path.stat().st_mode)
    else:
        original = ""

    rendered = render_config(original, training_settings(device, cuda_devices))
    encoding = "utf-8-sig" if had_bom else "utf-8"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{config_path.name}.", suffix=".tmp", dir=config_path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding=encoding, newline="") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        if original_mode is not None:
            os.chmod(temporary, original_mode)
        os.replace(temporary, config_path)
    finally:
        temporary.unlink(missing_ok=True)
    return config_path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--device", choices=("cuda", "cpu"), required=True)
    parser.add_argument("--cuda-devices", default="0")
    args = parser.parse_args(argv)
    update_training_config(
        args.config,
        device=args.device,
        cuda_devices=args.cuda_devices,
    )
    require_gpu = "true" if args.device == "cuda" else "false"
    print(
        "Training accelerator configured: "
        f"device={args.device}, require_gpu={require_gpu}, "
        f"cuda_devices={args.cuda_devices}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
