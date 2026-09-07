#!/usr/bin/env python3

import json
import sqlite3
from pathlib import Path

import app_config
import deal_intelligence
import deal_scanner
import igor_model_runtime
import neural_model_runtime
import price_model_runtime
from item_classification import ITEM_KIND_WEAPON_SKIN, classify_market_hash_name

DB_PATH = str(app_config.path("CS2_DB_PATH", "csfloat_history.db"))


def connect_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def main():
    if not Path(DB_PATH).exists():
        raise FileNotFoundError(f"DB not found: {DB_PATH}")

    updated = 0
    failed = 0
    verdict_counts = {}
    model_verdict_counts = {}
    models = {
        "almaz": price_model_runtime.predict_deal,
        "igor": igor_model_runtime.predict_deal,
        "nova": neural_model_runtime.predict_deal,
    }
    config = deal_scanner.load_config()

    with connect_db() as conn:
        deal_scanner.init_db(conn)
        rows = conn.execute("""
            SELECT
                listing_id,
                source_sticker_id,
                source_sticker_name,
                skin_name,
                key_file,
                proxy,
                rate_remaining,
                last_seen_at,
                deal_payload_json
            FROM model_deal_candidates
            WHERE deal_payload_json IS NOT NULL
            ORDER BY last_seen_at DESC
        """).fetchall()

        print(f"Loaded {len(rows)} saved deal candidates")
        historical_profile = deal_intelligence.load_history_profile(conn)

        for row in rows:
            listing_id = row["listing_id"]
            conn.execute("SAVEPOINT reprice_candidate")
            try:
                payload = json.loads(row["deal_payload_json"])
                source_sticker = {
                    "sticker_id": row["source_sticker_id"],
                    "name": row["source_sticker_name"],
                }
                listing_data = {
                    "listing_id": listing_id,
                    "deal_payload": payload,
                }
                meta = {
                    "key_file": row["key_file"],
                    "proxy": row["proxy"],
                    "rate_remaining": row["rate_remaining"],
                }

                item_kind = classify_market_hash_name(payload.get("name"), conn=conn)
                if item_kind != ITEM_KIND_WEAPON_SKIN:
                    for model_name in models:
                        deal_scanner.invalidate_model_signal(
                            conn, listing_id, model_name
                        )
                    conn.execute(
                        "DELETE FROM model_deal_candidates WHERE listing_id = ?",
                        (listing_id,),
                    )
                    deal_intelligence.refresh_listing_intelligence(
                        conn,
                        listing_id,
                        profile=historical_profile,
                    )
                    conn.execute("RELEASE SAVEPOINT reprice_candidate")
                    updated += 1
                    verdict_counts["wrong_item_kind_removed"] = (
                        verdict_counts.get("wrong_item_kind_removed", 0) + 1
                    )
                    print(
                        f"{updated}/{len(rows)} {listing_id} | "
                        f"{row['skin_name']} | removed stale weapon-model signal "
                        f"for {item_kind}"
                    )
                    continue

                prediction = price_model_runtime.predict_deal(payload, conn)
                stickers = payload.get("stickers") or []

                stored_prediction, almaz_eligible = deal_scanner.prediction_for_storage(
                    prediction,
                    config,
                )
                if almaz_eligible:
                    conn.execute("""
                        UPDATE model_deal_candidates
                        SET
                            skin_name = ?,
                            buy_price_cents = ?,
                            predicted_price_cents = ?,
                            low_price_cents = ?,
                            high_price_cents = ?,
                            net_expected_cents = ?,
                            profit_cents = ?,
                            roi_percent = ?,
                            liquidity_score = ?,
                            historical_sales_count = ?,
                            sales_per_day = ?,
                            verdict = ?,
                            pricing_method = ?,
                            sticker_count = ?,
                            sticker_value_cents = ?,
                            float_value = ?,
                            paint_seed = ?,
                            paint_index = ?,
                            prediction_json = ?
                        WHERE listing_id = ?
                    """, (
                        stored_prediction.get("name") or payload.get("name"),
                        stored_prediction.get("buy_price_cents"),
                        stored_prediction.get("predicted_price_cents"),
                        stored_prediction.get("low_price_cents"),
                        stored_prediction.get("high_price_cents"),
                        stored_prediction.get("net_expected_cents"),
                        stored_prediction.get("profit_cents"),
                        stored_prediction.get("roi_percent"),
                        stored_prediction.get("liquidity_score"),
                        stored_prediction.get("historical_sales_count"),
                        stored_prediction.get("sales_per_day"),
                        stored_prediction.get("verdict"),
                        stored_prediction.get("pricing_method"),
                        len(stickers),
                        stored_prediction.get("sticker_value_cents"),
                        payload.get("float_value"),
                        payload.get("paint_seed"),
                        payload.get("paint_index"),
                        json.dumps(stored_prediction, ensure_ascii=False, default=str),
                        listing_id,
                    ))
                else:
                    conn.execute(
                        "DELETE FROM model_deal_candidates WHERE listing_id = ?",
                        (listing_id,),
                    )

                for model_name, predictor in models.items():
                    model_prediction = predictor(payload, conn)
                    stored_model_prediction, _eligible = deal_scanner.prediction_for_storage(
                        model_prediction,
                        config,
                    )
                    deal_scanner.upsert_model_prediction(
                        conn,
                        listing_data,
                        stored_model_prediction,
                        source_sticker,
                        meta,
                        model_name,
                        refresh_observation=False,
                        observed_at=row["last_seen_at"],
                    )
                    model_verdict = stored_model_prediction.get("verdict") or "unknown"
                    model_verdict_counts[model_name] = model_verdict_counts.get(model_name, {})
                    model_verdict_counts[model_name][model_verdict] = (
                        model_verdict_counts[model_name].get(model_verdict, 0) + 1
                    )

                deal_intelligence.refresh_listing_intelligence(
                    conn,
                    listing_id,
                    profile=historical_profile,
                )

            except Exception as exc:  # noqa: BLE001 - isolate independent saved listings
                conn.execute("ROLLBACK TO SAVEPOINT reprice_candidate")
                conn.execute("RELEASE SAVEPOINT reprice_candidate")
                failed += 1
                print(f"{listing_id} | {row['skin_name']} -> error: {exc}")
                continue

            conn.execute("RELEASE SAVEPOINT reprice_candidate")
            updated += 1
            verdict = stored_prediction.get("verdict") or "unknown"
            verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1
            roi = prediction.get("roi_percent")
            roi_text = "unknown" if roi is None else f"{roi:.2f}%"
            print(
                f"{updated}/{len(rows)} {listing_id} | "
                f"{prediction.get('name') or row['skin_name']} | "
                f"{verdict} | ROI {roi_text}"
            )

        conn.commit()

    print(f"Done. Updated: {updated}, failed: {failed}, verdicts: {verdict_counts}, model verdicts: {model_verdict_counts}")


if __name__ == "__main__":
    main()
