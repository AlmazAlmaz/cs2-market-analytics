import io
import json
import sqlite3
import urllib.error
from pathlib import Path

import pytest

import cs2_item_catalog as catalog
import parser_final as parser

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "item_catalog"


@pytest.fixture
def bymykel_payload():
    return json.loads(
        (FIXTURE_DIR / "bymykel_skins_not_grouped.json").read_text(encoding="utf-8")
    )


@pytest.fixture
def fallback_bundle():
    return json.loads(
        (FIXTURE_DIR / "somespecialone_bundle.json").read_text(encoding="utf-8")
    )


@pytest.fixture
def grouped_payload():
    return json.loads(
        (FIXTURE_DIR / "bymykel_skins_grouped.json").read_text(encoding="utf-8")
    )


class FakeResponse:
    def __init__(self, raw, *, status=200, headers=None, url="https://example.test/catalog.json"):
        self._stream = io.BytesIO(raw)
        self.status = status
        self.headers = headers or {}
        self._url = url
        self.closed = False

    def read(self, size=-1):
        return self._stream.read(size)

    def geturl(self):
        return self._url

    def close(self):
        self.closed = True


def _build(payload):
    return catalog.build_catalog(payload, min_primary_rows=1)


def _repeat_kinds(payload, *, weapon_skin, knife, glove):
    templates = {
        "weapon_skin": payload[0],
        "knife": payload[1],
        "glove": payload[3],
    }
    result = []
    for kind, count in {
        "weapon_skin": weapon_skin,
        "knife": knife,
        "glove": glove,
    }.items():
        for index in range(count):
            entry = json.loads(json.dumps(templates[kind]))
            stem, separator, wear = entry["market_hash_name"].rpartition(" (")
            entry["market_hash_name"] = (
                f"{stem} Test {kind} {index}{separator}{wear}"
            )
            entry["name"] = entry["market_hash_name"]
            entry["id"] = f"{kind}-{index}"
            entry["skin_id"] = f"{kind}-base-{index}"
            result.append(entry)
    return result


def test_normalizes_exact_kinds_and_preserves_all_doppler_variants(bymykel_payload):
    build = _build(bymykel_payload)
    rows = {row["market_hash_name"]: row for row in build.rows}

    assert len(rows) == 3
    assert rows["AK-47 | Redline (Field-Tested)"]["item_kind"] == "weapon_skin"
    assert rows["★ Karambit | Doppler (Factory New)"]["item_kind"] == "knife"
    assert rows["★ Sport Gloves | Vice (Field-Tested)"]["item_kind"] == "glove"

    knife = rows["★ Karambit | Doppler (Factory New)"]
    assert knife["paint_index"] is None
    assert json.loads(knife["paint_indices_json"]) == [418, 419]
    assert json.loads(knife["phase_names_json"]) == ["Phase 1", "Phase 2"]
    assert knife["variant_count"] == 2

    weapon = rows["AK-47 | Redline (Field-Tested)"]
    assert weapon["weapon_def_index"] == 7
    assert weapon["wear_name"] == "Field-Tested"
    assert weapon["paint_name"] == "Redline"
    assert json.loads(weapon["collection_names_json"]) == ["The Phoenix Collection"]
    assert json.loads(weapon["crate_names_json"]) == ["Operation Phoenix Weapon Case"]
    assert weapon["image_url"] == "https://images.example.test/ak47-redline.png"
    assert len(build.variants) == 4


def test_grouped_feed_fills_relationships_without_defining_exact_variants(
    bymykel_payload, grouped_payload
):
    exact_gap = json.loads(json.dumps(bymykel_payload))
    for entry in exact_gap:
        entry["collections"] = []
        entry["crates"] = []

    build = catalog.build_catalog(
        exact_gap,
        enrichment_payload=grouped_payload,
        min_primary_rows=1,
        min_enrichment_rows=1,
    )
    rows = {row["market_hash_name"]: row for row in build.rows}

    assert len(rows) == 3
    assert len(build.variants) == 4
    assert "Grouped-only row must not become a market variant" not in rows
    assert json.loads(
        rows["AK-47 | Redline (Field-Tested)"]["collection_names_json"]
    ) == ["The Phoenix Collection"]
    assert json.loads(
        rows["★ Karambit | Doppler (Factory New)"]["crate_names_json"]
    ) == ["Gamma Case", "Gamma 2 Case"]
    assert rows["★ Karambit | Doppler (Factory New)"]["source_id"] == (
        catalog.PRIMARY_SOURCE_ID
    )
    assert build.crosscheck["enrichment_rows"] == 5
    assert build.crosscheck["enrichment_matched_variants"] == 4
    assert build.crosscheck["enrichment_collection_fills"] == 1
    assert build.crosscheck["enrichment_crate_fills"] == 4


