import dashboard_server
import scanner_target_catalog


def test_training_workspace_has_six_models_and_a_clear_process():
    template = dashboard_server.HTML

    assert (
        "Object.freeze(['almaz', 'igor', 'nova', 'vega', 'knives', 'gloves'])"
        in template
    )
    assert "weapon skins only" in template
    assert "advisory Vega" in template
    assert "knives only" in template
    assert "gloves only" in template
    for stage in ("data", "features", "train", "quality", "activation"):
        assert f"'{stage}'" in template or f"{stage}:" in template
    assert 'data-role="training-stages"' in template
    assert 'role="progressbar"' in template
    assert 'data-role="training-log"' in template
    assert 'data-training-action="activate"' not in template


def test_training_workspace_renders_detailed_candidate_comparison():
    template = dashboard_server.HTML

    assert 'data-role="quality-comparison"' in template
    assert 'data-role="quality-summary"' in template
    assert 'data-role="quality-table"' in template
    assert "function renderQualityComparison(card, job)" in template
    assert "job?.quality_comparison" in template
    for metric in (
        "median_ape",
        "mean_ape",
        "p90_ape",
        "mae_usd",
        "rmse_log",
        "r2",
        "interval_coverage",
        "quantile_crossing_rate",
        "candidate_row_win_percent",
    ):
        assert metric in template
    assert "quality-better" in template
    assert "quality-worse" in template
    assert "technical integrity check failed" in template
    assert "function trainingCardReason(job, gate)" in template
    assert "production_item_kind" in template
    assert "strict retrain is required" in template


def test_dashboard_freshness_reads_separate_glove_artifacts():
    source = dashboard_server.build_model_freshness.__code__.co_consts
    joined = "\n".join(value for value in source if isinstance(value, str))

    assert "GLOVEMODEL" in joined
    assert "glove_price_model.joblib" in joined
    assert "glove_price_model_metrics.json" in joined


def test_glove_scanner_preview_uses_verified_dedicated_generation(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        dashboard_server,
        "_verified_active_glove_generation",
        lambda: {"dedicated_model_available": True},
    )

    def preview(_path, profile, **kwargs):
        captured.update(profile=profile, **kwargs)
        return {"profile": profile, "scan_ready": kwargs["readiness"]["scan_ready"]}

    monkeypatch.setattr(scanner_target_catalog, "build_scanner_target_preview", preview)

    payload = dashboard_server.build_scanner_target_preview_payload("gloves")

    assert payload == {"profile": "gloves", "scan_ready": True}
    assert captured["readiness"] == {"scan_ready": True, "blocked_reason": None}
