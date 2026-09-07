import contextlib
import io
import sys
import types
import urllib.error

import tg_deals_bot


def test_long_single_line_is_split_to_telegram_limit():
    chunks = tg_deals_bot.split_telegram_text("x" * 8000, 3900)
    assert "".join(chunks) == "x" * 8000
    assert max(map(len, chunks)) <= 3900


def test_load_deals_uses_keyword_arguments(monkeypatch):
    captured = {}
    fake_dashboard = types.ModuleType("dashboard_server")

    def fake_fetch(**kwargs):
        captured.update(kwargs)
        return {"rows": [], "summary": {}}

    fake_dashboard.fetch_model_deal_comparison_page = fake_fetch
    monkeypatch.setitem(sys.modules, "dashboard_server", fake_dashboard)
    monkeypatch.setattr(tg_deals_bot, "db_connect", lambda: contextlib.nullcontext(object()))
    options = tg_deals_bot.parse_deals_command(
        "/deals 5 igor sort=best_roi seen=2 min=20 max=300"
    )
    tg_deals_bot.load_deals(options)
    assert captured["listing_type"] == "all"
    assert captured["sort_mode"] == "best_roi"
    assert captured["seen_hours"] == 2
    assert captured["created_hours"] == 0
    assert captured["min_price"] == "20"
    assert captured["max_price"] == "300"


def test_failed_update_does_not_block_later_updates(monkeypatch):
    handled = []
    saved_offsets = []

    def fake_handle(message):
        handled.append(message["text"])
        if message["text"] == "bad command":
            raise RuntimeError("broken handler")

    monkeypatch.setattr(tg_deals_bot, "handle_message", fake_handle)
    monkeypatch.setattr(tg_deals_bot, "save_offset", saved_offsets.append)
    payload = {
        "result": [
            {"update_id": 10, "message": {"text": "bad command"}},
            {"update_id": 11, "message": {"text": "good command"}},
        ]
    }

    offset = tg_deals_bot.process_updates(payload, 10)

    assert handled == ["bad command", "good command"]
    assert saved_offsets == [11, 12]
    assert offset == 12


def test_watcher_marks_deal_sent_only_after_success(monkeypatch):
    chat_id = 123
    row = {"listing_id": "listing-1"}
    watcher = {
        "options": {"limit": 1, "seen_hours": 1},
        "interval_seconds": 60,
        "next_at": 0,
        "sent_listing_ids": set(),
    }
    tg_deals_bot.WATCHERS.clear()
    tg_deals_bot.WATCHERS[chat_id] = watcher
    monkeypatch.setattr(
        tg_deals_bot,
        "load_deals",
        lambda _options: {"rows": [row]},
    )
    monkeypatch.setattr(tg_deals_bot, "send_message", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(tg_deals_bot.time, "sleep", lambda _seconds: None)

    def fail_send(*_args, **_kwargs):
        raise RuntimeError("temporary send failure")

    monkeypatch.setattr(tg_deals_bot, "send_deal", fail_send)
    tg_deals_bot.run_watchers()
    assert watcher["sent_listing_ids"] == set()

    watcher["next_at"] = 0
    monkeypatch.setattr(tg_deals_bot, "send_deal", lambda *_args, **_kwargs: True)
    tg_deals_bot.run_watchers()
    assert watcher["sent_listing_ids"] == {"listing-1"}
    tg_deals_bot.WATCHERS.clear()


def test_telegram_http_error_never_exposes_bot_token(monkeypatch):
    token = "123456:secret_bot_token"
    monkeypatch.setattr(tg_deals_bot, "BOT_TOKEN", token)

    def fail_request(request, timeout):
        del timeout
        raise urllib.error.HTTPError(
            request.full_url,
            400,
            "Bad Request",
            {},
            io.BytesIO(b'{"ok":false,"description":"bad chat id"}'),
        )

    monkeypatch.setattr(tg_deals_bot.urllib.request, "urlopen", fail_request)
    try:
        tg_deals_bot.telegram_request("sendMessage", {"chat_id": "bad"})
    except RuntimeError as exc:
        error_text = str(exc)
    else:
        raise AssertionError("telegram_request must reject HTTP errors")

    assert token not in error_text
    assert "bad chat id" in error_text


def test_offset_file_is_written_atomically(tmp_path, monkeypatch):
    offset_path = tmp_path / "offset.txt"
    monkeypatch.setattr(tg_deals_bot, "OFFSET_PATH", offset_path)

    tg_deals_bot.save_offset(42)

    assert tg_deals_bot.load_saved_offset() == 42
    assert list(tmp_path.glob("*.tmp")) == []