def test_grouped_feed_never_overrides_primary_relationships(
    bymykel_payload, grouped_payload
):
    primary = json.loads(json.dumps(bymykel_payload))
    primary[0]["collections"] = [
        {"id": "primary-collection", "name": "Primary Collection"}
    ]
    grouped = json.loads(json.dumps(grouped_payload))
    grouped[0]["collections"] = [
        {"id": "secondary-collection", "name": "Secondary Collection"}
    ]

    build = catalog.build_catalog(
        primary,
        enrichment_payload=grouped,
        min_primary_rows=1,
        min_enrichment_rows=1,
    )
    weapon = next(
        row for row in build.rows if row["market_hash_name"].startswith("AK-47")
    )
    assert json.loads(weapon["collection_names_json"]) == ["Primary Collection"]
    assert build.crosscheck["enrichment_collection_fills"] == 0


def test_grouped_feed_rejects_ambiguous_duplicate_ids(
    bymykel_payload, grouped_payload
):
    grouped = [grouped_payload[0], dict(grouped_payload[0])]
    with pytest.raises(catalog.CatalogEnrichmentValidationError, match="duplicate id"):
        catalog.build_catalog(
            bymykel_payload,
            enrichment_payload=grouped,
            min_primary_rows=1,
            min_enrichment_rows=1,
        )


def test_grouped_feed_rejects_plausible_but_truncated_join_coverage(
    bymykel_payload, grouped_payload
):
    with pytest.raises(
        catalog.CatalogEnrichmentValidationError,
        match="exact-variant match coverage",
    ):
        catalog.build_catalog(
            bymykel_payload,
            enrichment_payload=grouped_payload[:1],
            min_primary_rows=1,
            min_enrichment_rows=1,
        )


def test_schema_bundle_crosscheck_fills_and_adds_without_overriding_primary(
    bymykel_payload, fallback_bundle
):
    build = catalog.build_catalog(
        bymykel_payload,
        fallback_payload=fallback_bundle,
        min_primary_rows=1,
    )
    rows = {row["market_hash_name"]: row for row in build.rows}

    assert rows["AK-47 | Redline (Field-Tested)"]["source_id"].startswith(
        catalog.PRIMARY_SOURCE_ID
    )
    fallback_knife = rows["★ Bayonet | Fallback Finish (Factory New)"]
    assert fallback_knife["item_kind"] == "knife"
    assert fallback_knife["phase_name"] == "Phase 4"
    assert json.loads(fallback_knife["crate_names_json"]) == ["Fallback Knife Case"]
    assert build.crosscheck["matched_market_names"] >= 1
    assert build.crosscheck["fallback_only_market_names"] >= 1


def test_sqlite_publish_is_atomic_and_persists_mit_attribution(bymykel_payload):
    conn = sqlite3.connect(":memory:")
    catalog.ensure_catalog_schema(conn)
    good = _build(bymykel_payload)
    catalog.publish_catalog(
        conn,
        good,
        build_id="good-build",
        source_updated_at="2026-08-21T10:00:00+00:00",
        metadata={
            "primary_attribution_json": json.dumps(catalog.PRIMARY_ATTRIBUTION),
            "primary_payload_sha256": "a" * 64,
        },
    )

    bad = catalog.CatalogBuild(
        rows=[{**good.rows[0], "item_kind": "case"}],
        variants=[good.variants[0]],
        crosscheck={},
    )
    with pytest.raises(sqlite3.IntegrityError):
        catalog.publish_catalog(
            conn,
            bad,
            build_id="bad-build",
            source_updated_at="2026-08-21T11:00:00+00:00",
            metadata={},
        )

    assert catalog.catalog_row_count(conn) == 3
    assert conn.execute(
        "SELECT COUNT(*) FROM cs2_item_catalog WHERE catalog_build_id='good-build'"
    ).fetchone()[0] == 3
    metadata = dict(conn.execute("SELECT key, value FROM cs2_item_catalog_meta"))
    assert metadata["active_build_id"] == "good-build"
    attribution = json.loads(metadata["primary_attribution_json"])
    assert attribution["license"] == "MIT"
    assert attribution["repository_url"] == "https://github.com/ByMykel/CSGO-API"


