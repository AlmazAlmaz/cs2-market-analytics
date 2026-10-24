from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import stat
import zipfile
from pathlib import Path
from typing import Any

import pytest

import market_history as history
import market_history_worker as worker

ECB_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<Envelope xmlns="urn:test">
  <Cube><Cube time="2026-07-03">
    <Cube currency="USD" rate="1.20"/>
    <Cube currency="CNY" rate="8.00"/>
  </Cube></Cube>
</Envelope>
"""


def _csv_payload(headers: tuple[str, ...], rows: list[dict[str, str]]) -> str:
    target = io.StringIO(newline="")
    writer = csv.DictWriter(target, fieldnames=headers, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return target.getvalue()


def _sale(
    *,
    marketplace: str = "youpin",
    sale_id: str = "18446744073709551600",
    sold_at: str = "2026-07-05 12:59:12.668",
) -> dict[str, str]:
    return {
        "marketplace": marketplace,
        "sale_id": sale_id,
        "market_hash_name": "AK-47 | Redline (Field-Tested)",
        "price": "10000",
        "sold_at": sold_at,
        "def_index": "7",
        "paint_index": "282",
        "paint_seed": "12",
        "float_value": "0.151234",
        "is_stattrak": "0",
        "is_souvenir": "0",
        "rarity": "4",
        "quality": "4",
        "cs2_img": "https://example.invalid/skin.png",
        "fetched_at": "2026-07-05 13:00:00.000",
    }


def _sticker(sale: dict[str, str]) -> dict[str, str]:
    return {
        "marketplace": sale["marketplace"],
        "sale_id": sale["sale_id"],
        "sold_at": sale["sold_at"],
        "position_idx": "0",
        "sticker_id": "18446744073709550000",
        "slot": "0",
        "sticker_name": "Sticker | Test",
        "wear": "0.1",
        "offset_x": "0",
        "offset_y": "0",
        "rotation": "0",
        "reference_price": "1000",
        "reference_quantity": "20",
        "reference_updated": "2026-07-05 12:00:00.000",
        "fetched_at": sale["fetched_at"],
    }


def _keychain(sale: dict[str, str]) -> dict[str, str]:
    return {
        "marketplace": sale["marketplace"],
        "sale_id": sale["sale_id"],
        "sold_at": sale["sold_at"],
        "position_idx": "1",
        "keychain_id": "18446744073709540000",
        "slot": "0",
        "keychain_name": "Charm | Test",
        "pattern": "7",
        "offset_x": "0",
        "offset_y": "0",
        "offset_z": "0",
        "reference_price": "2000",
        "reference_quantity": "4",
        "reference_updated": "2026-07-05 12:00:00.000",
        "fetched_at": sale["fetched_at"],
    }


def _zip_payload(
    sales: list[dict[str, str]],
    stickers: list[dict[str, str]] | None = None,
    keychains: list[dict[str, str]] | None = None,
    *,
    sales_header: tuple[str, ...] = history.SALES_HEADER,
) -> bytes:
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("sales.csv", _csv_payload(sales_header, sales))
        archive.writestr(
            "stickers.csv",
            _csv_payload(history.STICKERS_HEADER, stickers or []),
        )
        archive.writestr(
            "keychains.csv",
            _csv_payload(history.KEYCHAINS_HEADER, keychains or []),
        )
    return target.getvalue()


def _snapshot(marketplace: str) -> bytes:
    price_field = "price_rmb" if marketplace == "youpin" else "price"
    source = "YouPin" if marketplace == "youpin" else "Buff163"
    return json.dumps(
        {
            "data": [
                {
                    "id": 101,
                    "market_hash_name": "AK-47 | Redline (Field-Tested)",
                    price_field: "100.0000",
                    "count": 12,
                    "liquidity": "8.50",
                    "updated_at": "2026-07-05T12:00:00.000Z",
                }
            ],
            "source": source,
            "timestamp": "2026-07-05T12:10:00.000Z",
        },
        separators=(",", ":"),
    ).encode()


def test_schema_is_isolated_and_registers_legacy_provenance(tmp_path: Path) -> None:
    database = tmp_path / "market.db"
    connection = history.connect_database(database)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "sales" not in tables
        assert {
            "market_sale_events",
            "market_sale_stickers",
            "market_sale_keychains",
            "price_snapshots",
            "fx_rates",
            "ingest_runs",
            "ingest_quarantine",
            "source_datasets",
        } <= tables
        legacy = connection.execute(
            "SELECT * FROM source_datasets WHERE dataset_code='legacy_csfloat_sales'"
        ).fetchone()
        assert legacy["storage_ref"] == "csfloat_history.db:sales"
        assert legacy["marketplace_code"] == "csfloat"
        assert legacy["provider_code"] == "csfloat_direct"
        assert legacy["native_currency"] == "USD"
        assert legacy["price_field"] == "price_cents"
        assert legacy["model_training_eligible"] == 0
        assert (
            connection.execute(
                "SELECT value FROM market_history_meta WHERE key='model_training_eligible'"
            ).fetchone()[0]
            == "false"
        )
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        for sidecar in (
            database,
            Path(f"{database}-wal"),
            Path(f"{database}-shm"),
        ):
            assert sidecar.exists()
            if os.name != "nt":
                assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600
    finally:
        connection.close()
    if os.name != "nt":
        assert stat.S_IMODE(database.stat().st_mode) == 0o600


def test_database_with_legacy_sales_table_is_rejected(tmp_path: Path) -> None:
    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE sales(id INTEGER)")
    with pytest.raises(history.UnsafeDatabaseError):
        history.connect_database(database)
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name='market_sale_events'"
            ).fetchone()[0]
            == 0
        )


def test_default_private_parent_is_tightened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private = tmp_path / ".private"
    private.mkdir(mode=0o755)
    private.chmod(0o755)
    monkeypatch.setenv("CS2_MARKET_HISTORY_DB_PATH", str(private / "history.db"))
    monkeypatch.setenv("CS2_DB_PATH", str(tmp_path / "legacy.db"))
    connection = history.connect_database()
    connection.close()
    if os.name != "nt":
        assert stat.S_IMODE(private.stat().st_mode) == 0o700


def test_ecb_cross_rate_and_weekend_resolution_are_exact(tmp_path: Path) -> None:
    connection = history.connect_database(tmp_path / "history.db")
    try:
        assert history.import_ecb_reference_rates(connection, ECB_XML) == 3
        cny = history.resolve_fx_rate(connection, "CNY", "2026-07-05")
        eur = history.resolve_fx_rate(connection, "EUR", "2026-07-04")
        usd = history.resolve_fx_rate(connection, "USD", "2026-07-03")
        assert cny is not None and cny.rate_to_usd == history.Decimal("0.15")
        assert cny.rate_date == "2026-07-03"
        assert eur is not None and eur.rate_to_usd == history.Decimal("1.20")
        assert usd is not None and usd.rate_to_usd == history.Decimal(1)
    finally:
        connection.close()


def test_ecb_rejects_document_type(tmp_path: Path) -> None:
    connection = history.connect_database(tmp_path / "history.db")
    try:
        payload = (
            b" " * 5000 + b'<!DOCTYPE x [<!ENTITY x "boom">]><Envelope>&x;</Envelope>'
        )
        with pytest.raises(history.ValidationError, match="declarations") as raised:
            history.import_ecb_reference_rates(connection, payload)
        assert raised.value.code == "unsafe_ecb_xml"
        assert connection.execute("SELECT COUNT(*) FROM fx_rates").fetchone()[0] == 0
    finally:
        connection.close()


def test_unverified_sales_keep_raw_money_and_time_without_usd(tmp_path: Path) -> None:
    sale = _sale()
    payload = _zip_payload([sale], [_sticker(sale)], [_keychain(sale)])
    connection = history.connect_database(tmp_path / "history.db")
    try:
        result = history.import_cspriceapi_sales_zip(
            connection, payload, source_uri="manual-private-import"
        )
        assert result.inserted_rows == 3
        event = connection.execute("SELECT * FROM market_sale_events").fetchone()
        assert event["marketplace_code"] == "youpin"
        assert event["provider_code"] == "cspriceapi"
        assert event["native_price_minor"] == 10000
        assert event["minor_scale"] == 2
        assert event["currency"] == "UNK"
        assert event["currency_verified"] == 0
        assert event["price_usd_micros"] is None
        assert event["price_usd_cents"] is None
        assert event["fx_rate_id"] is None
        assert event["source_event_at_raw"] == sale["sold_at"]
        assert event["timestamp_assumption"] == "UTC_unverified"
        assert event["payload_sha256"]
        sticker = connection.execute("SELECT * FROM market_sale_stickers").fetchone()
        assert sticker["currency"] == "UNK"
        assert sticker["currency_verified"] == 0
        assert sticker["reference_price_usd_micros"] is None
        run = connection.execute(
            "SELECT * FROM ingest_runs WHERE run_id=?", (result.run_id,)
        ).fetchone()
        assert run["currency_contract_status"] == "unverified"
        assert run["timestamp_assumption"] == "UTC_unverified"
        history.import_ecb_reference_rates(connection, ECB_XML)
        assert history.backfill_missing_usd_prices(connection)["sales"] == 0
        assert (
            connection.execute(
                "SELECT price_usd_micros FROM market_sale_events"
            ).fetchone()[0]
            is None
        )
    finally:
        connection.close()


def test_confirmed_contract_converts_native_prices_with_fx(tmp_path: Path) -> None:
    sales = [
        _sale(marketplace="youpin", sale_id="7"),
        _sale(marketplace="csfloat", sale_id="7"),
    ]
    connection = history.connect_database(tmp_path / "history.db")
    try:
        history.import_ecb_reference_rates(connection, ECB_XML)
        contract = history.SalesExportContract.confirmed_cspriceapi("written-v1")
        result = history.import_cspriceapi_sales_zip(
            connection, _zip_payload(sales), contract=contract
        )
        assert result.inserted_rows == 2
        rows = {
            row["marketplace_code"]: row
            for row in connection.execute(
                "SELECT * FROM market_sale_events ORDER BY marketplace_code"
            )
        }
        assert rows["youpin"]["currency"] == "CNY"
        assert rows["youpin"]["currency_verified"] == 1
        assert rows["youpin"]["price_usd_micros"] == 15_000_000
        assert rows["youpin"]["price_usd_cents"] == 1500
        assert rows["youpin"]["fx_rate_to_usd"] == "0.15"
        assert rows["youpin"]["fx_rate_date"] == "2026-07-03"
        assert rows["csfloat"]["currency"] == "USD"
        assert rows["csfloat"]["price_usd_cents"] == 10_000
        assert rows["csfloat"]["provider_sale_id"] == "7"
        assert rows["youpin"]["provider_sale_id"] == "7"
        assert rows["youpin"]["timestamp_assumption"] == "UTC_verified:written-v1"
    finally:
        connection.close()


def test_sales_zip_is_idempotent_by_archive_hash(tmp_path: Path) -> None:
    sale = _sale()
    payload = _zip_payload([sale], [_sticker(sale)])
    connection = history.connect_database(tmp_path / "history.db")
    try:
        first = history.import_cspriceapi_sales_zip(connection, payload)
        second = history.import_cspriceapi_sales_zip(connection, payload)
        assert second.run_id == first.run_id
        assert second.reused_run is True
        assert connection.execute("SELECT COUNT(*) FROM ingest_runs").fetchone()[0] == 1
        assert (
            connection.execute("SELECT COUNT(*) FROM market_sale_events").fetchone()[0]
            == 1
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM market_sale_stickers").fetchone()[
                0
            ]
            == 1
        )
    finally:
        connection.close()


def test_orphan_attachment_quarantines_whole_archive(tmp_path: Path) -> None:
    sale = _sale(sale_id="1")
    orphan_source = _sale(sale_id="2")
    payload = _zip_payload([sale], [_sticker(orphan_source)])
    connection = history.connect_database(tmp_path / "history.db")
    try:
        with pytest.raises(history.ValidationError) as raised:
            history.import_cspriceapi_sales_zip(connection, payload)
        assert raised.value.code == "orphan_attachment"
        assert (
            connection.execute("SELECT COUNT(*) FROM market_sale_events").fetchone()[0]
            == 0
        )
        run = connection.execute("SELECT * FROM ingest_runs").fetchone()
        assert run["status"] == "quarantined"
        assert run["error_code"] == "orphan_attachment"
        quarantine = connection.execute("SELECT * FROM ingest_quarantine").fetchone()
        assert quarantine["source_file"] == "stickers.csv"
        assert quarantine["row_payload_sha256"]
    finally:
        connection.close()


def test_header_and_response_count_validation_fail_closed(tmp_path: Path) -> None:
    connection = history.connect_database(tmp_path / "history.db")
    try:
        bad_header = tuple(name for name in history.SALES_HEADER if name != "price")
        with pytest.raises(history.ValidationError) as header_error:
            history.import_cspriceapi_sales_zip(
                connection, _zip_payload([_sale()], sales_header=bad_header)
            )
        assert header_error.value.code == "invalid_csv_header"

        with pytest.raises(history.ValidationError) as count_error:
            history.import_cspriceapi_sales_zip(
                connection,
                _zip_payload([_sale(sale_id="2")]),
                response_headers={"X-Sales-Row-Count": "2"},
            )
        assert count_error.value.code == "row_count_mismatch"
        assert (
            connection.execute("SELECT COUNT(*) FROM market_sale_events").fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM ingest_runs WHERE status='quarantined'"
            ).fetchone()[0]
            == 2
        )
        with pytest.raises(history.ValidationError) as missing_headers:
            history.import_cspriceapi_sales_zip(
                connection,
                _zip_payload([_sale(sale_id="3")]),
                require_response_headers=True,
            )
        assert missing_headers.value.code == "missing_response_header"
    finally:
        connection.close()


def test_zip_member_symlink_is_rejected_and_quarantined(tmp_path: Path) -> None:
    payload_target = io.BytesIO()
    with zipfile.ZipFile(payload_target, "w") as archive:
        symlink = zipfile.ZipInfo("sales.csv")
        symlink.create_system = 3
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(symlink, "target")
        archive.writestr("stickers.csv", _csv_payload(history.STICKERS_HEADER, []))
        archive.writestr("keychains.csv", _csv_payload(history.KEYCHAINS_HEADER, []))
    connection = history.connect_database(tmp_path / "history.db")
    try:
        with pytest.raises(history.ValidationError) as raised:
            history.import_cspriceapi_sales_zip(connection, payload_target.getvalue())
        assert raised.value.code == "unsafe_zip_member"
        assert (
            connection.execute("SELECT status FROM ingest_runs").fetchone()[0]
            == "quarantined"
        )
    finally:
        connection.close()


def test_snapshot_is_verified_cny_point_and_idempotent(tmp_path: Path) -> None:
    connection = history.connect_database(tmp_path / "history.db")
    try:
        history.import_ecb_reference_rates(connection, ECB_XML)
        payload = _snapshot("youpin")
        first = history.import_cspriceapi_snapshot(connection, "youpin", payload)
        second = history.import_cspriceapi_snapshot(connection, "youpin", payload)
        assert second.reused_run is True
        assert second.run_id == first.run_id
        row = connection.execute("SELECT * FROM price_snapshots").fetchone()
        assert row["event_kind"] == "listing_snapshot"
        assert row["granularity"] == "point"
        assert row["currency"] == "CNY"
        assert row["currency_verified"] == 1
        assert row["native_price_minor"] == 1_000_000
        assert row["minor_scale"] == 4
        assert row["price_usd_micros"] == 15_000_000
        assert row["price_usd_cents"] == 1500
        assert row["provider_observed_at"] == "2026-07-05T12:10:00.000Z"
        assert row["payload_sha256"] != row["response_sha256"]
    finally:
        connection.close()


def test_snapshot_identity_collision_quarantines_second_response(
    tmp_path: Path,
) -> None:
    connection = history.connect_database(tmp_path / "history.db")
    try:
        original = _snapshot("buff163")
        history.import_cspriceapi_snapshot(connection, "buff163", original)
        changed = json.loads(original)
        changed["data"][0]["price"] = "101.0000"
        changed_payload = json.dumps(changed).encode()
        with pytest.raises(history.ValidationError) as raised:
            history.import_cspriceapi_snapshot(connection, "buff163", changed_payload)
        assert raised.value.code == "snapshot_identity_collision"
        assert (
            connection.execute(
                "SELECT native_price_minor FROM price_snapshots"
            ).fetchone()[0]
            == 1_000_000
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM ingest_runs WHERE status='quarantined'"
            ).fetchone()[0]
            == 1
        )
    finally:
        connection.close()


@pytest.mark.parametrize("kind", ("sale", "snapshot"))
def test_zero_primary_price_is_quarantined(tmp_path: Path, kind: str) -> None:
    connection = history.connect_database(tmp_path / "history.db")
    try:
        if kind == "sale":
            sale = _sale()
            sale["price"] = "0"
            action = lambda: history.import_cspriceapi_sales_zip(
                connection, _zip_payload([sale])
            )
        else:
            document = json.loads(_snapshot("youpin"))
            document["data"][0]["price_rmb"] = "0.0000"
            action = lambda: history.import_cspriceapi_snapshot(
                connection, "youpin", json.dumps(document).encode()
            )
        with pytest.raises(history.ValidationError) as raised:
            action()
        assert raised.value.code == "invalid_price"
        assert (
            connection.execute("SELECT status FROM ingest_runs").fetchone()[0]
            == "quarantined"
        )
    finally:
        connection.close()


class _FakeResponse:
    def __init__(self, status: int, body: bytes, content_type: str) -> None:
        self.status_code = status
        self.body = body
        self.headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
        }

    def iter_content(self, chunk_size: int) -> list[bytes]:
        return [
            self.body[index : index + chunk_size]
            for index in range(0, len(self.body), chunk_size)
        ]

    def close(self) -> None:
        return None


class _FakeSession:
    def __init__(self, responses: dict[str, _FakeResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, str], Any, bool, bool]] = []

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str],
        timeout: Any,
        stream: bool,
        allow_redirects: bool,
    ) -> _FakeResponse:
        self.calls.append((url, headers, timeout, stream, allow_redirects))
        return self.responses[url]


def _settings(tmp_path: Path, **changes: object) -> worker.WorkerSettings:
    values: dict[str, object] = {
        "enabled": True,
        "license_confirmed": True,
        "database_path": tmp_path / "history.db",
        "status_path": tmp_path / "status.json",
        "refresh_seconds": 900,
        "fetch_snapshots": True,
        "fetch_sales": True,
        "sales_contract_confirmed": True,
        "sales_contract_version": "written-v1",
        "sales_timestamps_are_utc": True,
        "api_key": "super-secret-key",
    }
    values.update(changes)
    return worker.WorkerSettings(**values)  # type: ignore[arg-type]


def test_http_client_uses_fixed_url_key_header_limits_and_no_redirects() -> None:
    response = _FakeResponse(200, b"zip", "application/zip")
    session = _FakeSession({history.CSPRICEAPI_EXPORT_URL: response})
    downloaded = history.download_cspriceapi_sales_zip("secret", session=session)
    assert downloaded.body == b"zip"
    assert len(session.calls) == 1
    url, headers, timeout, stream, redirects = session.calls[0]
    assert url == history.CSPRICEAPI_EXPORT_URL
    assert headers["x-api-key"] == "secret"
    assert timeout == (5.0, 90.0)
    assert stream is True
    assert redirects is False

    wrong_type = _FakeSession(
        {history.CSPRICEAPI_EXPORT_URL: _FakeResponse(200, b"not zip", "text/html")}
    )
    with pytest.raises(history.DownloadError) as raised:
        history.download_cspriceapi_sales_zip("secret", session=wrong_type)
    assert raised.value.code == "invalid_content_type"
    assert "secret" not in str(raised.value)


def test_automatic_network_gate_requires_enable_license_and_key(tmp_path: Path) -> None:
    session = _FakeSession({})
    for changes, reason in (
        ({"enabled": False}, "disabled"),
        ({"license_confirmed": False}, "license_not_confirmed"),
        ({"api_key": ""}, "missing_api_key"),
    ):
        report = worker.run_once(_settings(tmp_path, **changes), session=session)
        assert report["reason"] == reason
        assert report["network_attempted"] is False
    assert session.calls == []
    assert not (tmp_path / "history.db").exists()


def test_unconfirmed_sales_contract_skips_export_but_keeps_snapshots(
    tmp_path: Path,
) -> None:
    session = _FakeSession(
        {
            history.ECB_HISTORY_URL: _FakeResponse(200, ECB_XML, "application/xml"),
            history.CSPRICEAPI_SNAPSHOT_URLS["buff163"]: _FakeResponse(
                200, _snapshot("buff163"), "application/json"
            ),
            history.CSPRICEAPI_SNAPSHOT_URLS["youpin"]: _FakeResponse(
                200, _snapshot("youpin"), "application/json"
            ),
        }
    )
    report = worker.run_once(
        _settings(tmp_path, sales_contract_confirmed=False), session=session
    )
    assert report["sales"]["reason"] == "unverified_sales_export_contract"
    assert {call[0] for call in session.calls} == {
        history.ECB_HISTORY_URL,
        history.CSPRICEAPI_SNAPSHOT_URLS["buff163"],
        history.CSPRICEAPI_SNAPSHOT_URLS["youpin"],
    }
    with sqlite3.connect(tmp_path / "history.db") as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM price_snapshots").fetchone()[0]
            == 2
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM market_sale_events").fetchone()[0]
            == 0
        )


def test_sales_403_does_not_rollback_successful_snapshots(tmp_path: Path) -> None:
    session = _FakeSession(
        {
            history.ECB_HISTORY_URL: _FakeResponse(200, ECB_XML, "application/xml"),
            history.CSPRICEAPI_SNAPSHOT_URLS["buff163"]: _FakeResponse(
                200, _snapshot("buff163"), "application/json"
            ),
            history.CSPRICEAPI_SNAPSHOT_URLS["youpin"]: _FakeResponse(
                200, _snapshot("youpin"), "application/json"
            ),
            history.CSPRICEAPI_EXPORT_URL: _FakeResponse(
                403, b'{"error":"forbidden"}', "application/json"
            ),
        }
    )
    report = worker.run_once(_settings(tmp_path), session=session)
    assert report["state"] == "ready"
    assert report["sales"] == {
        "status": "skipped",
        "reason": "enterprise_export_not_authorized",
        "http_status": 403,
    }
    with sqlite3.connect(tmp_path / "history.db") as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM price_snapshots").fetchone()[0]
            == 2
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM market_sale_events").fetchone()[0]
            == 0
        )


def test_manual_zip_reader_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.zip"
    target.write_bytes(_zip_payload([_sale()]))
    link = tmp_path / "link.zip"
    try:
        os.symlink(target, link)
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows symlink privilege is not enabled")
        raise
    with pytest.raises(history.ValidationError) as raised:
        history.read_zip_file_bounded(link)
    assert raised.value.code == "unsafe_zip_file"


def test_migrate_only_initializes_database_without_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "migrated.db"
    settings = _settings(tmp_path, enabled=False, license_confirmed=False, api_key="")
    monkeypatch.setattr(
        worker.WorkerSettings,
        "from_environment",
        classmethod(lambda _cls: settings),
    )

    def fail_network(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("migrate-only must not call the network")

    monkeypatch.setattr(history, "download_ecb_rates", fail_network)
    monkeypatch.setattr(history, "download_cspriceapi_snapshot", fail_network)
    monkeypatch.setattr(history, "download_cspriceapi_sales_zip", fail_network)
    assert worker.main(["--db", str(database), "--migrate-only"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["network_attempted"] is False
    assert output["schema_version"] == history.SCHEMA_VERSION
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT value FROM market_history_meta WHERE key='schema_version'"
        ).fetchone()[0] == str(history.SCHEMA_VERSION)


@pytest.mark.parametrize(
    ("contract_confirmed", "expected_currency", "expected_verified"),
    ((False, "UNK", 0), (True, "CNY", 1)),
)
def test_manual_import_uses_confirmed_contract_only_when_all_gates_are_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    contract_confirmed: bool,
    expected_currency: str,
    expected_verified: int,
) -> None:
    archive = tmp_path / f"sales-{contract_confirmed}.zip"
    archive.write_bytes(_zip_payload([_sale()]))
    database = tmp_path / f"history-{contract_confirmed}.db"
    settings = _settings(
        tmp_path,
        enabled=False,
        license_confirmed=False,
        sales_contract_confirmed=contract_confirmed,
        sales_contract_version="written-v1" if contract_confirmed else "",
        sales_timestamps_are_utc=contract_confirmed,
    )
    monkeypatch.setattr(
        worker.WorkerSettings,
        "from_environment",
        classmethod(lambda _cls: settings),
    )
    assert worker.main(["--db", str(database), "--import-zip", str(archive)]) == 0
    capsys.readouterr()
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        event = connection.execute("SELECT * FROM market_sale_events").fetchone()
        run = connection.execute("SELECT * FROM ingest_runs").fetchone()
        assert event["currency"] == expected_currency
        assert event["currency_verified"] == expected_verified
        assert run["contract_version"] == ("written-v1" if contract_confirmed else None)
