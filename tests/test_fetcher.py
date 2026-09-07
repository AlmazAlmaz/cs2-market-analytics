from urllib.parse import parse_qs, urlparse

import pytest

import csfloat_listing_fetcher as fetcher
import price_model_runtime


@pytest.fixture
def isolated_key_sources(tmp_path, monkeypatch):
    monkeypatch.delenv("CSFLOAT_ROUTES_PATH", raising=False)
    monkeypatch.delenv("CSFLOAT_API_KEY", raising=False)
    monkeypatch.setattr(fetcher, "KEY_PROXY_CONFIG_PATH", tmp_path / "missing-routes.txt")
    monkeypatch.setattr(fetcher, "KEY_PATHS", [])
    return tmp_path


def test_route_loader_respects_configured_path_and_deduplicates_exact_routes(
    isolated_key_sources,
    monkeypatch,
):
    first_key = isolated_key_sources / "first-key"
    duplicate_key = isolated_key_sources / "duplicate-key"
    first_key.write_text("test-route-secret\n", encoding="utf-8")
    duplicate_key.write_text("test-route-secret\n", encoding="utf-8")
    proxy = "socks5://user:proxy-password@127.0.0.1:1080"
    routes = isolated_key_sources / "custom-routes.txt"
    routes.write_text(
        f"{first_key}|{proxy}\n{duplicate_key}|{proxy}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CSFLOAT_ROUTES_PATH", str(routes))

    records = fetcher.load_key_records(strict=True)

    assert len(records) == 1
    assert records[0]["value"] == "test-route-secret"
    assert records[0]["proxy"] == proxy
    assert records[0]["proxy_label"] == "socks5://***@127.0.0.1:1080"
    assert "proxy-password" not in records[0]["proxy_label"]


def test_route_loader_keeps_same_key_on_different_proxies(isolated_key_sources, monkeypatch):
    key_path = isolated_key_sources / "shared-key"
    key_path.write_text("same-test-secret", encoding="utf-8")
    routes = isolated_key_sources / "routes.txt"
    routes.write_text(
        f"{key_path}|socks5://127.0.0.1:1001\n"
        f"{key_path}|socks5://127.0.0.1:1002\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CSFLOAT_ROUTES_PATH", str(routes))

    records = fetcher.load_key_records(strict=True)

    assert [record["proxy"] for record in records] == [
        "socks5://127.0.0.1:1001",
        "socks5://127.0.0.1:1002",
    ]


def test_route_loader_uses_environment_key_before_legacy_files(isolated_key_sources, monkeypatch):
    legacy_key = isolated_key_sources / "key4"
    legacy_key.write_text("legacy-test-secret", encoding="utf-8")
    monkeypatch.setattr(fetcher, "KEY_PATHS", [legacy_key])
    monkeypatch.setenv("CSFLOAT_API_KEY", "environment-test-secret")

    records = fetcher.load_key_records(strict=True)

    assert records == [{
        "path": "CSFLOAT_API_KEY",
        "value": "environment-test-secret",
        "proxy": None,
        "proxy_label": "direct",
    }]


def test_route_loader_supports_and_deduplicates_legacy_keys(isolated_key_sources, monkeypatch):
    first_key = isolated_key_sources / "key4"
    duplicate_key = isolated_key_sources / "key2"
    second_key = isolated_key_sources / "key"
    first_key.write_text("first-test-secret", encoding="utf-8")
    duplicate_key.write_text("first-test-secret", encoding="utf-8")
    second_key.write_text("second-test-secret", encoding="utf-8")
    monkeypatch.setattr(fetcher, "KEY_PATHS", [first_key, duplicate_key, second_key])

    records = fetcher.load_key_records(strict=True)

    assert [(record["path"], record["value"]) for record in records] == [
        ("key4", "first-test-secret"),
        ("key", "second-test-secret"),
    ]