def test_conditional_fetch_uses_etag_and_verified_cache(tmp_path, bymykel_payload):
    raw = json.dumps(bymykel_payload).encode("utf-8")
    requests = []

    def first_open(request, *, timeout):
        requests.append((request, timeout))
        return FakeResponse(
            raw,
            headers={
                "Content-Length": str(len(raw)),
                "ETag": '"fixture-etag"',
                "Last-Modified": "Fri, 21 Aug 2026 09:00:00 GMT",
            },
        )

    spec = catalog.SourceSpec(
        source_id="fixture",
        url="https://example.test/catalog.json",
        cache_name="fixture",
        attribution={"license": "MIT"},
    )
    first = catalog.fetch_json_source(spec, cache_dir=tmp_path, opener=first_open)
    catalog.commit_source_cache(first, cache_dir=tmp_path)

    def not_modified(request, *, timeout):
        requests.append((request, timeout))
        raise urllib.error.HTTPError(
            request.full_url,
            304,
            "Not Modified",
            {},
            None,
        )

    second = catalog.fetch_json_source(spec, cache_dir=tmp_path, opener=not_modified)

    assert second.not_modified is True
    assert second.sha256 == first.sha256
    assert second.payload == bymykel_payload
    assert requests[-1][0].get_header("If-none-match") == '"fixture-etag"'
    assert (
        requests[-1][0].get_header("If-modified-since")
        == "Fri, 21 Aug 2026 09:00:00 GMT"
    )
    cache_metadata = json.loads((tmp_path / "fixture.meta.json").read_text())
    assert cache_metadata["attribution"]["license"] == "MIT"


def test_cache_provenance_redacts_query_and_uses_hash_for_conditionals(
    tmp_path, bymykel_payload
):
    raw = json.dumps(bymykel_payload).encode("utf-8")
    secret_url = "https://example.test/catalog.json?token=do-not-persist"
    spec = catalog.SourceSpec(
        source_id="fixture",
        url=secret_url,
        cache_name="redacted",
        attribution={
            "license": "MIT",
            "dataset_url": secret_url,
            "private_note": "must-not-be-published",
        },
    )
    first = catalog.fetch_json_source(
        spec,
        cache_dir=tmp_path,
        opener=lambda request, timeout: FakeResponse(
            raw,
            headers={"ETag": '"redacted-etag"'},
            url=secret_url,
        ),
    )
    catalog.commit_source_cache(first, cache_dir=tmp_path)

    metadata_path = tmp_path / "redacted.meta.json"
    metadata_text = metadata_path.read_text(encoding="utf-8")
    metadata = json.loads(metadata_text)
    assert "do-not-persist" not in metadata_text
    assert metadata["url"] == "https://example.test/catalog.json"
    assert len(metadata["url_sha256"]) == 64
    assert metadata["attribution"]["dataset_url"] == (
        "https://example.test/catalog.json"
    )
    assert "private_note" not in metadata["attribution"]

    # Simulate metadata written by an older release: a 304 must still rewrite
    # it into the redacted/hash-bound form without rewriting the payload.
    metadata["url"] = secret_url
    metadata.pop("url_sha256")
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    requests = []

    def not_modified(request, *, timeout):
        requests.append(request)
        raise urllib.error.HTTPError(request.full_url, 304, "Not Modified", {}, None)

    second = catalog.fetch_json_source(
        spec,
        cache_dir=tmp_path,
        opener=not_modified,
    )
    assert second.not_modified is True
    assert requests[0].get_header("If-none-match") == '"redacted-etag"'
    catalog.commit_source_cache(second, cache_dir=tmp_path)
    upgraded_text = metadata_path.read_text(encoding="utf-8")
    assert "do-not-persist" not in upgraded_text
    assert json.loads(upgraded_text)["url"] == "https://example.test/catalog.json"


