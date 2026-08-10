# File name: telegram_bot_server.py
# Routes Telegram button events and user text input to feature modules.
import io
import os
import tempfile
import time

import telebot
from telebot.apihelper import ApiTelegramException

import config
import runtime_settings
import app_daily_market_scheduler
import app_runtime_lock
import app_scan_jobs
import core_market_data_store
import core_portfolio_performance
import core_universe_backtest
from app_bot_reports import (
    quick_report,
    run_compiled_scan,
    snapshot_scan_request,
)
from app_bot_session import (
    CUSTOM_GROUP_CODE,
    NEUTRALIZATION_LABELS,
    POSITION_MODE_LABELS,
    STRATEGY_LABELS,
    check_permission,
    clear_direct_input,
    compile_selected_universe,
    get_backtest_report,
    get_or_create_session,
    parse_tickers,
    set_direct_input,
    sync_config_file,
)
from app_bot_ui import (
    admin_text,
    build_admin_backfill_confirm_markup,
    build_back_home_markup,
    build_admin_markup,
    build_home_markup,
    build_neutralization_markup,
    build_position_mode_markup,
    build_settings_markup,
    build_scan_result_markup,
    build_strategy_markup,
    build_universe_markup,
    home_text,
    settings_text,
)


VERSION = runtime_settings.VERSION
BUILD = runtime_settings.BUILD
VERSION_LABEL = runtime_settings.VERSION_LABEL
BOT_TOKEN = runtime_settings.get_setting("BOT_TOKEN", config.BOT_TOKEN)
LOCAL_CONFIG_PATH = runtime_settings.config_path_for_display()
if not BOT_TOKEN:
    raise RuntimeError(
        f"Set BOT_TOKEN in ignored config file {LOCAL_CONFIG_PATH} before starting the bot."
    )

bot = telebot.TeleBot(BOT_TOKEN)
HEAVY_JOB_KEY = "HEAVY_OPERATION"
BACKTEST_EXPORT_JOB_KEY = "BACKTEST_EXPORT"

STAGE_RANGES = {
    "DATA_DOWNLOAD": (1.0, 10.0, "신규 종목 데이터 다운로드"),
    "PREPARING": (1.0, 5.0, "스캔 준비"),
    "DATA_LOAD": (5.0, 10.0, "로컬 데이터 로딩"),
    "CURRENT_SCAN": (10.0, 35.0, "현재 후보 스캔"),
    "IS_BACKTEST": (35.0, 65.0, "IS 백테스트"),
    "OOS_BACKTEST": (65.0, 95.0, "OOS 백테스트"),
    "FINALIZING": (95.0, 99.0, "원장·리포트 생성"),
}


def _elapsed_text(snapshot):
    started_at = snapshot.started_at or snapshot.created_at
    elapsed = max(0, int(time.time() - started_at))
    hours, remainder = divmod(elapsed, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}시간 {minutes:02d}분 {seconds:02d}초"
    return f"{minutes}분 {seconds:02d}초"


