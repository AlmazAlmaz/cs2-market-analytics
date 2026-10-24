from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]


def test_nginx_log_format_is_declared_in_http_context():
    config = (PROJECT_DIR / "deploy" / "nginx-cs2.conf.example").read_text(
        encoding="utf-8"
    )

    assert config.index("log_format cs2_safe") < config.index("server {")
    server_body = config[config.index("server {") :]
    assert "log_format cs2_safe" not in server_body
    assert "access_log /var/log/nginx/cs2_access.log cs2_safe;" in server_body


def test_start_all_opens_only_a_verified_public_url_and_supports_quick_tunnel():
    script = (PROJECT_DIR / "start_all.sh").read_text(encoding="utf-8")

    assert 'PUBLIC_SITE_VERIFIED=0' in script
    assert 'if wait_for_public_site; then' in script
    assert 'if [[ "$PUBLIC_SITE_VERIFIED" == "1"' in script
    assert 'QUICK_TUNNEL_VERIFIED=0' in script
    assert 'if wait_for_quick_tunnel; then' in script
    assert 'DASHBOARD_URL="$QUICK_TUNNEL_URL/?token=$DASHBOARD_TOKEN"' in script
    assert "opening the healthy local dashboard instead" in script
    assert 'QUIET_DASHBOARD_URL="${QUIET_DASHBOARD_URL:-1}"' in script
    assert "print(dashboard_server.TOKEN_PATH)" in script
    assert "admin token file: %q" in script
    assert script.index('if [[ "$QUIET_DASHBOARD_URL" == "1" ]]') < script.index(
        'echo "dashboard: $DASHBOARD_URL"'
    )


def test_start_all_activates_only_pending_quality_gated_models_before_services():
    script = (PROJECT_DIR / "start_all.sh").read_text(encoding="utf-8")

    activation = (
        '"$PYTHON_BIN" model_training_activation.py '
        "--activate-pending --offline-confirm"
    )
    assert activation in script
    assert script.index(activation) < script.index("CHECK_CSFLOAT_KEYS=0")
    assert script.index(activation) < script.index("start_service dashboard")
    assert "activation/recovery failed; services were not started" in script
    assert script.index("exit 1", script.index(activation)) < script.index(
        "CHECK_CSFLOAT_KEYS=0"
    )


def test_start_all_treats_vega_as_optional_until_activation_then_strict():
    for name in ("start_all.sh", "start_all.ps1"):
        script = (PROJECT_DIR / name).read_text(encoding="utf-8")

        assert 'read_active_pointer(\n    "vega"' in script
        assert "if vega_pointer is not None:" in script
        assert '"Vega model": vega_dir / "vega_price_model.pt"' in script
        assert "verify_files=True" in script
        assert "vega_model_runtime.load_bundle_from_directory(vega_dir)" in script
        assert "vega_model_runtime.smoke_bundle(vega_bundle)" in script


def test_start_all_holds_one_process_wide_singleton_before_activation():
    script = (PROJECT_DIR / "start_all.sh").read_text(encoding="utf-8")

    acquire = "if ! acquire_startup_singleton; then"
    activation = '"$PYTHON_BIN" model_training_activation.py'
    assert "model_lifecycle.startup_lifetime_lock()" in script
    assert acquire in script
    assert script.index(acquire) < script.index(activation)
    assert "release_startup_singleton" in script


def test_start_all_publishes_a_supervisor_pid_for_safe_external_stop():
    start_script = (PROJECT_DIR / "start_all.sh").read_text(encoding="utf-8")
    stop_script = (PROJECT_DIR / "stop_all.sh").read_text(encoding="utf-8")

    assert 'START_ALL_PID_FILE="$RUN_DIR/start_all.pid"' in start_script
    assert 'printf \'%s\\n\' "$$" > "$START_ALL_PID_FILE"' in start_script
    assert 'kill -TERM "$pid"' in stop_script
    assert "start_all.sh supervisor" in stop_script
    assert "kill -9" not in stop_script


def test_start_all_supervises_the_trend_worker_by_default():
    script = (PROJECT_DIR / "start_all.sh").read_text(encoding="utf-8")

    assert 'START_TREND="${START_TREND:-1}"' in script
    assert 'validate_switch START_TREND "$START_TREND"' in script
    launch = 'start_service trend_worker "$PYTHON_BIN" -u trend_worker.py'
    assert launch in script
    assert 'wait_for_service_stable trend_worker 3' in script
    assert 'CORE_SERVICES="$CORE_SERVICES trend_worker"' in script


def test_market_history_worker_is_optional_and_license_gated():
    script = (PROJECT_DIR / "start_all.sh").read_text(encoding="utf-8")
    config = (PROJECT_DIR / "config.local.env.example").read_text(encoding="utf-8")

    assert 'START_MARKET_HISTORY="${START_MARKET_HISTORY:-auto}"' in script
    assert 'validate_auto_switch START_MARKET_HISTORY "$START_MARKET_HISTORY"' in script
    assert 'app_config.get_bool("CS2_MARKET_HISTORY_ENABLED", False)' in script
    assert '"CS2_MARKET_HISTORY_LICENSE_CONFIRMED", False' in script
    assert 'app_config.read_secret("CSPRICEAPI_KEY")' in script
    assert 'print("no_fetches")' in script
    assert "no provider request was made" in script
    launch = (
        'start_service market_history_worker "$PYTHON_BIN" -u '
        "market_history_worker.py --loop"
    )
    assert launch in script
    assert 'CORE_SERVICES="$CORE_SERVICES market_history_worker"' in script

    assert "CS2_MARKET_HISTORY_ENABLED=false" in config
    assert "CS2_MARKET_HISTORY_LICENSE_CONFIRMED=false" in config
    assert "CS2_MARKET_HISTORY_SALES_CONTRACT_CONFIRMED=false" in config
    assert "CS2_MARKET_HISTORY_SALES_CONTRACT_VERSION=" in config
    assert "CS2_MARKET_HISTORY_SALES_TIMESTAMPS_ARE_UTC=false" in config
    assert "CSPRICEAPI_KEY=" in config