def test_source_url_with_userinfo_is_rejected_before_network(tmp_path):
    spec = catalog.SourceSpec(
        source_id="fixture",
        url="https://user:secret@example.test/catalog.json",
        cache_name="userinfo",
        attribution={},
    )
    with pytest.raises(catalog.CatalogDownloadError, match="unauthenticated HTTPS"):
        catalog.fetch_json_source(
            spec,
            cache_dir=tmp_path,
            opener=lambda *args, **kwargs: pytest.fail("must reject before network"),
        )

    conn = sqlite3.connect(":memory:")
    result = catalog.refresh_catalog_if_due(
        conn,
        cache_dir=tmp_path,
        primary_url=spec.url,
        opener=lambda *args, **kwargs: pytest.fail("must reject before network"),
        force=True,
        min_primary_rows=1,
    )
    assert result["status"] == "unavailable"
    assert "secret" not in json.dumps(result)


def test_download_rejects_declared_and_streamed_oversize(tmp_path):
    spec = catalog.SourceSpec(
        source_id="fixture",
        url="https://example.test/catalog.json",
        cache_name="fixture",
        attribution={},
    )

    with pytest.raises(catalog.CatalogDownloadError, match="limit"):
        catalog.fetch_json_source(
            spec,
            cache_dir=tmp_path,
            max_bytes=8,
            opener=lambda request, timeout: FakeResponse(
                b"[]", headers={"Content-Length": "9"}
            ),
        )

    with pytest.raises(catalog.CatalogDownloadError, match="exceeded"):
        catalog.fetch_json_source(
            spec,
            cache_dir=tmp_path,
            max_bytes=8,
            opener=lambda request, timeout: FakeResponse(b"[123456789]"),
        )


def test_download_enforces_total_deadline_and_closes_response(tmp_path, monkeypatch):
    spec = catalog.SourceSpec(
        source_id="fixture",
        url="https://example.test/catalog.json",
        cache_name="fixture",
        attribution={},
    )
    response = FakeResponse(b"[]")
    ticks = iter((0.0, 2.0))
    monkeypatch.setattr(catalog.time, "monotonic", lambda: next(ticks))

    with pytest.raises(catalog.CatalogDownloadError, match="time limit"):
        catalog.fetch_json_source(
            spec,
            cache_dir=tmp_path,
            deadline=1.0,
            opener=lambda request, timeout: response,
        )
    assert response.closed is True


def test_invalid_schema_never_poison_commits_download_cache(tmp_path):
    raw = json.dumps([{"unexpected": "shape"}]).encode("utf-8")
    conn = sqlite3.connect(":memory:")
    result = catalog.refresh_catalog_if_due(
        conn,
        cache_dir=tmp_path,
        primary_url="https://example.test/catalog.json",
        opener=lambda request, timeout: FakeResponse(raw),
        min_primary_rows=1,
        required_kinds=(),
    )

    assert result["status"] == "invalid"
    assert catalog.catalog_row_count(conn) == 0
    assert list(tmp_path.iterdir()) == []


def test_due_refresh_publishes_then_fresh_path_never_opens_network(
    tmp_path, bymykel_payload
):
    raw = json.dumps(bymykel_payload).encode("utf-8")
    calls = []

    def opener(request, *, timeout):
        calls.append(request.full_url)
        return FakeResponse(raw)

    conn = sqlite3.connect(":memory:")
    first = catalog.refresh_catalog_if_due(
        conn,
        cache_dir=tmp_path,
        primary_url="https://example.test/catalog.json",
        opener=opener,
        now=1_000,
        min_primary_rows=1,
    )
    assert first["status"] == "updated"
    assert len(calls) == 1

    second = catalog.refresh_catalog_if_due(
        conn,
        cache_dir=tmp_path,
        primary_url="https://example.test/catalog.json",
        opener=lambda *args, **kwargs: pytest.fail("fresh catalog must not use network"),
        now=1_100,
        min_primary_rows=1,
    )
    assert second == {"status": "fresh", "row_count": 3, "network": False}


