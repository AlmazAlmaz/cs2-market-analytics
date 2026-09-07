import copy
import json
import sqlite3

import pytest

import parser_final as parser


@pytest.fixture
def parser_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    parser.create_tables(conn)
    parser.ensure_sales_columns(conn)
    parser.ensure_sticker_columns(conn)
    parser.ensure_schedule_columns(conn)
    parser.ensure_request_log_columns(conn)
    parser.ensure_parser_indexes(conn)
    try:
        yield conn
    finally:
        conn.close()


def make_sale(listing_id="sale-1"):
    return {
        "id": listing_id,
        "price": 12_500,
        "state": "sold",
        "type": "buy_now",
        "sold_at": "2026-08-01T12:00:00Z",
        "created_at": "2026-08-01T11:00:00Z",
        "watchers": 3,
        "reference": {
            "base_price": 11_000,
            "predicted_price": 12_000,
            "float_factor": 0,
            "quantity": 17,
            "keychain_price": 222,
            "last_updated": "2026-08-01T10:00:00Z",
        },
        "item": {
            "market_hash_name": "AK-47 | Test (Field-Tested)",
            "float_value": 0.25,
            "paint_seed": 0,
            "paint_index": 7,
            "stickers": [
                {
                    "stickerId": 101,
                    "slot": 0,
                    "name": "Sticker | Test",
                    "wear": 0,
                    "rotation": 17.5,
                    "steam_market_url": "https://steamcommunity.com/market/listings/730/test-sticker",
                    "reference": {
                        "price": 345,
                        "quantity": 9,
                        "updated_at": "2026-08-01T09:00:00Z",
                    },
                }
            ],
            "keychains": [
                {
                    "stickerId": 501,
                    "slot": 0,
                    "name": "Charm | Test",
                    "pattern": 42,
                    "offset_x": 0.1,
                    "offset_y": 0.2,
                    "offset_z": 0.3,
                    "steam_market_url": "https://steamcommunity.com/market/listings/730/test-charm",
                    "reference": {
                        "price": 456,
                        "quantity": 5,
                        "updated_at": "2026-08-01T08:00:00Z",
                    },
                }
            ],
        },
    }


SCHEDULER_NOW = 10_000
SCHEDULER_SKIN = "AK-47 | Scheduler Test (Field-Tested)"


def prepare_scheduler_test(monkeypatch, conn, *, due=True):
    state_updates = []
    monkeypatch.setattr(parser, "STOP_REQUESTED", False)
    monkeypatch.setattr(parser, "unix_now", lambda: SCHEDULER_NOW)
    monkeypatch.setattr(parser, "iso_now", lambda: "2026-08-10T10:00:00+00:00")
    monkeypatch.setattr(parser, "SCHEDULE_JITTER_FRACTION", 0)
    monkeypatch.setattr(
        parser,
        "update_parser_state",
        lambda **values: state_updates.append(values),
    )
    skins = [{"market_hash_name": SCHEDULER_SKIN, "paint_index": 7}]
    parser.ensure_skin_schedule(conn, skins)
    conn.execute("""
        UPDATE skin_schedule
        SET request_count = 1,
            next_fetch_at = ?,
            interval_seconds = 3600,
            interval_reason = 'test_seed',
            last_fetch_at = 9000,
            last_success_at = 9000,
            attempt_count = 1,
            total_sales_returned = 0,
            total_new_sales = 0,
            avg_sales_count = 0,
            avg_new_sales_count = 0
        WHERE market_hash_name = ?
    """, (
        SCHEDULER_NOW - 1 if due else SCHEDULER_NOW + 3600,
        SCHEDULER_SKIN,
    ))
    conn.commit()
    return skins, state_updates


def test_decode_sales_response_accepts_legacy_list():
    page = parser.decode_sales_response(
        json.dumps([{"id": "one"}, {"id": "two"}]),
        "https://example.invalid/history",
        "Test Skin",
        {"path": "key", "proxy_label": "direct"},
    )

    assert isinstance(page, parser.SalesPage)
    assert [sale["id"] for sale in page.sales] == ["one", "two"]
    assert page.cursor is None


@pytest.mark.parametrize(
    ("payload", "expected_cursor"),
    [
        ({"data": [{"id": "one"}], "cursor": "next/page+1"}, "next/page+1"),
        ({"sales": [{"id": "two"}], "next_cursor": 123}, "123"),
    ],
)
def test_decode_sales_response_accepts_cursor_envelopes(payload, expected_cursor):
    page = parser.decode_sales_response(
        json.dumps(payload),
        "https://example.invalid/history",
        "Test Skin",
        {"path": "key", "proxy_label": "direct"},
    )

    assert len(page.sales) == 1
    assert page.cursor == expected_cursor


