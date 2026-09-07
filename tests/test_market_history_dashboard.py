from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import market_history
import market_history_dashboard as dashboard


def _timestamp(*, days_ago: int = 0) -> str:
    value = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _insert_sale(
    connection: sqlite3.Connection,
    *,
    sale_id: str,
    name: str,
    amount_minor: int,
    marketplace: str = "youpin",
    provider: str = "cspriceapi",
    currency: str = "CNY",
    currency_verified: int = 1,
    usd_micros: int | None = None,
    fx_rate_id: int | None = 1,
    days_ago: int = 0,
) -> None:
    occurred_at = _timestamp(days_ago=days_ago)
    verified_fx = fx_rate_id if currency_verified else None
    connection.execute(
        """
        INSERT INTO market_sale_events(
            provider_code, marketplace_code, provider_sale_id, event_kind,
            granularity, market_hash_name, native_price_minor, currency,
            currency_verified, minor_scale, price_usd_micros, price_usd_cents,
            fx_rate_id, fx_rate_to_usd, fx_rate_date, fx_rate_source,
            source_event_at, source_event_at_raw, timestamp_assumption,
            provider_observed_at, provider_observed_at_raw, imported_at,
            paint_index, paint_seed, float_value, is_stattrak, is_souvenir,
            payload_sha256, archive_sha256, source_file, source_row_number,
            ingest_run_id
        ) VALUES (
            :provider, :marketplace, :sale_id, 'sale', 'event', :name,
            :amount_minor, :currency, :currency_verified, 2, :usd_micros,
            :usd_cents, :fx_rate_id, :fx_rate, :fx_date, :fx_source,
            :occurred_at, :occurred_raw, :timestamp_assumption,
            :observed_at, :observed_raw, :imported_at, 282, 17, '0.151234',
            0, 0, :payload_sha, :archive_sha, 'sales.csv', 2, 'sales-run'
        )
        """,
        {
            "provider": provider,
            "marketplace": marketplace,
            "sale_id": sale_id,
            "name": name,
            "amount_minor": amount_minor,
            "currency": currency,
            "currency_verified": currency_verified,
            "usd_micros": usd_micros if currency_verified else None,
            "usd_cents": (
                (usd_micros + 5_000) // 10_000
                if currency_verified and usd_micros is not None
                else None
            ),
            "fx_rate_id": verified_fx,
            "fx_rate": "0.14" if verified_fx else None,
            "fx_date": occurred_at[:10] if verified_fx else None,
            "fx_source": "ecb_reference" if verified_fx else None,
            "occurred_at": occurred_at,
            "occurred_raw": occurred_at,
            "timestamp_assumption": (
                "UTC_verified:test_v1" if currency_verified else "UTC_unverified"
            ),
            "observed_at": occurred_at,
            "observed_raw": occurred_at,
            "imported_at": occurred_at,
            "payload_sha": f"payload-{sale_id}",
            "archive_sha": "archive-secret-hash",
        },
    )


def _insert_snapshot(connection: sqlite3.Connection) -> None:
    occurred_at = _timestamp()
    connection.execute(
        """
        INSERT INTO price_snapshots(
            provider_code, marketplace_code, provider_item_id, event_kind,
            granularity, market_hash_name, native_price_minor, currency,
            currency_verified, minor_scale, price_usd_micros, price_usd_cents,
            fx_rate_id, fx_rate_to_usd, fx_rate_date, fx_rate_source,
            listing_count, liquidity, source_event_at, provider_observed_at,
            imported_at, provider_source_name, payload_sha256, response_sha256,
            source_uri, source_row_number, ingest_run_id
        ) VALUES (
            'cspriceapi', 'buff163', 'snapshot-item', 'listing_snapshot',
            'point', 'AK-47 | Cheap (Field-Tested)', 665, 'CNY', 1, 2,
            950000, 95, 1, '0.14', :rate_date, 'ecb_reference', 12, '8.5',
            :occurred_at, :occurred_at, :occurred_at, 'Buff163',
            'payload-snapshot-secret', 'response-secret',
            'https://provider.invalid/private?token=secret', 1, 'snapshot-run'
        )
        """,
        {"occurred_at": occurred_at, "rate_date": occurred_at[:10]},
    )


