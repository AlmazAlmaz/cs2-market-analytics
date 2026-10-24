import importlib
import subprocess
import sys
from pathlib import Path

import pytest
import torch

import train_neural_model
from IGORMODEL import train as igor_train
from windows_training_config import update_training_config


def test_nova_auto_prefers_cuda(monkeypatch):
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    assert train_neural_model.choose_device("auto").type == "cuda"


def test_nova_explicit_cuda_fails_closed(monkeypatch):
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    with pytest.raises(RuntimeError, match="CUDA was requested"):
        train_neural_model.choose_device("cuda")


def test_nova_require_gpu_does_not_fall_back_to_cpu(monkeypatch):
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    monkeypatch.setattr(torch.backends.mps, "is_available", lambda: False)
    with pytest.raises(RuntimeError, match="GPU training is required"):
        train_neural_model.choose_device("auto", require_gpu=True)


def test_igor_auto_prefers_catboost_gpu(monkeypatch):
    monkeypatch.setattr(igor_train, "get_gpu_device_count", lambda: 1)
    assert igor_train.choose_training_device("auto") == "GPU"


def test_igor_explicit_cuda_fails_closed(monkeypatch):
    monkeypatch.setattr(igor_train, "get_gpu_device_count", lambda: 0)
    with pytest.raises(RuntimeError, match="found no compatible NVIDIA GPU"):
        igor_train.choose_training_device("cuda")


def test_igor_gpu_parameters_cover_every_fit_configuration():
    parameters = igor_train.model_params(
        10,
        "Quantile:alpha=0.15",
        2,
        task_type="GPU",
        gpu_devices="0",
    )
    assert parameters["task_type"] == "GPU"
    assert parameters["devices"] == "0"


def test_windows_training_config_changes_only_accelerator_keys(tmp_path):
    config = tmp_path / "config.local.env"
    secret_lines = [
        "API_TOKEN=a-secret=value # keep exactly",
        "TELEGRAM_BOT_TOKEN=another-secret",
    ]
    config.write_text(
        "# local settings\r\n"
        f"{secret_lines[0]}\r\n"
        "CS2_TRAINING_DEVICE=auto\r\n"
        f"{secret_lines[1]}\r\n"
        "export CS2_TRAINING_REQUIRE_GPU=false\r\n"
        "CS2_CUDA_DEVICES=3\r\n"
        "CS2_TRAINING_DEVICE=cpu\r\n",
        encoding="utf-8",
        newline="",
    )

    update_training_config(config, device="cuda", cuda_devices="0")
    rendered = config.read_text(encoding="utf-8")
    for line in secret_lines:
        assert line in rendered.splitlines()
    assert rendered.count("CS2_TRAINING_DEVICE=") == 1
    assert "CS2_TRAINING_DEVICE=cuda" in rendered
    assert "CS2_TRAINING_REQUIRE_GPU=true" in rendered
    assert "CS2_CUDA_DEVICES=0" in rendered

    update_training_config(config, device="cpu", cuda_devices="0")
    rendered = config.read_text(encoding="utf-8")
    assert "CS2_TRAINING_DEVICE=cpu" in rendered
    assert "CS2_TRAINING_REQUIRE_GPU=false" in rendered
    for line in secret_lines:
        assert line in rendered.splitlines()


def _import_fast_iterate(monkeypatch):
    igor_dir = Path(__file__).resolve().parents[1] / "IGORMODEL"
    monkeypatch.syspath_prepend(str(igor_dir))
    sys.modules.pop("fast_iterate", None)
    return importlib.import_module("fast_iterate")


def test_fast_iterate_uses_shared_strict_gpu_config(monkeypatch):
    fast_iterate = _import_fast_iterate(monkeypatch)
    monkeypatch.setattr(
        fast_iterate.app_config,
        "get",
        lambda name, default="": {
            "CS2_TRAINING_DEVICE": "cuda",
            "CS2_CUDA_DEVICES": "0",
        }.get(name, default),
    )
    monkeypatch.setattr(fast_iterate.app_config, "get_bool", lambda *_args: True)
    monkeypatch.setattr(
        fast_iterate.T, "choose_training_device", lambda requested: "GPU"
    )

    assert fast_iterate.resolve_training_accelerator() == ("cuda", "0", "GPU")


def test_fast_iterate_strict_auto_cannot_fall_back_to_cpu(monkeypatch):
    fast_iterate = _import_fast_iterate(monkeypatch)
    monkeypatch.setattr(
        fast_iterate.app_config,
        "get",
        lambda name, default="": {
            "CS2_TRAINING_DEVICE": "auto",
            "CS2_CUDA_DEVICES": "0",
        }.get(name, default),
    )
    monkeypatch.setattr(fast_iterate.app_config, "get_bool", lambda *_args: True)
    monkeypatch.setattr(
        fast_iterate.T, "choose_training_device", lambda requested: "CPU"
    )

    with pytest.raises(RuntimeError, match="GPU training is required"):
        fast_iterate.resolve_training_accelerator()


def test_fast_iterate_help_starts_without_loading_training_data():
    project = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [
            sys.executable,
            str(project / "IGORMODEL" / "fast_iterate.py"),
            "--help",
        ],
        cwd=project,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert "--device" in completed.stdout
    assert "--gpu-devices" in completed.stdout


def test_gpu_smoke_covers_the_real_vega_fp16_training_path():
    project = Path(__file__).resolve().parents[1]
    source = (project / "gpu_smoke_test.py").read_text(encoding="utf-8")

    assert "VegaPriceNet" in source
    assert 'torch.autocast(device_type="cuda", dtype=torch.float16)' in source
    assert "scaler.scale(vega_loss).backward()" in source
    assert '"ordered_quantiles": ordered_quantiles' in source