def test_strict_route_loader_fails_safely_on_invalid_entry(isolated_key_sources, monkeypatch):
    routes = isolated_key_sources / "routes.txt"
    routes.write_text(
        "|socks5://user:do-not-leak@127.0.0.1:1080\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CSFLOAT_ROUTES_PATH", str(routes))

    with pytest.raises(fetcher.CsfloatRouteConfigError) as raised:
        fetcher.load_key_records(strict=True)

    error = str(raised.value)
    assert "key file path is empty" in error
    assert "do-not-leak" not in error


def test_lenient_route_loader_skips_invalid_entries(isolated_key_sources, monkeypatch):
    valid_key = isolated_key_sources / "valid-key"
    valid_key.write_text("valid-test-secret", encoding="utf-8")
    routes = isolated_key_sources / "routes.txt"
    routes.write_text(
        f"missing-key|socks5://127.0.0.1:1001\n{valid_key}|\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CSFLOAT_ROUTES_PATH", str(routes))

    records = fetcher.load_key_records(strict=False)

    assert len(records) == 1
    assert records[0]["path"] == "valid-key"
    assert records[0]["proxy"] is None


def test_strict_route_loader_rejects_missing_configured_file(isolated_key_sources, monkeypatch):
    missing_path = isolated_key_sources / "explicitly-missing.txt"
    monkeypatch.setenv("CSFLOAT_ROUTES_PATH", str(missing_path))
    monkeypatch.setenv("CSFLOAT_API_KEY", "fallback-must-not-hide-misconfiguration")

    with pytest.raises(fetcher.CsfloatRouteConfigError, match="routes file not found") as raised:
        fetcher.load_key_records(strict=True)

    assert "fallback-must-not-hide-misconfiguration" not in str(raised.value)


def test_listing_normalization_preserves_zero_values_and_string_collection():
    raw = {
        "id": "12345678",
        "price": 1250,
        "reference": {"base_price": 1000, "predicted_price": 1100, "float_factor": 0},
        "item": {
            "market_hash_name": "AK-47 | Test (Factory New)",
            "float_value": 0,
            "paint_seed": 0,
            "paint_index": 0,
            "collection": "The Test Collection",
            "stickers": [],
        },
    }
    result = fetcher.normalize_listing_for_deal(
        raw,
        "https://csfloat.com/item/12345678",
        {"path": "key", "proxy_label": "direct"},
        {},
    )
    payload = result["deal_payload"]
    assert payload["float_value"] == 0
    assert payload["paint_seed"] == 0
    assert payload["paint_index"] == 0
    assert payload["reference_float_factor"] == 0
    assert payload["item_collection_name"] == "The Test Collection"


@pytest.mark.parametrize("normalizer", [fetcher.normalize_sticker_wear, price_model_runtime.normalize_sticker_wear])
def test_unknown_sticker_wear_is_idempotent(normalizer):
    assert normalizer(-1) == -1
    assert normalizer(normalizer(-1)) == -1
    assert normalizer(0) == 0


def test_direct_api_url_must_use_https():
    with pytest.raises(ValueError, match="Only HTTPS"):
        fetcher.parse_listing_url_or_id("http://csfloat.com/api/v1/listings/12345678")
    assert fetcher.parse_listing_url_or_id("https://csfloat.com/api/v1/listings/12345678").startswith(
        "https://csfloat.com/api/"
    )


def test_rate_limit_headers_are_case_insensitive():
    headers = {"x-ratelimit-remaining": "42"}
    assert fetcher.header_value(headers, "X-Ratelimit-Remaining") == "42"


def test_all_routes_rate_limited_uses_distinct_error(monkeypatch):
    monkeypatch.setattr(
        fetcher,
        "load_key_records",
        lambda: [
            {"path": "key-a", "value": "secret-a", "proxy_label": "direct"},
            {"path": "key-b", "value": "secret-b", "proxy_label": "direct"},
        ],
    )
    monkeypatch.setattr(
        fetcher,
        "request_with_key",
        lambda _url, _record: (
            429,
            {"X-Ratelimit-Remaining": "0"},
            '{"error":"rate limited"}',
        ),
    )

    with pytest.raises(fetcher.CsfloatRateLimitError) as raised:
        fetcher.request_json_with_keys("https://csfloat.com/api/v1/listings")

    assert "secret-a" not in str(raised.value)
    assert "secret-b" not in str(raised.value)