def test_refresh_publishes_grouped_enrichment_and_sanitized_provenance(
    tmp_path, bymykel_payload, grouped_payload
):
    primary_raw = json.dumps(
        [{**entry, "collections": [], "crates": []} for entry in bymykel_payload]
    ).encode("utf-8")
    grouped_raw = json.dumps(grouped_payload).encode("utf-8")
    primary_url = "https://example.test/skins_not_grouped.json?token=primary-secret"
    enrichment_url = "https://example.test/skins.json?token=grouped-secret"

    def opener(request, *, timeout):
        if "/skins_not_grouped.json" in request.full_url:
            return FakeResponse(primary_raw, url=request.full_url)
        if "/skins.json" in request.full_url:
            return FakeResponse(grouped_raw, url=request.full_url)
        pytest.fail(f"unexpected URL path: {request.full_url}")

    conn = sqlite3.connect(":memory:")
    result = catalog.refresh_catalog_if_due(
        conn,
        cache_dir=tmp_path,
        primary_url=primary_url,
        enrichment_enabled=True,
        enrichment_url=enrichment_url,
        opener=opener,
        min_primary_rows=1,
        min_enrichment_rows=1,
    )

    assert result["status"] == "updated"
    knife_crates = conn.execute(
        """
        SELECT crate_names_json FROM cs2_item_catalog
        WHERE market_hash_name='★ Karambit | Doppler (Factory New)'
        """
    ).fetchone()[0]
    assert json.loads(knife_crates) == ["Gamma Case", "Gamma 2 Case"]
    metadata = dict(conn.execute("SELECT key, value FROM cs2_item_catalog_meta"))
    assert metadata["primary_source_url"] == (
        "https://example.test/skins_not_grouped.json"
    )
    assert metadata["enrichment_source_url"] == "https://example.test/skins.json"
    assert "primary-secret" not in json.dumps(metadata)
    assert "grouped-secret" not in json.dumps(metadata)
    assert json.loads(metadata["enrichment_attribution_json"])["license"] == "MIT"
    assert len(metadata["enrichment_payload_sha256"]) == 64


def test_enabled_enrichment_failure_never_publishes_partial_catalog(
    tmp_path, bymykel_payload
):
    primary_raw = json.dumps(bymykel_payload).encode("utf-8")

    def opener(request, *, timeout):
        if "skins_not_grouped" in request.full_url:
            return FakeResponse(primary_raw, url=request.full_url)
        raise OSError("secondary unavailable")

    conn = sqlite3.connect(":memory:")
    result = catalog.refresh_catalog_if_due(
        conn,
        cache_dir=tmp_path,
        primary_url="https://example.test/skins_not_grouped.json",
        enrichment_enabled=True,
        enrichment_url="https://example.test/skins.json",
        opener=opener,
        min_primary_rows=1,
        min_enrichment_rows=1,
    )

    assert result["status"] == "unavailable"
    assert result["row_count"] == 0
    assert result["error"] == "catalog request failed (OSError)"
    assert list(tmp_path.iterdir()) == []