def format_job_progress(snapshot):
    progress = max(1, min(100, int(snapshot.progress)))
    filled = min(20, progress // 5)
    bar = "█" * filled + "░" * (20 - filled)
    generic_stages = {
        "Queued": "대기 중",
        "Running": "작업 시작",
        "Completed": "완료",
        "Failed": "실패",
        "Cancelled": "취소",
    }
    stage = generic_stages.get(snapshot.stage, snapshot.stage)
    lines = [
        snapshot.label or "QuantBot 작업",
        "",
        f"{bar} {progress}%",
        "",
        f"단계: {stage}",
    ]
    if snapshot.current is not None and snapshot.total is not None:
        lines.append(f"진행: {snapshot.current} / {snapshot.total}종목")
    lines.append(f"경과시간: {_elapsed_text(snapshot)}")
    return "\n".join(lines)


def _edit_job_progress(chat_id, message_id, snapshot):
    try:
        bot.edit_message_text(
            format_job_progress(snapshot),
            chat_id,
            message_id,
            reply_markup=telebot.types.InlineKeyboardMarkup(),
        )
    except ApiTelegramException as exc:
        if "message is not modified" not in str(exc.description):
            print(f"[job progress edit failed] {type(exc).__name__}: {exc}")
    except Exception as exc:
        print(f"[job progress edit failed] {type(exc).__name__}: {exc}")


def _scan_progress_adapter(progress, cancel_event):
    def report(stage, completed, total, ticker=None, succeeded=True):
        if cancel_event.is_set():
            raise app_scan_jobs.JobCancelled()
        start, end, label = STAGE_RANGES.get(
            stage,
            (5.0, 95.0, str(stage).replace("_", " ").title()),
        )
        ratio = (
            min(max(float(completed) / float(total), 0.0), 1.0)
            if total
            else 0.0
        )
        progress(
            start + (end - start) * ratio,
            label,
            completed,
            total,
        )

    return report


def _download_progress_adapter(progress, cancel_event, stage_label):
    def report(completed, total, ticker):
        if cancel_event.is_set():
            raise app_scan_jobs.JobCancelled()
        ratio = (
            min(max(float(completed) / float(total), 0.0), 1.0)
            if total
            else 0.0
        )
        progress(
            5.0 + 90.0 * ratio,
            stage_label,
            completed,
            total,
        )

    return report


def _benchmark_progress_adapter(progress, cancel_event):
    def report(completed, total, stage_label):
        if cancel_event.is_set():
            raise app_scan_jobs.JobCancelled()
        ratio = (
            min(max(float(completed) / float(total), 0.0), 1.0)
            if total
            else 0.0
        )
        progress(
            5.0 + 90.0 * ratio,
            stage_label,
        )

    return report


def submit_background_operation(
    chat_id,
    message_id,
    *,
    label,
    task_type,
    runner,
    on_result,
    error_markup=None,
    job_key=HEAVY_JOB_KEY,
):
    def on_progress(snapshot):
        _edit_job_progress(chat_id, message_id, snapshot)

    def on_complete(snapshot):
        _edit_job_progress(chat_id, message_id, snapshot)
        try:
            on_result(snapshot.result)
        except Exception as exc:
            bot.edit_message_text(
                f"작업은 완료됐지만 결과 전송에 실패했습니다.\n{type(exc).__name__}: {exc}",
                chat_id,
                message_id,
                reply_markup=error_markup or build_back_home_markup(),
            )

    def on_error(snapshot):
        bot.edit_message_text(
            f"{label}\n\n작업 실패: {snapshot.error or 'Unknown error'}",
            chat_id,
            message_id,
            reply_markup=error_markup or build_back_home_markup(),
        )

    submission = app_scan_jobs.submit_job(
        chat_id,
        job_key,
        runner,
        task_type=task_type,
        label=label,
        metadata={"message_id": int(message_id)},
        on_progress=on_progress,
        on_complete=on_complete,
        on_error=on_error,
    )
    return submission


def submit_ticker_background_operation(
    chat_id,
    message_id,
    *,
    label,
    task_type,
    tickers,
    operation,
    operation_kwargs,
    stage_label,
    format_result,
    result_markup,
):
    frozen_tickers = tuple(tickers)

    def runner(progress, cancel_event):
        progress(5, "작업 준비", 0, len(frozen_tickers))
        kwargs = dict(operation_kwargs)
        kwargs["progress_callback"] = _download_progress_adapter(
            progress,
            cancel_event,
            stage_label,
        )
        return serialized_runtime_call(
            operation,
            frozen_tickers,
            **kwargs,
        )

    def on_result(summary):
        bot.edit_message_text(
            format_result(summary),
            chat_id,
            message_id,
            parse_mode="Markdown",
            reply_markup=result_markup,
        )

    return submit_background_operation(
        chat_id,
        message_id,
        label=label,
        task_type=task_type,
        runner=runner,
        on_result=on_result,
        error_markup=result_markup,
    )


def submit_benchmark_background_operation(chat_id, message_id):
    def runner(progress, cancel_event):
        progress(5, "ACWI 다운로드 준비")
        return serialized_runtime_call(
            core_market_data_store.refresh_market_benchmark,
            progress_callback=_benchmark_progress_adapter(progress, cancel_event),
        )

    def on_result(summary):
        bot.edit_message_text(
            core_market_data_store.format_market_benchmark_refresh_summary(summary),
            chat_id,
            message_id,
            reply_markup=build_admin_markup(),
        )

    return submit_background_operation(
        chat_id,
        message_id,
        label="ACWI Benchmark Refresh",
        task_type="BENCHMARK_REFRESH",
        runner=runner,
        on_result=on_result,
        error_markup=build_admin_markup(),
    )


def serialized_runtime_call(callback, *args, **kwargs):
    with app_runtime_lock.serialized_runtime_operation():
        return callback(*args, **kwargs)


def run_manual_scan(chat_id, progress_callback, request=None):
    return serialized_runtime_call(
        run_compiled_scan,
        chat_id,
        progress_callback=progress_callback,
        return_report_id=True,
        request=request,
    )


def build_quick_reports(tickers):
    def _build():
        reports = []
        for idx, ticker in enumerate(tickers, start=1):
            report = quick_report(ticker)
            if len(tickers) > 1:
                report = f"**[{idx}/{len(tickers)}]**\n" + report
            reports.append(report)
        return reports

    return serialized_runtime_call(_build)


def permission_message(chat_id):
    return (
        "This chat is not allowed for this test bot.\n"
        f"chat_id: `{chat_id}`\n\n"
        f"Add this chat_id to ALLOWED_CHAT_IDS in {LOCAL_CONFIG_PATH}, "
        "then restart the test bot."
    )


def send_long_message(chat_id, text, first_message_id=None, markup=None):
    max_length = 4000
    if len(text) <= max_length and first_message_id:
        bot.edit_message_text(
            text,
            chat_id,
            first_message_id,
            parse_mode="Markdown",
            reply_markup=markup,
        )
        return

    if first_message_id:
        bot.edit_message_text("Scan complete. Sending report...", chat_id, first_message_id)

    chunks = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(line) > max_length:
            if current:
                chunks.append(current.rstrip())
                current = ""
            chunks.extend(
                line[i : i + max_length] for i in range(0, len(line), max_length)
            )
        elif len(current) + len(line) > max_length:
            chunks.append(current.rstrip())
            current = line
        else:
            current += line
    if current:
        chunks.append(current.rstrip())
    for idx, chunk in enumerate(chunks):
        chunk_markup = markup if idx == len(chunks) - 1 else None
        bot.send_message(chat_id, chunk, parse_mode="Markdown", reply_markup=chunk_markup)


def _has_backtest_export(payload):
    return bool(
        payload
        and (payload.get("export_spec") or payload.get("export_path"))
    )


def prepare_backtest_export(payload):
    """Build one self-contained document payload outside Telegram callbacks."""
    export_spec = payload.get("export_spec")
    if export_spec:
        with tempfile.TemporaryDirectory(prefix="quantbot_export_") as directory:
            export_path = core_universe_backtest.materialize_backtest_export(
                export_spec,
                directory,
            )
            with open(export_path, "rb") as export_file:
                return os.path.basename(export_path), export_file.read()

    export_path = payload.get("export_path")
    if not export_path or not os.path.isfile(export_path):
        raise FileNotFoundError("Backtest export is no longer available")
    with open(export_path, "rb") as export_file:
        return os.path.basename(export_path), export_file.read()


def send_prepared_backtest_export(chat_id, prepared_export):
    caption = (
        "Full-universe backtest export "
        "(trades, portfolio ledger, daily NAV, and benchmark)"
    )
    filename, document_bytes = prepared_export
    with io.BytesIO(document_bytes) as export_file:
        export_file.name = filename
        bot.send_document(chat_id, export_file, caption=caption)


def send_backtest_export(chat_id, payload):
    """Compatibility helper for direct, explicit export delivery."""
    send_prepared_backtest_export(chat_id, prepare_backtest_export(payload))


@bot.message_handler(commands=["menu", "start"])
def handle_menu(message):
    chat_id = str(message.chat.id)
    if not check_permission(chat_id):
        bot.reply_to(message, permission_message(chat_id), parse_mode="Markdown")
        return
    session = get_or_create_session(chat_id)
    session["awaiting"] = None
    bot.send_message(
        chat_id,
        home_text(chat_id),
        reply_markup=build_home_markup(),
        parse_mode="Markdown",
    )


@bot.callback_query_handler(func=lambda call: True)
def handle_menu_callbacks(call):
    chat_id = str(call.message.chat.id)
    if not check_permission(chat_id):
        bot.answer_callback_query(call.id, "This chat is not allowed.", show_alert=True)
        return

    session = get_or_create_session(chat_id)
    data = call.data

    try:
        if data == "go_home":
            session["awaiting"] = None
            bot.answer_callback_query(call.id)
            bot.edit_message_text(
                home_text(chat_id),
                chat_id,
                call.message.message_id,
                parse_mode="Markdown",
                reply_markup=build_home_markup(),
            )

        elif data == "settings":
            session["awaiting"] = None
            bot.answer_callback_query(call.id)
            bot.edit_message_text(
                settings_text(chat_id),
                chat_id,
                call.message.message_id,
                parse_mode="Markdown",
                reply_markup=build_settings_markup(chat_id),
            )

        elif data == "admin":
            session["awaiting"] = None
            bot.answer_callback_query(call.id)
            bot.edit_message_text(
                admin_text(),
                chat_id,
                call.message.message_id,
                parse_mode="Markdown",
                reply_markup=build_admin_markup(),
            )

        elif data == "settings_universe":
            bot.answer_callback_query(call.id)
            bot.edit_message_text(
                "**Universe Settings**\n\nSelect one or more groups.",
                chat_id,
                call.message.message_id,
                parse_mode="Markdown",
                reply_markup=build_universe_markup(chat_id),
            )

        elif data == "settings_strategy":
            bot.answer_callback_query(call.id)
            bot.edit_message_text(
                "**Strategy Settings**\n\nChoose the scan engine.",
                chat_id,
                call.message.message_id,
                parse_mode="Markdown",
                reply_markup=build_strategy_markup(chat_id),
            )

        elif data == "settings_neutralize":
            bot.answer_callback_query(call.id)
            bot.edit_message_text(
                "**Neutralization Settings**\n\n"
                "Subtract the selected group mean from today's cross-sectional candidate score.\n"
                "Country uses listing-market groups only: US / CH / JP / KR.\n"
                "Singleton groups become zero; missing labels use a parent/Unclassified group.\n"
                "The same entry-date selection is applied to IS/OOS; exits remain strategy-specific.",
                chat_id,
                call.message.message_id,
                parse_mode="Markdown",
                reply_markup=build_neutralization_markup(chat_id),
            )

        elif data == "settings_position_mode":
            bot.answer_callback_query(call.id)
            bot.edit_message_text(
                "**Position Mode Settings**\n\n"
                "Long Only: allow LONG candidates and trades only.\n"
                "Long / Short Neutral: allow both directions symmetrically.\n"
                "Short Only: allow SHORT candidates and trades only.",
                chat_id,
                call.message.message_id,
                parse_mode="Markdown",
                reply_markup=build_position_mode_markup(chat_id),
            )

        elif data.startswith("toggle_group:"):
            group_code = data.split(":", 1)[1]
            if group_code == CUSTOM_GROUP_CODE and not session.get("custom_tickers"):
                bot.answer_callback_query(
                    call.id, "Set Direct Input first.", show_alert=True
                )
                return

            if group_code in session["selected_groups"]:
                if len(session["selected_groups"]) > 1:
                    session["selected_groups"].remove(group_code)
                    bot.answer_callback_query(call.id, "Removed.")
                else:
                    bot.answer_callback_query(
                        call.id,
                        "At least one universe must stay selected.",
                        show_alert=True,
                    )
                    return
            else:
                session["selected_groups"].add(group_code)
                bot.answer_callback_query(call.id, "Added.")

            bot.edit_message_reply_markup(
                chat_id,
                call.message.message_id,
                reply_markup=build_universe_markup(chat_id),
            )

        elif data == "await_custom_universe":
            session["awaiting"] = "custom_universe"
            bot.answer_callback_query(call.id)
            bot.edit_message_text(
                "Send the tickers to use as Direct Input.\n"
                "Examples: `TSLA NVDA`, `1099 HK, 9983 JP, 600276 SH`",
                chat_id,
                call.message.message_id,
                parse_mode="Markdown",
                reply_markup=build_back_home_markup(),
            )

        elif data == "clear_custom":
            clear_direct_input(session)
            bot.answer_callback_query(call.id, "Direct Input cleared.")
            bot.edit_message_reply_markup(
                chat_id,
                call.message.message_id,
                reply_markup=build_universe_markup(chat_id),
            )

        elif data.startswith("set_strategy:"):
            strategy = data.split(":", 1)[1]
            if strategy in STRATEGY_LABELS:
                session["strategy"] = strategy
            bot.answer_callback_query(call.id, "Strategy updated.")
            bot.edit_message_reply_markup(
                chat_id,
                call.message.message_id,
                reply_markup=build_strategy_markup(chat_id),
            )

        elif data.startswith("set_neutralize:"):
            mode = data.split(":", 1)[1]
            if mode in NEUTRALIZATION_LABELS:
                session["neutralization"] = mode
            bot.answer_callback_query(call.id, "Neutralization updated.")
            bot.edit_message_reply_markup(
                chat_id,
                call.message.message_id,
                reply_markup=build_neutralization_markup(chat_id),
            )

        elif data.startswith("set_position_mode:"):
            mode = data.split(":", 1)[1]
            if mode in POSITION_MODE_LABELS:
                session["position_mode"] = mode
            bot.answer_callback_query(call.id, "Position mode updated.")
            bot.edit_message_reply_markup(
                chat_id,
                call.message.message_id,
                reply_markup=build_position_mode_markup(chat_id),
            )

        elif data == "toggle_scheduler":
            session["scheduler_active"] = not session.get("scheduler_active", True)
            bot.answer_callback_query(
                call.id,
                "Scheduler ON." if session["scheduler_active"] else "Scheduler OFF.",
            )
            bot.edit_message_text(
                settings_text(chat_id),
                chat_id,
                call.message.message_id,
                parse_mode="Markdown",
                reply_markup=build_settings_markup(chat_id),
            )

        elif data == "run_scan":
            request = snapshot_scan_request(chat_id)
            strategy_label = STRATEGY_LABELS.get(
                request.strategy,
                request.strategy,
            )

            def scan_runner(progress, cancel_event):
                return run_manual_scan(
                    chat_id,
                    _scan_progress_adapter(progress, cancel_event),
                    request=request,
                )

            def deliver_scan(result):
                report, report_id = result
                payload = get_backtest_report(session, report_id)
                send_long_message(
                    chat_id,
                    report,
                    first_message_id=call.message.message_id,
                    markup=build_scan_result_markup(
                        has_detail=bool(payload and payload.get("detail")),
                        report_id=report_id,
                        has_export=_has_backtest_export(payload),
                    ),
                )

            submission = submit_background_operation(
                chat_id,
                call.message.message_id,
                label=f"{strategy_label} Scan",
                task_type="STRATEGY_SCAN",
                runner=scan_runner,
                on_result=deliver_scan,
            )
            if submission.created:
                bot.answer_callback_query(call.id, "Background scan started.")
            else:
                bot.answer_callback_query(
                    call.id,
                    f"Another task is already {submission.progress}% complete.",
                    show_alert=True,
                )

        elif data == "scan_detail" or data.startswith("scan_detail:"):
            report_id = data.partition(":")[2] or None
            payload = get_backtest_report(session, report_id)
            if not payload or not payload.get("detail"):
                bot.answer_callback_query(
                    call.id,
                    "This detail has expired. Run the scan again.",
                    show_alert=True,
                )
                return
            detail = payload["detail"]
            bot.answer_callback_query(call.id, "Sending backtest detail.")
            send_long_message(
                chat_id,
                detail,
                markup=build_scan_result_markup(
                    has_detail=False,
                    report_id=report_id,
                    has_export=_has_backtest_export(payload),
                ),
            )

        elif data == "scan_export" or data.startswith("scan_export:"):
            report_id = data.partition(":")[2] or None
            payload = get_backtest_report(session, report_id)
            if not payload or not _has_backtest_export(payload):
                bot.answer_callback_query(
                    call.id,
                    "This export has expired. Run the scan again.",
                    show_alert=True,
                )
                return
            progress_message = bot.send_message(
                chat_id,
                "Backtest Export\n\n대기 중...",
            )

            def export_runner(progress, cancel_event):
                if cancel_event.is_set():
                    raise app_scan_jobs.JobCancelled()
                progress(10, "내보내기 파일 생성")
                prepared_export = prepare_backtest_export(payload)
                if cancel_event.is_set():
                    raise app_scan_jobs.JobCancelled()
                progress(95, "Telegram 전송 준비")
                return prepared_export

            def deliver_export(prepared_export):
                send_prepared_backtest_export(chat_id, prepared_export)
                bot.edit_message_text(
                    "Backtest export sent.",
                    chat_id,
                    progress_message.message_id,
                    reply_markup=build_back_home_markup(),
                )

            submission = submit_background_operation(
                chat_id,
                progress_message.message_id,
                label="Backtest Export",
                task_type="BACKTEST_EXPORT",
                runner=export_runner,
                on_result=deliver_export,
                error_markup=build_back_home_markup(),
                job_key=BACKTEST_EXPORT_JOB_KEY,
            )
            if submission.created:
                bot.answer_callback_query(call.id, "Preparing backtest export.")
            else:
                bot.answer_callback_query(
                    call.id,
                    "Another export is already being prepared.",
                    show_alert=True,
                )
                bot.edit_message_text(
                    "Another backtest export is already being prepared.",
                    chat_id,
                    progress_message.message_id,
                    reply_markup=build_back_home_markup(),
                )

        elif data in {"admin_backfill_all_confirm", "admin_preload_all"}:
            all_tickers = sorted(set(config.universe))
            bot.answer_callback_query(call.id)
            bot.edit_message_text(
                "**Confirm Full 4Y Backfill**\n\n"
                f"Scope: all configured tickers ({len(all_tickers)})\n"
                "This downloads four years for every ticker and may take several minutes. "
                "Use Update All (Latest) for routine daily maintenance.",
                chat_id,
                call.message.message_id,
                parse_mode="Markdown",
                reply_markup=build_admin_backfill_confirm_markup("all"),
            )

        elif data == "admin_backfill_selected_confirm":
            selected = compile_selected_universe(session)
            if not selected:
                bot.answer_callback_query(call.id, "No selected tickers.", show_alert=True)
                return
            bot.answer_callback_query(call.id)
            bot.edit_message_text(
                "**Confirm Selected 4Y Backfill**\n\n"
                f"Scope: selected universe ({len(selected)} tickers)\n"
                "This downloads four years for every selected ticker. "
                "Use Update Selected (Latest) for routine daily maintenance.",
                chat_id,
                call.message.message_id,
                parse_mode="Markdown",
                reply_markup=build_admin_backfill_confirm_markup("selected"),
            )

        elif data == "admin_refresh_all":
            all_tickers = sorted(set(config.universe))
            submission = submit_ticker_background_operation(
                chat_id,
                call.message.message_id,
                label="All-Ticker Latest Update",
                task_type="MARKET_REFRESH",
                tickers=all_tickers,
                operation=core_market_data_store.refresh_latest_price_data,
                operation_kwargs={
                    "min_period": core_market_data_store.FIXED_IS_OOS_HISTORY_PERIOD,
                },
                stage_label="최신 가격 캐시 다운로드",
                format_result=lambda summary: core_market_data_store.format_cache_summary(
                    summary,
                    title="All-Ticker Latest Update Complete",
                ),
                result_markup=build_admin_markup(),
            )
            bot.answer_callback_query(
                call.id,
                "Background update started."
                if submission.created
                else f"Another task is already {submission.progress}% complete.",
                show_alert=not submission.created,
            )

        elif data == "admin_refresh_selected":
            selected = compile_selected_universe(session)
            if not selected:
                bot.answer_callback_query(call.id, "No selected tickers.", show_alert=True)
                return
            submission = submit_ticker_background_operation(
                chat_id,
                call.message.message_id,
                label="Selected-Universe Latest Update",
                task_type="MARKET_REFRESH",
                tickers=selected,
                operation=core_market_data_store.refresh_latest_price_data,
                operation_kwargs={
                    "min_period": core_market_data_store.FIXED_IS_OOS_HISTORY_PERIOD,
                },
                stage_label="최신 가격 캐시 다운로드",
                format_result=lambda summary: core_market_data_store.format_cache_summary(
                    summary,
                    title="Selected-Universe Latest Update Complete",
                ),
                result_markup=build_admin_markup(),
            )
            bot.answer_callback_query(
                call.id,
                "Background update started."
                if submission.created
                else f"Another task is already {submission.progress}% complete.",
                show_alert=not submission.created,
            )

        elif data in {"admin_backfill_selected_run", "admin_backfill_all_run"}:
            if data == "admin_backfill_selected_run":
                target = compile_selected_universe(session)
                scope_label = "selected universe"
            else:
                target = sorted(set(config.universe))
                scope_label = "all configured tickers"
            if not target:
                bot.answer_callback_query(call.id, "No target tickers.", show_alert=True)
                return
            submission = submit_ticker_background_operation(
                chat_id,
                call.message.message_id,
                label=f"4Y Backfill: {scope_label}",
                task_type="MARKET_BACKFILL",
                tickers=target,
                operation=core_market_data_store.backfill_price_data,
                operation_kwargs={
                    "period": core_market_data_store.FIXED_IS_OOS_HISTORY_PERIOD,
                },
                stage_label="4년 가격 캐시 다운로드",
                format_result=lambda summary: core_market_data_store.format_cache_summary(
                    summary,
                    title="Confirmed 4Y Backfill Complete",
                ),
                result_markup=build_admin_markup(),
            )
            bot.answer_callback_query(
                call.id,
                "Background 4Y backfill started."
                if submission.created
                else f"Another task is already {submission.progress}% complete.",
                show_alert=not submission.created,
            )

        elif data == "admin_refresh_benchmark":
            submission = submit_benchmark_background_operation(
                chat_id,
                call.message.message_id,
            )
            bot.answer_callback_query(
                call.id,
                "Background benchmark refresh started."
                if submission.created
                else f"Another task is already {submission.progress}% complete.",
                show_alert=not submission.created,
            )

        elif data == "admin_cache_status":
            all_tickers = sorted(set(config.universe))
            cache_summary = serialized_runtime_call(
                core_market_data_store.freshness_summary,
                all_tickers,
            )
            benchmark_ticker = (
                core_portfolio_performance.MARKET_BENCHMARK_TICKER
            )
            benchmark_freshness = serialized_runtime_call(
                core_market_data_store.get_data_freshness,
                [benchmark_ticker],
            )
            benchmark_latest = benchmark_freshness.get(benchmark_ticker)
            benchmark_status = (
                f"latest {benchmark_latest}"
                if benchmark_latest
                else "NOT LOADED (reports show N/A / REVIEW)"
            )
            report = (
                "**Data Cache Status**\n\n"
                f"Configured tickers: {len(all_tickers)}\n"
                f"{cache_summary}\n"
                f"ACWI: {benchmark_status}\n"
                f"DB: `{os.path.basename(core_market_data_store.DB_PATH)}`"
            )
            bot.answer_callback_query(call.id)
            bot.edit_message_text(
                report,
                chat_id,
                call.message.message_id,
                parse_mode="Markdown",
                reply_markup=build_admin_markup(),
            )

        elif data == "admin_refresh_market_countries":
            bot.answer_callback_query(call.id, "Refreshing listing-market countries.")
            bot.edit_message_text(
                "Classifying cached tickers into US / CH / JP / KR...",
                chat_id,
                call.message.message_id,
            )
            summary = serialized_runtime_call(
                core_market_data_store.refresh_listing_country_labels_from_cache
            )
            bot.edit_message_text(
                core_market_data_store.format_market_country_summary(summary),
                chat_id,
                call.message.message_id,
                parse_mode="Markdown",
                reply_markup=build_admin_markup(),
            )

        elif data == "admin_refresh_classifications":
            all_tickers = sorted(set(config.universe))
            submission = submit_ticker_background_operation(
                chat_id,
                call.message.message_id,
                label="Security Classification Update",
                task_type="METADATA_REFRESH",
                tickers=all_tickers,
                operation=core_market_data_store.refresh_security_metadata,
                operation_kwargs={},
                stage_label="국가·섹터·산업 데이터 다운로드",
                format_result=core_market_data_store.format_metadata_summary,
                result_markup=build_admin_markup(),
            )
            bot.answer_callback_query(
                call.id,
                "Background metadata update started."
                if submission.created
                else f"Another task is already {submission.progress}% complete.",
                show_alert=not submission.created,
            )

        elif data == "await_quick":
            session["awaiting"] = "quick"
            bot.answer_callback_query(call.id)
            bot.edit_message_text(
                "Send up to 5 tickers for Quick Check.\nExample: `PANW MU TSLA`",
                chat_id,
                call.message.message_id,
                parse_mode="Markdown",
                reply_markup=build_back_home_markup(),
            )

    except ApiTelegramException as e:
        if "message is not modified" not in str(e.description):
            raise e
    except Exception as e:
        bot.answer_callback_query(call.id, "Error.")
        bot.edit_message_text(
            f"Error: {e}",
            chat_id,
            call.message.message_id,
            reply_markup=build_back_home_markup(),
        )


@bot.message_handler(commands=["whoami", "chatid"])
def handle_whoami(message):
    chat_id = str(message.chat.id)
    bot.reply_to(message, f"chat_id: `{chat_id}`", parse_mode="Markdown")


@bot.message_handler(commands=["custom"])
def handle_custom_universe_legacy(message):
    chat_id = str(message.chat.id)
    if not check_permission(chat_id):
        return
    tickers = parse_tickers(message.text.partition(" ")[2])
    if not tickers:
        bot.reply_to(message, "Open /menu -> Settings -> Universe -> Set Direct Input.")
        return
    session = get_or_create_session(chat_id)
    set_direct_input(session, tickers)
    bot.reply_to(
        message,
        f"Direct Input set ({len(tickers)}): `{', '.join(tickers)}`",
        parse_mode="Markdown",
    )


@bot.message_handler(commands=["list"])
def handle_list(message):
    chat_id = str(message.chat.id)
    if not check_permission(chat_id):
        return
    ticker_list = ", ".join(sorted(config.universe))
    bot.reply_to(message, f"Master universe ({len(config.universe)} tickers):\n{ticker_list}")


@bot.message_handler(commands=["add"])
def handle_add_ticker(message):
    chat_id = str(message.chat.id)
    if not check_permission(chat_id):
        return
    try:
        payload = message.text.partition(" ")[2]
        tickers = parse_tickers(payload)
        if not tickers:
            bot.reply_to(
                message,
                "Usage: `/add TSLA` or `/add 1099 HK` or `/add 9983 JP`",
                parse_mode="Markdown",
            )
            return
        new_ticker = tickers[0]
        if new_ticker in config.universe:
            bot.reply_to(message, f"{new_ticker} already exists.")
            return
        config.universe.append(new_ticker)
        if sync_config_file(os.path.dirname(os.path.abspath(__file__))):
            bot.reply_to(message, f"{new_ticker} added. Total: {len(config.universe)}")
        else:
            config.universe.remove(new_ticker)
            bot.reply_to(
                message,
                "Persistent /add is unavailable in QuantBot Beta. "
                "Use /menu -> Settings -> Universe -> Set Direct Input.",
            )
    except Exception as e:
        bot.reply_to(message, f"Add failed: {e}")


@bot.message_handler(commands=["del", "delete"])
def handle_del_ticker(message):
    chat_id = str(message.chat.id)
    if not check_permission(chat_id):
        return
    try:
        payload = message.text.partition(" ")[2]
        tickers = parse_tickers(payload)
        if not tickers:
            bot.reply_to(
                message,
                "Usage: `/del AAPL` or `/del 1099 HK` or `/del 9983 JP`",
                parse_mode="Markdown",
            )
            return
        target_ticker = tickers[0]
        if target_ticker not in config.universe:
            bot.reply_to(message, f"{target_ticker} is not in the master universe.")
            return
        config.universe.remove(target_ticker)
        if sync_config_file(os.path.dirname(os.path.abspath(__file__))):
            bot.reply_to(message, f"{target_ticker} removed. Total: {len(config.universe)}")
        else:
            config.universe.append(target_ticker)
            config.universe.sort()
            bot.reply_to(
                message,
                "Persistent /del is unavailable in QuantBot Beta. "
                "Use /menu -> Settings -> Universe -> Set Direct Input.",
            )
    except Exception as e:
        bot.reply_to(message, f"Delete failed: {e}")


@bot.message_handler(content_types=["text"])
def handle_pending_text(message):
    chat_id = str(message.chat.id)
    if not check_permission(chat_id):
        return

    session = get_or_create_session(chat_id)
    awaiting = session.get("awaiting")
    if not awaiting:
        bot.reply_to(message, "Use /menu to start.")
        return

    tickers = parse_tickers(message.text)
    if not tickers:
        bot.reply_to(message, "No ticker detected. Try again or open /menu.")
        return

    try:
        if awaiting == "custom_universe":
            set_direct_input(session, tickers)
            bot.reply_to(
                message,
                f"Direct Input set ({len(tickers)}): `{', '.join(tickers)}`",
                parse_mode="Markdown",
                reply_markup=build_home_markup(),
            )
        elif awaiting == "quick":
            session["awaiting"] = None
            if len(tickers) > 5:
                bot.reply_to(
                    message,
                    "Quick Check supports up to 5 tickers at once. Please try again with 5 or fewer.",
                    reply_markup=build_back_home_markup(),
                )
                return
            bot.reply_to(
                message,
                f"Running Quick Check for {len(tickers)} ticker(s): {', '.join(tickers)}...",
            )
            reports = build_quick_reports(tickers)
            send_long_message(
                chat_id,
                "\n\n----------------------------------------\n\n".join(reports),
                markup=build_back_home_markup(),
            )
    except Exception as e:
        session["awaiting"] = None
        bot.reply_to(message, f"Error: {e}", reply_markup=build_back_home_markup())


def main():
    print(f"[START] QuantBot version={VERSION} build={BUILD}")
    print(f"[START] Market DB: {core_market_data_store.DB_PATH}")
    app_daily_market_scheduler.start_scheduler_thread()
    bot.infinity_polling()


if __name__ == "__main__":
    main()