def test_missing_listing_uses_distinct_error(monkeypatch):
    monkeypatch.setattr(
        fetcher,
        "load_key_records",
        lambda: [
            {"path": "key-a", "value": "secret-a", "proxy_label": "direct"},
        ],
    )
    monkeypatch.setattr(
        fetcher,
        "request_with_key",
        lambda _url, _record: (
            404,
            {},
            '{"error":"listing not found"}',
        ),
    )

    with pytest.raises(fetcher.CsfloatNotFoundError) as raised:
        fetcher.request_json_with_keys(
            "https://csfloat.com/api/v1/listings/12345678"
        )

    assert "secret-a" not in str(raised.value)


def test_active_listing_cursor_is_forwarded(monkeypatch):
    captured = {}

    def fake_request(url):
        captured["url"] = url
        return {"data": [], "cursor": None}, {}

    monkeypatch.setattr(fetcher, "request_json_with_keys", fake_request)
    fetcher.fetch_active_listings_for_sticker(123, cursor="next/page+1")
    assert "cursor=next%2Fpage%2B1" in captured["url"]


def test_skin_listing_fetch_is_exact_bounded_and_cursor_aware(monkeypatch):
    captured = {}

    def fake_request(url):
        captured["query"] = parse_qs(urlparse(url).query)
        return {"data": [{"id": "one"}], "cursor": "next"}, {"rate_remaining": "7"}

    monkeypatch.setattr(fetcher, "request_json_with_keys", fake_request)

    listings, cursor, meta, _url = fetcher.fetch_active_listings_for_skin(
        "AK-47 | Test (Field-Tested)",
        limit=999,
        sort_by="highest_discount",
        cursor="page/2+next",
    )

    assert listings == [{"id": "one"}]
    assert cursor == "next"
    assert meta["rate_remaining"] == "7"
    assert captured["query"] == {
        "limit": [str(fetcher.MAX_ACTIVE_LISTING_LIMIT)],
        "sort_by": ["highest_discount"],
        "market_hash_name": ["AK-47 | Test (Field-Tested)"],
        "max_price": [str(fetcher.CHEAP_SKIN_MAX_PRICE_CENTS)],
        "cursor": ["page/2+next"],
    }


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"sort_by": "unbounded_new_sort"}, "sort_by must be one of"),
        ({"max_price": 1001}, "between 1 and 1000"),
        ({"max_price": 0}, "between 1 and 1000"),
    ],
)
def test_skin_listing_fetch_rejects_unbounded_parameters(monkeypatch, kwargs, message):
    monkeypatch.setattr(
        fetcher,
        "request_json_with_keys",
        lambda _url: pytest.fail("invalid filters must fail before a request"),
    )

    with pytest.raises(ValueError, match=message):
        fetcher.fetch_active_listings_for_skin("AK-47 | Test", **kwargs)


def test_active_listing_type_is_forwarded_and_validated(monkeypatch):
    captured = {}

    def fake_request(url):
        captured["query"] = parse_qs(urlparse(url).query)
        return {"data": [], "cursor": None}, {}

    monkeypatch.setattr(fetcher, "request_json_with_keys", fake_request)

    fetcher.fetch_active_listings(
        market_hash_name="Kilowatt Case",
        listing_type="buy_now",
    )

    assert captured["query"]["type"] == ["buy_now"]
    with pytest.raises(ValueError, match="listing_type must be one of"):
        fetcher.fetch_active_listings(
            market_hash_name="Kilowatt Case",
            listing_type="not-a-real-type",
        )