def test_grouped_enrichment_upgrades_existing_primary_304_snapshot(
    tmp_path, bymykel_payload, grouped_payload
):
    primary_gap = [
        {**entry, "collections": [], "crates": []} for entry in bymykel_payload
    ]
    primary_raw = json.dumps(primary_gap).encode("utf-8")
    grouped_raw = json.dumps(grouped_payload).encode("utf-8")
    primary_url = "https://example.test/skins_not_grouped.json"
    enrichment_url = "https://example.test/skins.json"
    conn = sqlite3.connect(":memory:")

    first = catalog.refresh_catalog_if_due(
        conn,
        cache_dir=tmp_path,
        primary_url=primary_url,
        opener=lambda request, timeout: FakeResponse(
            primary_raw,
            headers={"ETag": '"primary-v1"'},
            url=request.full_url,
        ),
        min_primary_rows=1,
    )
    assert first["status"] == "updated"
    assert conn.execute(
        "SELECT crate_names_json FROM cs2_item_catalog WHERE item_kind='knife' LIMIT 1"
    ).fetchone()[0] == "[]"

    calls = []

    def second_opener(request, *, timeout):
        calls.append(request)
        if "skins_not_grouped" in request.full_url:
            assert request.get_header("If-none-match") == '"primary-v1"'
            raise urllib.error.HTTPError(
                request.full_url, 304, "Not Modified", {}, None
            )
        return FakeResponse(grouped_raw, url=request.full_url)

    second = catalog.refresh_catalog_if_due(
        conn,
        cache_dir=tmp_path,
        primary_url=primary_url,
        enrichment_enabled=True,
        enrichment_url=enrichment_url,
        opener=second_opener,
        force=True,
        min_primary_rows=1,
        min_enrichment_rows=1,
    )

    assert second["status"] == "updated"
    assert len(calls) == 2
    assert json.loads(
        conn.execute(
            "SELECT crate_names_json FROM cs2_item_catalog WHERE item_kind='knife' LIMIT 1"
        ).fetchone()[0]
    ) == ["Gamma Case", "Gamma 2 Case"]


def test_refresh_shrink_guard_preserves_last_good_total_count(
    tmp_path, bymykel_payload
):
    conn = sqlite3.connect(":memory:")
    catalog.ensure_catalog_schema(conn)
    last_good = catalog.build_catalog(
        _repeat_kinds(bymykel_payload, weapon_skin=1_200, knife=100, glove=100),
        min_primary_rows=1,
    )
    catalog.publish_catalog(
        conn,
        last_good,
        build_id="last-good",
        source_updated_at="2026-08-21T10:00:00+00:00",
        metadata={},
    )
    candidate_raw = json.dumps(
        _repeat_kinds(bymykel_payload, weapon_skin=1_000, knife=1, glove=1)
    ).encode("utf-8")

    result = catalog.refresh_catalog_if_due(
        conn,
        cache_dir=tmp_path,
        primary_url="https://example.test/catalog.json",
        opener=lambda request, timeout: FakeResponse(candidate_raw),
        force=True,
        min_primary_rows=1_000,
    )

    assert result["status"] == "stale"
    assert "shrink guard" in result["error"]
    assert catalog.catalog_row_count(conn) == 1_400
    assert conn.execute(
        "SELECT COUNT(*) FROM cs2_item_catalog WHERE catalog_build_id='last-good'"
    ).fetchone()[0] == 1_400
    assert list(tmp_path.iterdir()) == []


def test_refresh_shrink_guard_preserves_last_good_per_kind_count(
    tmp_path, bymykel_payload
):
    conn = sqlite3.connect(":memory:")
    catalog.ensure_catalog_schema(conn)
    last_good = catalog.build_catalog(
        _repeat_kinds(bymykel_payload, weapon_skin=10, knife=10, glove=10),
        min_primary_rows=1,
    )
    catalog.publish_catalog(
        conn,
        last_good,
        build_id="last-good-kinds",
        source_updated_at="2026-08-21T10:00:00+00:00",
        metadata={},
    )
    candidate = _repeat_kinds(
        bymykel_payload,
        weapon_skin=21,
        knife=1,
        glove=8,
    )

    result = catalog.refresh_catalog_if_due(
        conn,
        cache_dir=tmp_path,
        primary_url="https://example.test/catalog.json",
        opener=lambda request, timeout: FakeResponse(json.dumps(candidate).encode()),
        force=True,
        min_primary_rows=1,
    )

    assert result["status"] == "stale"
    assert "knife 1 is below retained floor 8 of 10" in result["error"]
    assert catalog.catalog_row_count(conn) == 30


