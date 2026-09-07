import scanner_target_profiles


def target_control(*, mode="recommended", include=None, exclude=None, enabled=True):
    profiles = {}
    for name in scanner_target_profiles.PROFILE_ORDER:
        profiles[name] = {
            "enabled": enabled,
            "selection_mode": mode,
            "include": list(include or []) if name == "knives" else [],
            "exclude": list(exclude or []) if name == "knives" else [],
        }
    return {"revision": 7, "profiles": profiles}


def candidates(count=5):
    return [
        {
            "target_key": f"knife:Knife {index}",
            "market_hash_name": f"Knife {index}",
            "estimated_sales_per_day": count - index,
        }
        for index in range(count)
    ]


def test_recommended_selection_prioritizes_manual_include_and_honors_cap():
    control = target_control(
        include=["knife:Knife 4"],
        exclude=["knife:Knife 1"],
    )

    selected, meta = scanner_target_profiles.apply_profile_selection(
        "knives",
        candidates(),
        control,
        recommended_limit=3,
    )

    assert [row["target_key"] for row in selected] == [
        "knife:Knife 4",
        "knife:Knife 0",
        "knife:Knife 2",
    ]
    assert meta["selection_truncated"] is True


def test_custom_selection_is_only_resolved_manual_targets():
    control = target_control(
        mode="custom",
        include=["knife:Missing", "knife:Knife 3"],
    )

    selected, meta = scanner_target_profiles.apply_profile_selection(
        "knives",
        candidates(),
        control,
        recommended_limit=None,
    )

    assert [row["target_key"] for row in selected] == ["knife:Knife 3"]
    assert meta["unresolved_include"] == ["knife:Missing"]


def test_preview_is_searchable_paginated_and_reports_network_budget():
    control = target_control(include=["knife:Knife 4"])

    payload = scanner_target_profiles.build_profile_preview(
        "knives",
        candidates(),
        control,
        recommended_limit=None,
        budget={
            "targets_per_cycle": 2,
            "listings_per_target": 10,
            "pages_per_target": 2,
        },
        search="Knife",
        limit=2,
        offset=1,
        scan_ready=False,
        blocked_reason="dedicated_knife_model_unavailable",
    )

    assert payload["control_revision"] == 7
    assert payload["total_matching"] == 5
    assert payload["next_offset"] == 3
    assert len(payload["items"]) == 2
    assert payload["budget"]["max_requests_per_cycle"] == 4
    assert payload["scan_ready"] is False
    assert payload["blocked_reason"] == "dedicated_knife_model_unavailable"


def test_disabled_profile_has_no_selected_targets_and_explicit_reason():
    control = target_control(enabled=False)

    payload = scanner_target_profiles.build_profile_preview(
        "knives",
        candidates(),
        control,
        recommended_limit=None,
        budget={
            "targets_per_cycle": 1,
            "listings_per_target": 10,
            "pages_per_target": 1,
        },
        scan_ready=True,
    )

    assert payload["selection"]["selected_count"] == 0
    assert payload["scan_ready"] is False
    assert payload["blocked_reason"] == "profile_disabled"