@pytest.fixture
def history_database(tmp_path: Path) -> Path:
    path = tmp_path / "market-history.db"
    connection = market_history.connect_database(path)
    now = _timestamp()
    try:
        connection.executemany(
            """
            INSERT INTO ingest_runs(
                run_id, provider_code, ingest_kind, marketplace_code, status,
                payload_sha256, source_uri, started_at, completed_at
            ) VALUES (?, 'cspriceapi', ?, NULL, 'complete', ?, ?, ?, ?)
            """,
            (
                (
                    "sales-run",
                    "sales_export",
                    "run-sales-secret",
                    "https://provider.invalid/sales?token=secret",
                    now,
                    now,
                ),
                (
                    "snapshot-run",
                    "listing_snapshot",
                    "run-snapshot-secret",
                    "https://provider.invalid/snapshot?token=secret",
                    now,
                    now,
                ),
            ),
        )
        connection.execute(
            """
            INSERT INTO fx_rates(
                fx_rate_id, provider_code, source_code, base_currency,
                quote_currency, rate_date, rate_to_usd, source_uri,
                provider_observed_at, imported_at, payload_sha256
            ) VALUES (
                1, 'ecb', 'ecb_reference', 'CNY', 'USD', ?, '0.14',
                'https://ecb.invalid/private-source', ?, ?, 'fx-secret-hash'
            )
            """,
            (now[:10], now, now),
        )
        _insert_sale(
            connection,
            sale_id="cheap",
            name="AK-47 | Cheap (Field-Tested)",
            amount_minor=700,
            usd_micros=1_000_000,
        )
        _insert_sale(
            connection,
            sale_id="expensive",
            name="AK-47 | Expensive (Factory New)",
            amount_minor=700_000,
            usd_micros=1_000_000_000,
            provider="verified_feed",
        )
        _insert_sale(
            connection,
            sale_id="ambiguous",
            name="M4A1-S | Mystery (Minimal Wear)",
            amount_minor=1_234,
            currency="UNK",
            currency_verified=0,
            fx_rate_id=None,
        )
        _insert_snapshot(connection)
        connection.commit()
    finally:
        connection.close()
    return path


def _all_keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from _all_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _all_keys(child)


def test_missing_or_legacy_database_fails_closed_without_creation(tmp_path: Path):
    missing = tmp_path / "missing.db"
    payload = dashboard.build_market_history_payload(missing)
    assert payload["status"] == "unavailable"
    assert payload["reason"] == "database_missing"
    assert not missing.exists()

    legacy = tmp_path / "legacy.db"
    with sqlite3.connect(legacy) as connection:
        connection.execute("CREATE TABLE sales(id INTEGER)")
    status = dashboard.build_market_history_status(legacy)
    assert status["status"] == "unavailable"
    assert status["reason"] == "unsafe_legacy_database"


def test_status_keeps_fact_kinds_and_registry_separate(history_database: Path):
    status = dashboard.build_market_history_status(history_database)

    assert status["status"] == "ready"
    assert status["counts"] == {"confirmed_sale": 3, "listing_snapshot": 1}
    assert {source["kind"] for source in status["sources"]} == {
        "confirmed_sale",
        "listing_snapshot",
    }
    legacy = next(
        item
        for item in status["datasets"]
        if item["dataset_code"] == "legacy_csfloat_sales"
    )
    assert legacy["registry_only"] is True
    assert legacy["available_in_current_skin_detail"] is True
    assert "storage_ref" not in legacy


def test_payload_never_mixes_sales_and_listing_snapshots(history_database: Path):
    sales = dashboard.build_market_history_payload(history_database, days=3650)
    snapshots = dashboard.build_market_history_payload(
        history_database, kind="listing_snapshot", days=3650
    )

    assert sales["status"] == snapshots["status"] == "ready"
    assert sales["truth"]["mixed_event_kinds"] is False
    assert {row["kind"] for row in sales["rows"]} == {"confirmed_sale"}
    assert {row["kind"] for row in snapshots["rows"]} == {"listing_snapshot"}
    assert sales["pagination"]["total"] == 3
    assert snapshots["pagination"]["total"] == 1
    assert snapshots["rows"][0]["marketplace_code"] == "buff163"
    assert snapshots["rows"][0]["native_price"]["currency"] == "CNY"
    assert snapshots["rows"][0]["normalized_usd"]["amount_micros"] == 950_000


