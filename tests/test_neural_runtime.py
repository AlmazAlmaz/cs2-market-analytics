from pathlib import Path

import pytest

import neural_model_runtime


NOVA_ARTIFACT_DIR = Path(__file__).resolve().parents[1] / "NEURALMODEL" / "artifacts"


@pytest.mark.skipif(
    not all(
        (NOVA_ARTIFACT_DIR / filename).is_file()
        for filename in ("nova_price_model.pt", "nova_metadata.json")
    ),
    reason="Nova model artifacts are intentionally excluded from the source repository",
)
def test_nova_prediction_smoke_without_database():
    payload = {
        "name": "AK-47 | Redline (Field-Tested)",
        "buy_price_cents": 2500,
        "reference_base_price": 2600,
        "reference_predicted_price": 2750,
        "reference_float_factor": 1.05,
        "float_value": 0.25,
        "paint_seed": 42,
        "paint_index": 282,
        "fee_percent": 4,
        "stickers": [],
    }
    result = neural_model_runtime.predict_deal(payload)
    assert result["model_name"] == "nova"
    assert 0 < result["low_price_cents"] <= result["predicted_price_cents"] <= result["high_price_cents"]
    assert result["historical_sales_count"] == 0