def test_catalog_schedule_merge_adds_knives_without_deleting_or_resetting_history(
    bymykel_payload
):
    conn = sqlite3.connect(":memory:")
    parser.run_schema_migrations(conn)
    catalog.ensure_catalog_schema(conn)
    catalog.publish_catalog(
        conn,
        _build(bymykel_payload),
        build_id="fixture-build",
        source_updated_at="2026-08-21T10:00:00+00:00",
        metadata={},
    )

    existing_knife = "★ Karambit | Doppler (Factory New)"
    user_target = "User Custom Target"
    parser.ensure_skin_schedule(
        conn,
        [
            {"market_hash_name": existing_knife, "paint_index": None},
            {"market_hash_name": user_target, "paint_index": 777},
        ],
    )
    conn.execute(
        """
        UPDATE skin_schedule
        SET request_count=19, total_sales_returned=321, last_fetch_at=123456
        WHERE market_hash_name=?
        """,
        (existing_knife,),
    )
    conn.commit()

    merged = catalog.merge_schedule_items(
        [
            {"market_hash_name": "AK-47 | Legacy (Field-Tested)"},
            {"market_hash_name": existing_knife, "paint_index": 418},
        ],
        catalog.load_schedule_items(conn),
    )
    assert next(
        item for item in merged if item["market_hash_name"] == existing_knife
    )["paint_index"] is None
    parser.ensure_skin_schedule(conn, merged)

    scheduled = {
        row[0]
        for row in conn.execute("SELECT market_hash_name FROM skin_schedule")
    }
    assert user_target in scheduled
    assert {row["market_hash_name"] for row in merged}.issubset(scheduled)
    assert conn.execute(
        """
        SELECT request_count, total_sales_returned, last_fetch_at, paint_index
        FROM skin_schedule WHERE market_hash_name=?
        """,
        (existing_knife,),
    ).fetchone() == (19, 321, 123456, None)


def test_reexported_classifier_reads_authoritative_catalog(bymykel_payload):
    conn = sqlite3.connect(":memory:")
    catalog.ensure_catalog_schema(conn)
    catalog.publish_catalog(
        conn,
        _build(bymykel_payload),
        build_id="fixture-build",
        source_updated_at="2026-08-21T10:00:00+00:00",
        metadata={},
    )
    assert (
        catalog.classify_market_hash_name(
            "★ Sport Gloves | Vice (Field-Tested)", conn=conn
        )
        == "glove"
    )


def test_parser_migrate_only_creates_local_schema_without_catalog_network(
    tmp_path, monkeypatch
):
    database = tmp_path / "history.db"
    skins_path = tmp_path / "skins.json"
    skins_path.write_text(
        json.dumps([{"market_hash_name": "AK-47 | Test (Field-Tested)"}]),
        encoding="utf-8",
    )
    monkeypatch.setattr(parser, "DB_PATH", str(database))
    monkeypatch.setattr(parser, "SKINS_PATH", str(skins_path))
    monkeypatch.setattr(parser, "LOCK_PATH", tmp_path / "parser.lock")
    monkeypatch.setattr(parser, "update_parser_state", lambda **kwargs: None)
    monkeypatch.setattr(parser, "CATALOG_REFRESH_ENABLED", True)
    monkeypatch.setattr(
        parser.cs2_item_catalog,
        "refresh_catalog_if_due",
        lambda *args, **kwargs: pytest.fail("--migrate-only must not use catalog network"),
    )

    assert parser.main(["--migrate-only"]) == 0

    with sqlite3.connect(database) as conn:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {
            "cs2_item_catalog",
            "cs2_item_catalog_variants",
            "cs2_item_catalog_meta",
        }.issubset(tables)
        assert conn.execute("SELECT COUNT(*) FROM skin_schedule").fetchone()[0] == 1