def test_price_list_is_cached_and_exact_name_lookup_is_safe(monkeypatch):
    fetcher.clear_price_list_cache()
    calls = []

    def fake_request(url):
        calls.append(url)
        return [
            {
                "market_hash_name": "Kilowatt Case",
                "quantity": 15_360,
                "min_price": 15,
            },
            {
                "market_hash_name": "10 Year Birthday Sticker Capsule",
                "quantity": 360,
                "min_price": 60,
            },
        ], {
            "key_file": "do-not-expose-key-file",
            "proxy": "do-not-expose-proxy",
        }

    monkeypatch.setattr(fetcher, "request_json_with_keys", fake_request)

    first = fetcher.fetch_csfloat_price_list()
    second = fetcher.fetch_csfloat_price_list()
    floor = fetcher.lookup_csfloat_price_list_floor("Kilowatt Case")

    assert calls == [fetcher.PRICE_LIST_API_URL]
    assert first == second
    assert first["items"]["Kilowatt Case"] == {
        "current_price_cents": 15,
        "active_quantity": 15_360,
    }
    assert "do-not-expose" not in repr(first)
    assert floor == {
        "market_hash_name": "Kilowatt Case",
        "current_price_cents": 15,
        "active_quantity": 15_360,
        "price_source": fetcher.PRICE_LIST_SOURCE,
        "price_as_of": first["price_as_of"],
    }
    assert fetcher.lookup_csfloat_price_list_floor("kilowatt case") is None

    # A caller cannot poison the cached values returned by the public snapshot.
    first["items"]["Kilowatt Case"]["current_price_cents"] = 1
    assert fetcher.lookup_csfloat_price_list_floor("Kilowatt Case")[
        "current_price_cents"
    ] == 15