@pytest.mark.parametrize("payload", [{"data": {}}, {"error": "bad shape"}, "wrong"])
def test_decode_sales_response_rejects_unexpected_shape(payload, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    with pytest.raises(parser.SalesFetchError) as error:
        parser.decode_sales_response(
            json.dumps(payload),
            "https://example.invalid/history",
            "Test Skin",
            {"path": "key", "proxy_label": "direct"},
        )

    assert error.value.outcome == "invalid_payload"
    assert (tmp_path / "last_sales_error.txt").exists()


def test_one_second_sleep_really_advances_clock(monkeypatch):
    clock = [100.0]
    sleeps = []
    state_updates = []

    monkeypatch.setattr(parser, "STOP_REQUESTED", False)
    monkeypatch.setattr(parser.time, "monotonic", lambda: clock[0])

    def fake_sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(parser.time, "sleep", fake_sleep)
    monkeypatch.setattr(
        parser,
        "update_parser_state",
        lambda **values: state_updates.append(values),
    )

    parser.sleep_with_message(1, "test delay")

    assert sum(sleeps) == pytest.approx(1.0)
    assert clock[0] == pytest.approx(101.0)
    assert state_updates[0]["sleep_reason"] == "test delay"
    assert state_updates[-1] == {"sleeping_until": None, "sleep_reason": None}


def test_adaptive_scheduler_tightens_saturated_pages(monkeypatch):
    monkeypatch.setattr(parser, "SCHEDULE_JITTER_FRACTION", 0)

    result = parser.adaptive_schedule_values(
        market_hash_name="AK-47 | Test (Field-Tested)",
        now=2 * 86_400,
        sales_count=40,
        new_sales_count=40,
        overlap_count=0,
        page_saturated=True,
        old_rate_per_day=40,
        old_interval_seconds=86_400,
        old_saturation_streak=1,
        previous_success_at=86_400,
        sales_span_seconds=86_400,
        new_sales_span_seconds=86_400,
        request_count=2,
    )

    assert result["interval_reason"] == "saturated_catchup"
    assert result["saturation_streak"] == 2
    assert result["estimated_sales_per_day"] >= 40
    assert parser.MIN_SKIN_INTERVAL_SECONDS <= result["interval_seconds"] < 43_200


def test_adaptive_scheduler_targets_requested_new_sales(monkeypatch):
    monkeypatch.setattr(parser, "SCHEDULE_JITTER_FRACTION", 0)

    result = parser.adaptive_schedule_values(
        market_hash_name="AK-47 | Test (Field-Tested)",
        now=2 * 86_400,
        sales_count=20,
        new_sales_count=20,
        overlap_count=5,
        page_saturated=False,
        old_rate_per_day=20,
        old_interval_seconds=2 * 86_400,
        old_saturation_streak=3,
        previous_success_at=86_400,
        sales_span_seconds=86_400,
        new_sales_span_seconds=86_400,
        request_count=4,
    )

    assert result == {
        "interval_seconds": 86_400,
        "interval_reason": "adaptive_rate",
        "estimated_sales_per_day": 20,
        "saturation_streak": 0,
    }


def test_adaptive_scheduler_zero_demand_uses_cold_interval(monkeypatch):
    monkeypatch.setattr(parser, "SCHEDULE_JITTER_FRACTION", 0)

    result = parser.adaptive_schedule_values(
        market_hash_name="AK-47 | Test (Field-Tested)",
        now=2 * 86_400,
        sales_count=0,
        new_sales_count=0,
        overlap_count=0,
        page_saturated=False,
        old_rate_per_day=0,
        old_interval_seconds=86_400,
        old_saturation_streak=0,
        previous_success_at=86_400,
        request_count=1,
    )

    assert result["estimated_sales_per_day"] == 0
    assert result["interval_reason"] == "fallback_demand"
    assert result["interval_seconds"] == parser.MAX_SKIN_INTERVAL_SECONDS


def test_duplicate_sale_preserves_immutable_core_and_rich_sticker(parser_db):
    first = make_sale()
    first_seen_at = "2026-08-01T12:05:00+00:00"
    second_seen_at = "2026-08-02T12:05:00+00:00"

    first_stats = parser.save_sales_batch(
        parser_db,
        [first],
        first["item"]["market_hash_name"],
        observed_at=first_seen_at,
    )
    duplicate = copy.deepcopy(first)
    duplicate["price"] = 99_999
    duplicate["sold_at"] = "2026-08-02T12:00:00Z"
    duplicate["item"]["market_hash_name"] = "AWP | Changed (Factory New)"
    duplicate["item"]["stickers"][0]["steam_market_url"] = None
    duplicate["item"]["stickers"][0]["rotation"] = None
    duplicate["watchers"] = 100
    second_stats = parser.save_sales_batch(
        parser_db,
        [duplicate],
        duplicate["item"]["market_hash_name"],
        observed_at=second_seen_at,
    )

    sale_row = parser_db.execute("""
        SELECT name, price_cents, sold_at, fetched_at,
               first_seen_at, last_seen_at, seen_count
        FROM sales WHERE listing_id = 'sale-1'
    """).fetchone()
    sticker_row = parser_db.execute("""
        SELECT rotation, steam_market_url
        FROM sale_stickers WHERE listing_id = 'sale-1' AND sticker_index = 0
    """).fetchone()

    assert first_stats.new_count == 1
    assert second_stats.overlap_count == 1
    assert second_stats.updated_count == 1
    assert dict(sale_row) == {
        "name": "AK-47 | Test (Field-Tested)",
        "price_cents": 12_500,
        "sold_at": "2026-08-01T12:00:00Z",
        "fetched_at": first_seen_at,
        "first_seen_at": first_seen_at,
        "last_seen_at": second_seen_at,
        "seen_count": 2,
    }
    assert sticker_row["rotation"] == 17.5
    assert sticker_row["steam_market_url"].endswith("/test-sticker")


def test_sale_normalizes_keychains_rotation_and_reference_timestamps(parser_db):
    sale = make_sale()

    result = parser.save_sale(
        parser_db,
        sale,
        observed_at="2026-08-01T12:05:00+00:00",
    )

    sale_row = parser_db.execute("""
        SELECT reference_keychain_price, reference_last_updated
        FROM sales WHERE listing_id = 'sale-1'
    """).fetchone()
    sticker_row = parser_db.execute("""
        SELECT rotation, reference_updated_at, steam_market_url
        FROM sale_stickers WHERE listing_id = 'sale-1'
    """).fetchone()
    keychain_row = parser_db.execute("""
        SELECT keychain_id, pattern, offset_x, offset_y, offset_z,
               price_cents, reference_quantity, reference_updated_at,
               steam_market_url
        FROM sale_keychains WHERE listing_id = 'sale-1'
    """).fetchone()

    assert result == "new"
    assert tuple(sale_row) == (222, "2026-08-01T10:00:00Z")
    assert tuple(sticker_row) == (
        17.5,
        "2026-08-01T09:00:00Z",
        "https://steamcommunity.com/market/listings/730/test-sticker",
    )
    assert tuple(keychain_row) == (
        501,
        42,
        0.1,
        0.2,
        0.3,
        456,
        5,
        "2026-08-01T08:00:00Z",
        "https://steamcommunity.com/market/listings/730/test-charm",
    )


def test_malformed_sale_is_quarantined_without_aborting_batch(parser_db):
    first = make_sale("valid-1")
    second = make_sale("valid-2")
    malformed = {"id": "broken", "price": 1_000, "sold_at": "not-a-date"}

    stats = parser.save_sales_batch(
        parser_db,
        [first, malformed, second],
        "AK-47 | Test (Field-Tested)",
        observed_at="2026-08-01T12:05:00+00:00",
    )

    assert stats.returned_count == 3
    assert stats.new_count == 2
    assert stats.rejected_count == 1
    assert parser_db.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 2
    quarantine = parser_db.execute("""
        SELECT listing_id, reason, payload_json FROM parser_quarantine
    """).fetchone()
    assert quarantine["listing_id"] == "broken"
    assert "item is missing" in quarantine["reason"]
    assert json.loads(quarantine["payload_json"])["id"] == "broken"


def test_initial_schedule_offsets_are_stable_and_bounded():
    name = "AK-47 | Redline (Field-Tested)"

    first = parser.initial_next_fetch_offset_seconds(0, 123, name)
    second = parser.initial_next_fetch_offset_seconds(0, 123, name)
    another_skin = parser.initial_next_fetch_offset_seconds(
        0,
        123,
        "M4A1-S | Printstream (Field-Tested)",
    )

    assert first == second == 5_020_130
    assert parser.INITIAL_MIN_FETCH_DELAY_SECONDS <= first < parser.MAX_SKIN_INTERVAL_SECONDS
    assert another_skin != first


def test_failure_backoff_preserves_success_counters(parser_db, monkeypatch):
    parser_db.execute("""
        INSERT INTO skin_schedule (
            market_hash_name, skin_order, request_count,
            total_sales_returned, total_new_sales,
            last_sales_count, last_new_sales_count,
            avg_sales_count, avg_new_sales_count,
            interval_seconds, interval_reason,
            last_fetch_at, next_fetch_at, last_success_at,
            attempt_count, consecutive_errors
        ) VALUES (
            'AK-47 | Test (Field-Tested)', 0, 7,
            280, 200,
            40, 20,
            40.0, 20.0,
            3600, 'adaptive_rate',
            1000, 2000, 1000,
            3, 0
        )
    """)
    parser_db.commit()
    monkeypatch.setattr(parser, "unix_now", lambda: 5_000)
    monkeypatch.setattr(parser, "SCHEDULE_JITTER_FRACTION", 0)
    error = parser.SalesFetchError(
        "temporary server error",
        outcome="transient_error",
        http_status=503,
        duration_ms=125,
    )

    first_retry, first_next = parser.record_skin_failure(
        parser_db,
        "AK-47 | Test (Field-Tested)",
        error,
    )
    second_retry, second_next = parser.record_skin_failure(
        parser_db,
        "AK-47 | Test (Field-Tested)",
        error,
    )

    row = parser_db.execute("""
        SELECT request_count, total_sales_returned, total_new_sales,
               last_sales_count, last_new_sales_count,
               avg_sales_count, avg_new_sales_count,
               last_fetch_at, last_success_at,
               attempt_count, consecutive_errors,
               last_outcome, last_http_status, last_duration_ms
        FROM skin_schedule
        WHERE market_hash_name = 'AK-47 | Test (Field-Tested)'
    """).fetchone()
    log_rows = parser_db.execute("""
        SELECT outcome, http_status, sales_count, new_sales_count,
               interval_reason, coverage_status
        FROM parser_request_log ORDER BY id
    """).fetchall()

    assert (first_retry, first_next) == (parser.ERROR_RETRY_BASE_SECONDS, 5_300)
    assert (second_retry, second_next) == (parser.ERROR_RETRY_BASE_SECONDS * 2, 5_600)
    assert tuple(row[:9]) == (7, 280, 200, 40, 20, 40.0, 20.0, 1000, 1000)
    assert tuple(row[9:]) == (5, 2, "transient_error", 503, 125)
    assert [tuple(log_row) for log_row in log_rows] == [
        ("transient_error", 503, 0, 0, "error_backoff", "error"),
        ("transient_error", 503, 0, 0, "error_backoff", "error"),
    ]


def test_run_scheduler_success_commits_sale_schedule_log_and_progress(
    parser_db,
    monkeypatch,
):
    skins, state_updates = prepare_scheduler_test(monkeypatch, parser_db)
    sale = make_sale("scheduler-success")
    sale["item"]["market_hash_name"] = SCHEDULER_SKIN
    window = parser.SalesWindow(
        sales=[sale],
        page_count=1,
        page_sizes=[1],
        http_status=200,
        duration_ms=42,
    )
    fetches = []
    transactional_writes = []

    def fake_fetch(conn, market_hash_name, *, paint_index=None):
        assert conn is parser_db
        fetches.append((market_hash_name, paint_index))
        return window

    monkeypatch.setattr(parser, "fetch_sales_window", fake_fetch)
    for function_name in (
        "save_sales_batch",
        "update_skin_schedule",
        "save_request_log",
        "save_progress",
    ):
        original = getattr(parser, function_name)

        def assert_transaction(*args, _name=function_name, _original=original, **kwargs):
            assert args[0] is parser_db
            assert args[0].in_transaction
            transactional_writes.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(parser, function_name, assert_transaction)

    processed = parser.run_scheduler(parser_db, skins, max_requests=1)

    sale_row = parser_db.execute("""
        SELECT listing_id, name, price_cents FROM sales
        WHERE listing_id = 'scheduler-success'
    """).fetchone()
    schedule = parser_db.execute("""
        SELECT request_count, total_sales_returned, total_new_sales,
               last_sales_count, last_new_sales_count,
               attempt_count, consecutive_errors, last_outcome,
               last_http_status, last_duration_ms, coverage_status
        FROM skin_schedule WHERE market_hash_name = ?
    """, (SCHEDULER_SKIN,)).fetchone()
    request_log = parser_db.execute("""
        SELECT market_hash_name, sales_count, new_sales_count,
               outcome, http_status, duration_ms, page_count,
               rejected_sales_count, coverage_status
        FROM parser_request_log
    """).fetchone()
    progress = parser_db.execute("""
        SELECT skin_index, market_hash_name FROM progress WHERE id = ?
    """, (parser.PROGRESS_ID,)).fetchone()

    assert processed == 1
    assert fetches == [(SCHEDULER_SKIN, 7)]
    assert transactional_writes == [
        "save_sales_batch",
        "update_skin_schedule",
        "save_request_log",
        "save_progress",
    ]
    assert tuple(sale_row) == ("scheduler-success", SCHEDULER_SKIN, 12_500)
    assert tuple(schedule) == (2, 1, 1, 1, 1, 2, 0, "success", 200, 42, "complete")
    assert tuple(request_log) == (
        SCHEDULER_SKIN,
        1,
        1,
        "success",
        200,
        42,
        1,
        0,
        "complete",
    )
    assert tuple(progress) == (1, SCHEDULER_SKIN)
    assert state_updates[-1]["last_outcome"] == "success"


def test_run_scheduler_fetch_error_records_backoff_without_success_counts(
    parser_db,
    monkeypatch,
):
    skins, state_updates = prepare_scheduler_test(monkeypatch, parser_db)
    parser_db.execute("""
        UPDATE skin_schedule
        SET request_count = 7,
            total_sales_returned = 280,
            total_new_sales = 200,
            last_sales_count = 40,
            last_new_sales_count = 20,
            avg_sales_count = 40,
            avg_new_sales_count = 20,
            attempt_count = 3
        WHERE market_hash_name = ?
    """, (SCHEDULER_SKIN,))
    parser_db.commit()

    def fail_fetch(*_args, **_kwargs):
        raise parser.SalesFetchError(
            "offline 503",
            outcome="transient_error",
            http_status=503,
            duration_ms=19,
        )

    monkeypatch.setattr(parser, "fetch_sales_window", fail_fetch)

    processed = parser.run_scheduler(parser_db, skins, max_requests=1)

    schedule = parser_db.execute("""
        SELECT request_count, total_sales_returned, total_new_sales,
               last_sales_count, last_new_sales_count,
               avg_sales_count, avg_new_sales_count,
               last_fetch_at, last_success_at,
               attempt_count, consecutive_errors, last_outcome,
               last_http_status, last_duration_ms, next_fetch_at
        FROM skin_schedule WHERE market_hash_name = ?
    """, (SCHEDULER_SKIN,)).fetchone()
    request_log = parser_db.execute("""
        SELECT sales_count, new_sales_count, outcome, http_status,
               interval_reason, coverage_status, error
        FROM parser_request_log
    """).fetchone()
    progress = parser_db.execute("""
        SELECT skin_index, market_hash_name FROM progress WHERE id = ?
    """, (parser.PROGRESS_ID,)).fetchone()

    assert processed == 1
    assert tuple(schedule[:9]) == (7, 280, 200, 40, 20, 40.0, 20.0, 9000, 9000)
    assert tuple(schedule[9:14]) == (4, 1, "transient_error", 503, 19)
    assert schedule["next_fetch_at"] == SCHEDULER_NOW + parser.ERROR_RETRY_BASE_SECONDS
    assert tuple(request_log[:6]) == (0, 0, "transient_error", 503, "error_backoff", "error")
    assert request_log["error"] == "offline 503"
    assert tuple(progress) == (1, SCHEDULER_SKIN)
    assert state_updates[-1]["last_outcome"] == "transient_error"
    assert parser_db.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 0


def test_run_scheduler_all_rejected_records_invalid_payload_and_quarantine(
    parser_db,
    monkeypatch,
):
    skins, state_updates = prepare_scheduler_test(monkeypatch, parser_db)
    window = parser.SalesWindow(
        sales=[
            {"id": "broken-1", "price": 1000, "sold_at": "not-a-date"},
            "not-an-object",
        ],
        page_count=1,
        page_sizes=[2],
        http_status=200,
        duration_ms=11,
    )
    monkeypatch.setattr(parser, "fetch_sales_window", lambda *_args, **_kwargs: window)

    processed = parser.run_scheduler(parser_db, skins, max_requests=1)

    schedule = parser_db.execute("""
        SELECT request_count, total_sales_returned, total_new_sales,
               attempt_count, consecutive_errors, last_outcome,
               last_http_status, last_success_at
        FROM skin_schedule WHERE market_hash_name = ?
    """, (SCHEDULER_SKIN,)).fetchone()
    request_log = parser_db.execute("""
        SELECT sales_count, new_sales_count, rejected_sales_count,
               page_count, outcome, http_status, coverage_status
        FROM parser_request_log
    """).fetchone()
    quarantine = parser_db.execute("""
        SELECT listing_id, reason FROM parser_quarantine ORDER BY id
    """).fetchall()

    assert processed == 1
    assert tuple(schedule) == (1, 0, 0, 2, 1, "invalid_payload", 200, 9000)
    assert tuple(request_log) == (2, 0, 2, 1, "invalid_payload", 200, "error")
    assert len(quarantine) == 2
    assert quarantine[0]["listing_id"] == "broken-1"
    assert "item is missing" in quarantine[0]["reason"]
    assert quarantine[1]["listing_id"] is None
    assert "sale must be an object" in quarantine[1]["reason"]
    assert parser_db.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 0
    assert state_updates[-1]["last_outcome"] == "invalid_payload"
    assert state_updates[-1]["last_coverage_status"] == "invalid_payload"


def test_once_with_no_due_skin_exits_without_fetch(parser_db, monkeypatch):
    skins, state_updates = prepare_scheduler_test(monkeypatch, parser_db, due=False)
    calls = []

    def forbidden_fetch(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("fetch_sales_window must not run when no skin is due")

    monkeypatch.setattr(parser, "fetch_sales_window", forbidden_fetch)
    args = parser.parse_args(["--once"])

    processed = parser.run_scheduler(
        parser_db,
        skins,
        max_requests=1 if args.once else None,
    )

    assert processed == 0
    assert calls == []
    assert state_updates == []
    assert parser_db.execute("SELECT COUNT(*) FROM parser_request_log").fetchone()[0] == 0
    assert parser_db.execute("SELECT COUNT(*) FROM progress").fetchone()[0] == 0


def test_cursor_404_after_first_page_is_cursor_error(parser_db, monkeypatch):
    calls = []
    pages = iter([
        parser.SalesPage(
            sales=[{"id": "first-page-sale"}],
            cursor="continuation-token",
            http_status=200,
            duration_ms=7,
        ),
        parser.SalesPage(
            sales=[],
            http_status=404,
            duration_ms=11,
        ),
    ])

    def fake_request(market_hash_name, *, cursor=None, paint_index=None):
        calls.append((market_hash_name, cursor, paint_index))
        return next(pages)

    monkeypatch.setattr(parser, "request_sales", fake_request)

    with pytest.raises(parser.SalesFetchError) as error:
        parser.fetch_sales_window(parser_db, SCHEDULER_SKIN)

    assert error.value.outcome == "cursor_error"
    assert error.value.http_status == 404
    assert error.value.duration_ms == 18
    assert calls == [
        (SCHEDULER_SKIN, None, None),
        (SCHEDULER_SKIN, "continuation-token", None),
    ]


def test_legacy_recovery_marker_waits_for_missing_schedule(parser_db, monkeypatch):
    marker = "parser_v2_schedule_recovery"
    monkeypatch.setattr(parser, "unix_now", lambda: SCHEDULER_NOW)
    monkeypatch.setattr(parser, "iso_now", lambda: "2026-08-10T10:00:00+00:00")
    monkeypatch.setattr(parser, "SCHEDULE_JITTER_FRACTION", 0)
    parser.save_request_log(
        parser_db,
        SCHEDULER_SKIN,
        parser.SALES_PAGE_SIZE,
        parser.SALES_PAGE_SIZE,
        86_400,
        "legacy",
        SCHEDULER_NOW + 86_400,
        outcome="success",
    )
    parser_db.commit()

    first_result = parser.migrate_legacy_schedule_v2(parser_db)

    assert first_result == 0
    assert parser.get_parser_meta(parser_db, marker) is None

    parser_db.execute("""
        INSERT INTO skin_schedule (
            market_hash_name, skin_order, interval_seconds,
            interval_reason, next_fetch_at, updated_at
        ) VALUES (?, 3, 86400, 'legacy', ?, ?)
    """, (
        SCHEDULER_SKIN,
        SCHEDULER_NOW + 86_400,
        "2026-08-10T10:00:00+00:00",
    ))
    parser_db.commit()

    second_result = parser.migrate_legacy_schedule_v2(parser_db)

    schedule = parser_db.execute("""
        SELECT interval_seconds, interval_reason, next_fetch_at,
               coverage_status, saturation_streak
        FROM skin_schedule WHERE market_hash_name = ?
    """, (SCHEDULER_SKIN,)).fetchone()
    metadata = json.loads(parser.get_parser_meta(parser_db, marker))
    assert second_result == 1
    assert schedule["interval_seconds"] == 43_200
    assert schedule["interval_reason"] == "saturated_catchup"
    assert SCHEDULER_NOW <= schedule["next_fetch_at"] <= SCHEDULER_NOW + 6 * 3600
    assert schedule["coverage_status"] == "coverage_uncertain"
    assert schedule["saturation_streak"] == 1
    assert metadata["capped_skins"] == 1
    assert metadata["fully_new_skins"] == 1


def test_get_current_key_reselects_route_after_cooldown_sleep(monkeypatch):
    clock = [100]
    sleeps = []
    routes = [
        {"path": "route-a", "proxy_label": "direct", "blocked_until": 200},
        {"path": "route-b", "proxy_label": "direct", "blocked_until": 150},
    ]
    monkeypatch.setattr(parser, "API_KEYS", routes)
    monkeypatch.setattr(parser, "CURRENT_KEY_INDEX", 0)
    monkeypatch.setattr(parser.time, "time", lambda: clock[0])
    monkeypatch.setattr(parser, "write_parser_status", lambda: None)

    def fake_sleep(seconds, reason):
        sleeps.append((seconds, reason))
        clock[0] += seconds

    monkeypatch.setattr(parser, "sleep_with_message", fake_sleep)

    selected = parser.get_current_key()

    assert selected is routes[1]
    assert parser.CURRENT_KEY_INDEX == 1
    assert sleeps == [(50, "All CSFloat routes are cooling down")]


def test_main_with_occupied_instance_lock_does_not_overwrite_status(
    tmp_path,
    monkeypatch,
):
    status_updates = []
    monkeypatch.setattr(parser, "LOCK_PATH", tmp_path / "parser.lock")
    monkeypatch.setattr(
        parser,
        "update_parser_state",
        lambda **values: status_updates.append(values),
    )
    monkeypatch.setattr(
        parser,
        "load_skins",
        lambda: pytest.fail("load_skins must not run without lock ownership"),
    )

    with (
        parser.parser_instance_lock(),
        pytest.raises(RuntimeError, match="already running"),
    ):
        parser.main(["--migrate-only"])

    assert status_updates == []


def test_stale_relative_rate_reset_enforces_minimum_single_route_cooldown(
    monkeypatch,
):
    route = {
        "path": "only-route",
        "proxy_label": "direct",
        "blocked_until": 0,
    }
    monkeypatch.setattr(parser, "API_KEYS", [route])
    monkeypatch.setattr(parser, "CURRENT_KEY_INDEX", 0)
    monkeypatch.setattr(parser.time, "time", lambda: 1000)
    monkeypatch.setattr(parser, "write_parser_status", lambda: None)

    parser.mark_current_key_limited("1", body="rate limited")

    assert route["blocked_until"] >= 1000 + parser.MIN_RATE_LIMIT_BACKOFF_SECONDS
    assert parser.seconds_until_any_key_available() >= parser.MIN_RATE_LIMIT_BACKOFF_SECONDS
    assert parser.switch_to_available_key() is False
    assert parser.CURRENT_KEY_INDEX == 0


class FakeRequestException(Exception):
    pass


class FakeRequestsModule:
    class exceptions:
        RequestException = FakeRequestException


class FakeResponse:
    def __init__(self, status_code, body, headers=None):
        self.status_code = status_code
        self.text = body
        self.headers = headers or {}


def make_requests_route(path, session, *, rate_remaining=321):
    return {
        "path": path,
        "value": f"secret-{path}",
        "proxy": f"socks5://user:password@{path}.invalid:1080",
        "proxy_label": f"socks5://***@{path}.invalid:1080",
        "client": {
            "type": "requests",
            "session": session,
            "requests": FakeRequestsModule,
        },
        "blocked_until": 0,
        "blocked_reason": None,
        "transport_blocked_until": 0,
        "transport_failure_count": 0,
        "transport_last_error": None,
        "transport_last_error_at": None,
        "rate_limit": 500,
        "rate_remaining": rate_remaining,
        "rate_reset": 50_000,
        "last_http_status": 200,
        "last_seen_at": None,
    }


def test_proxy_timeouts_pause_transport_not_api_keys_and_fail_over_once(
    tmp_path,
    monkeypatch,
):
    calls = []

    class FailingSession:
        def __init__(self, route_name):
            self.route_name = route_name

        def get(self, _url, *, headers, timeout):
            assert headers["Authorization"] == f"secret-{self.route_name}"
            assert timeout == (
                parser.CONNECT_TIMEOUT_SECONDS,
                parser.READ_TIMEOUT_SECONDS,
            )
            calls.append(self.route_name)
            raise FakeRequestException("SOCKS connect timed out")

    routes = [
        make_requests_route("route-a", FailingSession("route-a")),
        make_requests_route("route-b", FailingSession("route-b")),
    ]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(parser, "API_KEYS", routes)
    monkeypatch.setattr(parser, "CURRENT_KEY_INDEX", 0)
    monkeypatch.setattr(parser.time, "time", lambda: 10_000)
    monkeypatch.setattr(parser, "write_parser_status", lambda: None)
    monkeypatch.setattr(
        parser,
        "sleep_with_message",
        lambda *_args, **_kwargs: pytest.fail(
            "one request must not sleep and retry failed transports forever"
        ),
    )

    with pytest.raises(parser.SalesFetchError) as raised:
        parser.request_sales("AK-47 | Transport Test (Field-Tested)")

    assert raised.value.outcome == "transport_error"
    assert calls == ["route-a", "route-b"]
    assert all(route["blocked_until"] == 0 for route in routes)
    assert all(route["blocked_reason"] is None for route in routes)
    assert [route["rate_remaining"] for route in routes] == [321, 321]
    assert all(route["transport_blocked_until"] == 10_030 for route in routes)
    assert all(route["transport_failure_count"] == 1 for route in routes)
    assert all(route["last_http_status"] == "proxy_error" for route in routes)


def test_item_level_403_never_blocks_or_rotates_api_key(tmp_path, monkeypatch):
    calls = []

    class ForbiddenSession:
        def __init__(self, route_name):
            self.route_name = route_name

        def get(self, _url, *, headers, timeout):
            calls.append(self.route_name)
            return FakeResponse(403, '{"error":"Forbidden for this item"}')

    routes = [
        make_requests_route("route-a", ForbiddenSession("route-a")),
        make_requests_route("route-b", ForbiddenSession("route-b")),
    ]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(parser, "API_KEYS", routes)
    monkeypatch.setattr(parser, "write_parser_status", lambda: None)

    for route_index in range(len(routes)):
        monkeypatch.setattr(parser, "CURRENT_KEY_INDEX", route_index)
        with pytest.raises(parser.SalesFetchError) as raised:
            parser.request_sales("Souvenir Negev | Man-o'-war (Field-Tested)")
        assert raised.value.outcome == "forbidden_item"
        assert raised.value.http_status == 403

    assert calls == ["route-a", "route-b"]
    assert all(route["blocked_until"] == 0 for route in routes)
    assert all(route["blocked_reason"] is None for route in routes)
    assert all(route["rate_remaining"] is None for route in routes)


@pytest.mark.parametrize(
    ("status_code", "body"),
    [
        (401, '{"error":"Unauthorized"}'),
        (403, '{"error":"Invalid API key"}'),
    ],
)
def test_explicit_auth_response_still_blocks_key(
    status_code,
    body,
    tmp_path,
    monkeypatch,
):
    class AuthRejectedSession:
        def get(self, _url, *, headers, timeout):
            return FakeResponse(status_code, body)

    route = make_requests_route("route-a", AuthRejectedSession())
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(parser, "API_KEYS", [route])
    monkeypatch.setattr(parser, "CURRENT_KEY_INDEX", 0)
    monkeypatch.setattr(parser.time, "time", lambda: 10_000)
    monkeypatch.setattr(parser, "write_parser_status", lambda: None)

    with pytest.raises(parser.SalesFetchError) as raised:
        parser.request_sales("AK-47 | Auth Test (Field-Tested)")

    assert raised.value.outcome == "auth_error"
    assert route["blocked_until"] == 10_000 + 24 * 60 * 60
    assert route["blocked_reason"] == "authentication"
    assert route["transport_blocked_until"] == 0


def test_legacy_proxy_error_cooldown_restores_as_transport_only(
    tmp_path,
    monkeypatch,
):
    status_path = tmp_path / "parser_status.json"
    status_path.write_text(
        json.dumps({
            "status_version": 2,
            "keys": [{
                "path": "route-a",
                "proxy": "socks5://***@route-a.invalid:1080",
                "blocked_until": "1970-01-01T03:03:20+00:00",
                "last_http_status": "proxy_error",
                "rate_remaining": 321,
            }],
        }),
        encoding="utf-8",
    )
    route = make_requests_route("route-a", object())
    monkeypatch.setattr(parser, "STATUS_PATH", str(status_path))
    monkeypatch.setattr(parser, "unix_now", lambda: 10_000)

    # Use a future ISO timestamp relative to the test clock.
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    payload["keys"][0]["blocked_until"] = parser.unix_to_iso(10_100)
    status_path.write_text(json.dumps(payload), encoding="utf-8")
    restored = parser.restore_key_cooldowns([route])[0]

    assert restored["blocked_until"] == 0
    assert restored["transport_blocked_until"] == 10_100
    assert restored["transport_failure_count"] == 1


@pytest.mark.parametrize("rate_remaining", [321, 0, None])
def test_legacy_ambiguous_403_cooldown_is_not_restored(
    tmp_path, monkeypatch, rate_remaining
):
    status_path = tmp_path / "parser_status.json"
    status_path.write_text(
        json.dumps({
            "status_version": 2,
            "keys": [{
                "path": "route-a",
                "proxy": "socks5://***@route-a.invalid:1080",
                "blocked_until": parser.unix_to_iso(10_100),
                "last_http_status": 403,
                "rate_remaining": rate_remaining,
            }],
        }),
        encoding="utf-8",
    )
    route = make_requests_route("route-a", object())
    monkeypatch.setattr(parser, "STATUS_PATH", str(status_path))
    monkeypatch.setattr(parser, "unix_now", lambda: 10_000)

    restored = parser.restore_key_cooldowns([route])[0]

    assert restored["blocked_until"] == 0
    assert restored["transport_blocked_until"] == 0
    assert restored["blocked_reason"] is None


def test_sale_metadata_backfill_populates_legacy_fields_and_is_idempotent(parser_db):
    fetched_at = "2026-07-20T12:34:56+00:00"
    parser_db.execute("""
        INSERT INTO sales (
            listing_id, fetched_at, reference_raw_json
        ) VALUES (?, ?, ?)
    """, (
        "legacy-sale",
        fetched_at,
        json.dumps({
            "last_updated": "2026-07-20T11:00:00Z",
            "keychain_price": 987,
        }),
    ))
    parser_db.commit()

    first = parser.backfill_sale_metadata(parser_db)

    row = parser_db.execute("""
        SELECT first_seen_at, last_seen_at,
               reference_last_updated, reference_keychain_price
        FROM sales WHERE listing_id = 'legacy-sale'
    """).fetchone()
    marker = json.loads(parser.get_parser_meta(parser_db, "parser_v3_sale_metadata"))
    second = parser.backfill_sale_metadata(parser_db)

    assert first == {
        "first_seen": 1,
        "last_seen": 1,
        "reference_updated": 1,
        "keychain_price": 1,
        "already_done": False,
        "rows_updated": 1,
    }
    assert tuple(row) == (
        fetched_at,
        fetched_at,
        "2026-07-20T11:00:00Z",
        987,
    )
    assert marker == first
    assert second == {
        "rows_updated": 0,
        "first_seen": 0,
        "last_seen": 0,
        "reference_updated": 0,
        "keychain_price": 0,
        "already_done": True,
    }


def test_sale_metadata_backfill_tolerates_invalid_reference_json(parser_db):
    fetched_at = "2026-07-21T01:02:03+00:00"
    parser_db.execute("""
        INSERT INTO sales (
            listing_id, fetched_at, reference_raw_json
        ) VALUES (?, ?, ?)
    """, ("invalid-reference-sale", fetched_at, "{not-valid-json"))
    parser_db.commit()

    result = parser.backfill_sale_metadata(parser_db)

    row = parser_db.execute("""
        SELECT first_seen_at, last_seen_at,
               reference_last_updated, reference_keychain_price
        FROM sales WHERE listing_id = 'invalid-reference-sale'
    """).fetchone()
    assert result["rows_updated"] == 1
    assert result["first_seen"] == 1
    assert result["last_seen"] == 1
    assert result["reference_updated"] == 0
    assert result["keychain_price"] == 0
    assert result["already_done"] is False
    assert tuple(row) == (fetched_at, fetched_at, None, None)
    assert parser.get_parser_meta(parser_db, "parser_v3_sale_metadata") is not None


@pytest.mark.parametrize(
    "proxy_url",
    [
        "http://user:TOPSECRET@[::1",
        "user:TOPSECRET@",
        "://user:TOPSECRET@host",
    ],
)
def test_mask_proxy_never_echoes_malformed_credentials(proxy_url):
    masked = parser.mask_proxy(proxy_url)

    assert "TOPSECRET" not in masked
    assert masked != proxy_url
    assert masked == "invalid-proxy"