def test_unverified_currency_exposes_native_units_but_no_usd_or_fx(
    history_database: Path,
):
    payload = dashboard.build_market_history_payload(
        history_database, search="M4A1-S | Mystery", days=3650
    )
    row = payload["rows"][0]

    assert row["native_price"] == {
        "amount_minor": 1_234,
        "currency": None,
        "currency_verified": False,
        "exponent": 2,
        "unit_label": "provider minor units",
    }
    assert row["normalized_usd"] is None
    assert row["fx"] is None
    assert row["timestamp_assumption"] == "UTC_unverified"


def test_different_skin_prices_are_never_averaged_on_one_chart(
    history_database: Path,
):
    mixed = dashboard.build_market_history_payload(
        history_database, search="AK-47", days=3650, limit=1
    )
    assert mixed["pagination"]["total"] == 2
    assert mixed["truth"]["series_scope"] == "multiple_names"
    assert mixed["series"] == []
    assert mixed["summary"]["source_count"] == 2
    assert mixed["summary"]["usd_coverage"] == 1.0

    exact = dashboard.build_market_history_payload(
        history_database,
        search="AK-47 | Cheap (Field-Tested)",
        days=3650,
    )
    assert exact["truth"]["series_scope"] == "exact_name"
    assert exact["truth"]["series_market_hash_name"] == (
        "AK-47 | Cheap (Field-Tested)"
    )
    assert len(exact["series"]) == 1
    assert exact["series"][0]["points"][0]["average_native_minor"] == 700


def test_public_contract_does_not_leak_raw_payload_or_storage_provenance(
    history_database: Path,
):
    payload = dashboard.build_market_history_payload(history_database, days=3650)
    forbidden = {
        "storage_ref",
        "payload_sha256",
        "archive_sha256",
        "source_uri",
        "source_file",
        "source_row_number",
        "source_event_at_raw",
        "provider_observed_at_raw",
        "ingest_run_id",
    }
    assert forbidden.isdisjoint(set(_all_keys(payload)))
    serialized = json.dumps(payload)
    assert "token=secret" not in serialized
    assert str(history_database.parent) not in serialized


def test_filters_are_bounded(history_database: Path):
    with pytest.raises(ValueError, match="kind"):
        dashboard.build_market_history_payload(history_database, kind="all")
    with pytest.raises(ValueError, match="days"):
        dashboard.build_market_history_payload(history_database, days=0)
    with pytest.raises(ValueError, match="marketplace"):
        dashboard.build_market_history_payload(
            history_database, marketplace="buff163' OR 1=1 --"
        )


def test_ru_en_page_and_inline_javascript_are_safe(tmp_path: Path):
    russian = dashboard.render_market_history_page("ru")
    english = dashboard.render_market_history_page("en")

    assert '<html lang="ru">' in russian
    assert '<html lang="en">' in english
    assert 'href="/" data-i18n="overview"' in russian
    assert "/api/market-history" in english
    assert "Confirmed sales" in english
    assert "listing snapshot" in english.lower()
    assert "__LOCALE__" not in russian + english
    for unsafe_sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert unsafe_sink not in english

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is unavailable")
    scripts = re.findall(r"<script>(.*?)</script>", english, flags=re.DOTALL)
    assert len(scripts) == 1
    inline = tmp_path / "market-history-inline.js"
    inline.write_text(scripts[0], encoding="utf-8")
    checked = subprocess.run(
        [node, "--check", str(inline)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert checked.returncode == 0, checked.stderr

    assertions = tmp_path / "market-history-contract.cjs"
    assertions.write_text(
        "const assert=require('node:assert/strict');\n"
        f"const page={json.dumps(english)};\n"
        "assert(page.includes(\"group.currency_verified,group.native_unit_label\"));\n"
        "assert(page.includes(\"item.native_price?.currency_verified,item.native_price?.unit_label\"));\n"
        "assert(page.includes(\"group.currency_verified?group.native_currency:tr('currencyUnverified')\"));\n"
        "assert(!page.includes('innerHTML'));\n",
        encoding="utf-8",
    )
    asserted = subprocess.run(
        [node, str(assertions)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert asserted.returncode == 0, asserted.stderr