def test_price_list_cache_ttl_and_force_refresh(monkeypatch):
    fetcher.clear_price_list_cache()
    clock = [100.0]
    calls = []

    def fake_request(_url):
        calls.append(clock[0])
        return [{
            "market_hash_name": "Kilowatt Case",
            "quantity": 5,
            "min_price": 10 + len(calls),
        }], {}

    monkeypatch.setattr(fetcher, "PRICE_LIST_CACHE_TTL_SECONDS", 10)
    monkeypatch.setattr(fetcher.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(fetcher, "request_json_with_keys", fake_request)

    assert fetcher.fetch_csfloat_price_list()["items"]["Kilowatt Case"][
        "current_price_cents"
    ] == 11
    clock[0] = 109
    assert fetcher.fetch_csfloat_price_list()["items"]["Kilowatt Case"][
        "current_price_cents"
    ] == 11
    assert len(calls) == 1

    clock[0] = 111
    assert fetcher.fetch_csfloat_price_list()["items"]["Kilowatt Case"][
        "current_price_cents"
    ] == 12
    assert len(calls) == 2

    assert fetcher.fetch_csfloat_price_list(force=True)["items"][
        "Kilowatt Case"
    ]["current_price_cents"] == 13
    assert len(calls) == 3


@pytest.mark.parametrize(
    "payload",
    [
        {"data": []},
        ["not-an-object"],
        [{"market_hash_name": "", "quantity": 1, "min_price": 10}],
        [{"market_hash_name": " Padded Name", "quantity": 1, "min_price": 10}],
        [{"market_hash_name": "Test", "quantity": 1, "min_price": "10"}],
        [{"market_hash_name": "Test", "quantity": 1, "min_price": True}],
        [{"market_hash_name": "Test", "quantity": 0, "min_price": 10}],
        [
            {"market_hash_name": "Test", "quantity": 1, "min_price": 10},
            {"market_hash_name": "Test", "quantity": 2, "min_price": 11},
        ],
    ],
)
def test_price_list_rejects_invalid_payload_without_caching(monkeypatch, payload):
    fetcher.clear_price_list_cache()
    calls = []

    def fake_request(_url):
        calls.append(1)
        return payload, {}

    monkeypatch.setattr(fetcher, "request_json_with_keys", fake_request)

    with pytest.raises(fetcher.CsfloatFetchError):
        fetcher.fetch_csfloat_price_list()
    with pytest.raises(fetcher.CsfloatFetchError):
        fetcher.fetch_csfloat_price_list()

    assert len(calls) == 2


def test_live_floor_fallback_is_exact_buy_now_and_has_no_ten_dollar_cap(
    monkeypatch,
):
    captured = {}

    def fake_request(url):
        captured["query"] = parse_qs(urlparse(url).query)
        return {
            "data": [{
                "id": "listing-1",
                "type": "buy_now",
                "state": "listed",
                "price": 10_403,
                "item": {
                    "market_hash_name": "AWP | Asiimov (Field-Tested)",
                },
            }],
            "cursor": "ignored",
        }, {}

    monkeypatch.setattr(fetcher, "request_json_with_keys", fake_request)

    result = fetcher.fetch_lowest_active_buy_now_floor(
        "AWP | Asiimov (Field-Tested)"
    )

    assert captured["query"] == {
        "limit": ["1"],
        "sort_by": ["lowest_price"],
        "market_hash_name": ["AWP | Asiimov (Field-Tested)"],
        "type": ["buy_now"],
    }
    assert result["current_price_cents"] == 10_403
    assert result["active_quantity"] is None
    assert result["price_source"] == fetcher.ACTIVE_BUY_NOW_SOURCE


@pytest.mark.parametrize(
    "listing",
    [
        {
            "type": "auction",
            "state": "listed",
            "price": 10,
            "item": {"market_hash_name": "Kilowatt Case"},
        },
        {
            "type": "buy_now",
            "state": "sold",
            "price": 15,
            "item": {"market_hash_name": "Kilowatt Case"},
        },
        {
            "type": "buy_now",
            "state": "listed",
            "price": 15,
            "item": {"market_hash_name": "Different Case"},
        },
        {
            "type": "buy_now",
            "state": "listed",
            "price": "15",
            "item": {"market_hash_name": "Kilowatt Case"},
        },
    ],
)
def test_live_floor_rejects_misleading_listing(monkeypatch, listing):
    monkeypatch.setattr(
        fetcher,
        "request_json_with_keys",
        lambda _url: ({"data": [listing], "cursor": None}, {}),
    )

    with pytest.raises(fetcher.CsfloatFetchError):
        fetcher.fetch_lowest_active_buy_now_floor("Kilowatt Case")


def test_floor_uses_bounded_fallback_only_when_allowed(monkeypatch):
    calls = []
    monkeypatch.setattr(
        fetcher,
        "lookup_csfloat_price_list_floor",
        lambda _name: None,
    )
    monkeypatch.setattr(
        fetcher,
        "fetch_lowest_active_buy_now_floor",
        lambda name: calls.append(name) or {"market_hash_name": name},
    )

    assert fetcher.fetch_csfloat_floor("Kilowatt Case", allow_fallback=False) is None
    assert calls == []
    assert fetcher.fetch_csfloat_floor("Kilowatt Case") == {
        "market_hash_name": "Kilowatt Case"
    }
    assert calls == ["Kilowatt Case"]


def test_floor_falls_back_on_price_list_fetch_error_but_can_surface_it(monkeypatch):
    error = fetcher.CsfloatFetchError("price-list unavailable")
    monkeypatch.setattr(
        fetcher,
        "lookup_csfloat_price_list_floor",
        lambda _name: (_ for _ in ()).throw(error),
    )
    monkeypatch.setattr(
        fetcher,
        "fetch_lowest_active_buy_now_floor",
        lambda name: {
            "market_hash_name": name,
            "current_price_cents": 15,
        },
    )

    assert fetcher.fetch_csfloat_floor("Kilowatt Case")["current_price_cents"] == 15
    with pytest.raises(fetcher.CsfloatFetchError, match="price-list unavailable"):
        fetcher.fetch_csfloat_floor("Kilowatt Case", allow_fallback=False)