def test_parser_refreshes_catalog_before_loading_and_scheduling(tmp_path, monkeypatch):
    database = tmp_path / "history.db"
    events = []
    captured_skins = []
    knife = "★ Bayonet | Fade (Factory New)"

    monkeypatch.setattr(parser, "DB_PATH", str(database))
    monkeypatch.setattr(parser, "LOCK_PATH", tmp_path / "parser.lock")
    monkeypatch.setattr(parser, "update_parser_state", lambda **kwargs: None)
    monkeypatch.setattr(parser, "CATALOG_REFRESH_ENABLED", True)
    monkeypatch.setattr(parser, "initialize_api_keys", lambda: None)
    monkeypatch.setattr(parser, "print_loaded_key_routes", lambda: None)

    def refresh(conn, **kwargs):
        events.append("refresh")
        return {"status": "fresh", "row_count": 1, "network": False}

    def load_base():
        events.append("load_base")
        return [{"market_hash_name": "AK-47 | Legacy (Field-Tested)"}]

    def load_catalog(conn):
        events.append("load_catalog")
        return [{"market_hash_name": knife, "paint_index": 38, "item_kind": "knife"}]

    def run_scheduler(conn, skins, **kwargs):
        events.append("scheduler")
        captured_skins.extend(skins)
        return 0

    monkeypatch.setattr(parser.cs2_item_catalog, "refresh_catalog_if_due", refresh)
    monkeypatch.setattr(parser, "load_skins", load_base)
    monkeypatch.setattr(parser.cs2_item_catalog, "load_schedule_items", load_catalog)
    monkeypatch.setattr(parser, "run_scheduler", run_scheduler)

    assert parser.main(["--once"]) == 0
    assert events.index("refresh") < events.index("load_base") < events.index("scheduler")
    assert knife in {item["market_hash_name"] for item in captured_skins}
    with sqlite3.connect(database) as conn:
        assert conn.execute(
            "SELECT paint_index FROM skin_schedule WHERE market_hash_name=?", (knife,)
        ).fetchone() == (38,)


def test_refresh_catalog_only_is_forced_bounded_and_never_initializes_csfloat(
    tmp_path, monkeypatch
):
    database = tmp_path / "history.db"
    knife = "★ Bayonet | Fade (Factory New)"
    refresh_calls = []

    monkeypatch.setattr(parser, "DB_PATH", str(database))
    monkeypatch.setattr(parser, "LOCK_PATH", tmp_path / "parser.lock")
    monkeypatch.setattr(parser, "update_parser_state", lambda **kwargs: None)
    monkeypatch.setattr(parser, "CATALOG_REFRESH_ENABLED", False)
    monkeypatch.setattr(
        parser,
        "load_skins",
        lambda: [{"market_hash_name": "AK-47 | Legacy (Field-Tested)"}],
    )
    monkeypatch.setattr(
        parser.cs2_item_catalog,
        "load_schedule_items",
        lambda conn: [
            {"market_hash_name": knife, "paint_index": 38, "item_kind": "knife"}
        ],
    )

    def refresh(conn, **kwargs):
        refresh_calls.append(kwargs)
        return {"status": "updated", "row_count": 1, "network": True}

    monkeypatch.setattr(parser.cs2_item_catalog, "refresh_catalog_if_due", refresh)
    monkeypatch.setattr(
        parser,
        "initialize_api_keys",
        lambda: pytest.fail("catalog-only must not initialize CSFloat keys"),
    )
    monkeypatch.setattr(
        parser,
        "run_scheduler",
        lambda *args, **kwargs: pytest.fail("catalog-only must not run history scheduler"),
    )

    assert parser.main(["--refresh-catalog-only"]) == 0
    assert len(refresh_calls) == 1
    assert refresh_calls[0]["force"] is True
    assert refresh_calls[0]["enrichment_enabled"] is parser.CATALOG_ENRICHMENT_ENABLED
    assert refresh_calls[0]["enrichment_url"] == parser.CATALOG_ENRICHMENT_URL
    assert refresh_calls[0]["timeout_seconds"] == parser.CATALOG_TIMEOUT_SECONDS
    assert (
        refresh_calls[0]["total_timeout_seconds"]
        == parser.CATALOG_TOTAL_TIMEOUT_SECONDS
    )
    with sqlite3.connect(database) as conn:
        assert conn.execute(
            "SELECT paint_index FROM skin_schedule WHERE market_hash_name=?", (knife,)
        ).fetchone() == (38,)


@pytest.mark.parametrize(
    "arguments",
    [
        ["--refresh-catalog-only", "--migrate-only"],
        ["--refresh-catalog-only", "--once"],
        ["--migrate-only", "--once"],
    ],
)
def test_parser_rejects_conflicting_terminal_modes(arguments):
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(arguments)
    assert exc_info.value.code == 2
