"""배치 실행 진입점.

대화형 실행 (인자 없이 실행):
    python main.py
    ticket_sync.exe

배치/스케줄러 실행 (인자로 전부 지정, 입력 대기 없음):
    ticket_sync.exe --mode today --non-interactive
    ticket_sync.exe --mode range --start-date 2026-07-01 --end-date 2026-07-08 --non-interactive
    ticket_sync.exe --mode all --create-freshdesk-ticket true --non-interactive

Freshdesk 자격증명은 더 이상 CLI 인자로 넘기지 않는다 — 웹 UI의 "Freshdesk Management"
화면에서 저장한 값을 그대로 쓴다.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

import config as config_module
import crawler
import excel_exporter
import job_status
import sync_lock
from freshdesk_client import FreshdeskClient, FreshdeskResult
from logging_setup import setup_logging
from notify import notify
from storage import TicketStore

logger = logging.getLogger("ticket_sync.main")


# ----------------------------- CLI 인자 ----------------------------- #

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Engage Center -> Freshdesk 티켓 동기화")
    p.add_argument("--config", default=None, help="config.json 경로 (기본: ./config.json)")
    p.add_argument("--env-file", default=None, help=".env 경로 (기본: ./.env)")

    p.add_argument("--mode", choices=["all", "range", "today"], help="티켓 조회 범위")
    p.add_argument("--all", action="store_true", help="전체 조회 (--mode all 과 동일)")
    p.add_argument("--today", action="store_true", help="오늘 생성 티켓만 조회 (--mode today 와 동일)")
    p.add_argument("--start-date", help="조회 시작일 YYYY-MM-DD (종료일은 항상 오늘로 자동 설정됩니다)")
    p.add_argument("--end-date", help="조회 종료일 YYYY-MM-DD (기본값: 오늘)")
    p.add_argument(
        "--case-status",
        choices=["open", "closed", "all"],
        help="조회할 케이스 상태 (열기/완료됨/전체, 기본값 open). "
        "'all'은 화면에 옵션이 없어 열기/완료됨을 각각 조회해 Microsoft Case ID 기준으로 합칩니다.",
    )
    p.add_argument(
        "--date-basis",
        choices=["created", "updated"],
        help="날짜 조회 기준 (created=만들어짐, updated=업데이트됨, 기본값 created). "
        "updated는 화면 날짜 필터를 쓰지 않고 목록의 '업데이트됨' 값을 기준으로 직접 걸러냅니다.",
    )

    p.add_argument(
        "--create-freshdesk-ticket",
        choices=["true", "false"],
        help="신규 티켓을 Freshdesk에 등록할지 여부 (기본값 false)",
    )
    p.add_argument(
        "--save-excel",
        choices=["true", "false"],
        help="결과를 Excel 파일로도 저장할지 여부 (기본값 false)",
    )
    p.add_argument("--headless", choices=["true", "false"], help="브라우저 창을 숨길지 여부")
    p.add_argument(
        "--non-interactive",
        action="store_true",
        help="입력 프롬프트 없이 인자/설정값만으로 실행 (작업 스케줄러/Cron용)",
    )
    return p


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def resolve_mode(args) -> tuple[str, str | None, str | None]:
    if args.all:
        return "all", None, None
    if args.today:
        return "today", None, None
    if args.start_date or args.end_date:
        return "range", args.start_date, args.end_date or _today_str()
    if args.mode:
        end_date = args.end_date
        if args.mode == "range" and not end_date:
            end_date = _today_str()
        return args.mode, args.start_date, end_date
    return "", None, None


# ----------------------------- 대화형 입력 ----------------------------- #

def _ask(prompt: str, options: dict[str, str], default: str) -> str:
    print(f"\n{prompt}")
    for key, label in options.items():
        marker = " (기본)" if key == default else ""
        print(f"  {key}) {label}{marker}")
    choice = input(f"선택 [{default}]: ").strip() or default
    while choice not in options:
        choice = input(f"올바른 번호를 입력하세요 {list(options.keys())}: ").strip()
    return choice


def _ask_yes_no(prompt: str, default: bool = True) -> bool:
    suffix = "Y/n" if default else "y/N"
    ans = input(f"{prompt} [{suffix}]: ").strip().lower()
    if not ans:
        return default
    return ans in ("y", "yes")


def run_wizard(args) -> dict:
    print("=" * 60)
    print(" Engage Center -> Freshdesk 티켓 동기화")
    print("=" * 60)

    mode_choice_map = {"1": "all", "2": "range", "3": "today"}
    mode_choice = _ask(
        "1) 티켓 조회 범위를 선택하세요",
        {"1": "전체 조회", "2": "날짜 범위 조회 (시작일 입력, 종료일은 항상 오늘)", "3": "오늘 생성 티켓만 조회"},
        default="1",
    )
    mode = mode_choice_map[mode_choice]
    start_date = end_date = None
    date_basis = "created"
    if mode == "range":
        basis_choice_map = {"1": "created", "2": "updated"}
        basis_choice = _ask(
            "1-1) 날짜 조회 기준을 선택하세요",
            {"1": "만들어짐", "2": "업데이트됨"},
            default="1",
        )
        date_basis = basis_choice_map[basis_choice]

        while True:
            start_date = input("시작일 (예: 2026-01-01): ").strip()
            try:
                datetime.strptime(start_date, "%Y-%m-%d")
                break
            except ValueError:
                print("형식이 올바르지 않습니다. YYYY-MM-DD 형식으로 입력하세요 (예: 2026-01-01).")
        end_date = _today_str()

    status_choice_map = {"1": "open", "2": "closed", "3": "all"}
    status_choice = _ask(
        "2) 조회할 케이스 상태를 선택하세요",
        {"1": "열기 (Open)", "2": "완료됨 (Closed)", "3": "전체 (All, 열기+완료됨 각각 조회 후 통합)"},
        default="1",
    )
    case_status = status_choice_map[status_choice]

    save_excel = _ask_yes_no(
        "3) Excel 파일로도 저장하시겠습니까?", default=False
    )
    do_freshdesk = _ask_yes_no(
        "4) 신규 티켓을 Freshdesk에도 등록하시겠습니까? (Freshdesk Management 화면에서 "
        "자격증명을 먼저 저장해두어야 합니다)", default=False
    )

    print("\n" + "-" * 60)
    print("[실행 조건 확인]")
    print(f"  조회 범위          : {mode}" + (f" ({start_date} ~ {end_date})" if mode == "range" else ""))
    if mode == "range":
        print(f"  날짜 조회 기준      : {'만들어짐' if date_basis == 'created' else '업데이트됨'}")
    print(f"  케이스 상태         : {case_status}")
    print(f"  Excel 저장         : {'예' if save_excel else '아니오'}")
    print(f"  Freshdesk 등록      : {'예' if do_freshdesk else '아니오'}")
    print("-" * 60)
    if not _ask_yes_no("위 조건으로 실행할까요?", default=True):
        print("실행을 취소했습니다.")
        sys.exit(0)

    return {
        "mode": mode,
        "start_date": start_date,
        "end_date": end_date,
        "date_basis": date_basis,
        "case_status": case_status,
        "save_excel": save_excel,
        "do_freshdesk": do_freshdesk,
    }


# ----------------------------- 동기화 실행 ----------------------------- #

def run_sync(
    app_config,
    mode: str,
    start_date,
    end_date,
    case_status: str,
    date_basis: str,
    do_freshdesk: bool,
    run_date_str: str,
    owner: str = "cli",
    skip_dedup: bool = False,
    save_excel: bool = False,
) -> dict:
    """owner 는 sync_lock 잠금 파일에 남기는 실행 주체 표시일 뿐이다("web-ui", "cronjob"
    등). 웹 UI에서 실행한 동기화와 K8s 자동 배치(CronJob)가 완전히 다른 프로세스/파드로
    동시에 실행돼 브라우저 세션·SQLite 파일을 함께 건드리는 사고를 막기 위해, 실제 수집을
    시작하기 전에 data 디렉터리에 파일 기반 잠금을 건다. 이미 다른 프로세스가 실행
    중이면 SyncAlreadyRunningError 가 그대로 위로 전파된다(호출부에서 처리).

    skip_dedup=True(테스트 모드)이면 이미 Freshdesk 등록에 성공한 케이스도 무시하고 이번에
    수집된 케이스를 전부 다시 등록 시도한다 — 실제 운영에서는 절대 켜면 안 되고(중복 티켓
    생성 위험), 등록 흐름 자체를 테스트할 때만 웹 UI에서 켜서 쓴다. DB 행 자체의 중복 저장
    (ms_case_id UNIQUE 인덱스)은 이 옵션과 무관하게 항상 방지된다.

    save_excel=True 일 때만 결과를 Excel 파일로도 저장한다(기본값 False — 더 이상 매 실행마다
    자동으로 저장하지 않고, 필요할 때만 선택해서 저장한다)."""
    errors: list[dict] = []
    all_tickets: list[dict] = []
    new_tickets: list[dict] = []
    freshdesk_results: list[dict] = []
    freshdesk_success = 0
    freshdesk_failed = 0
    duplicate_count = 0
    excel_path = None

    with sync_lock.acquire_or_raise(app_config.db_path.parent, owner):
        cache_store = TicketStore.from_config(app_config)
        try:
            existing_tickets = cache_store.get_enrichment_cache()
        finally:
            cache_store.close()

        job_status.set_progress("Engage Center 로그인 확인 중", 2)
        try:
            job_status.set_progress("케이스 목록 수집 중", 5)
            all_tickets, crawl_warnings = crawler.collect_tickets(
                app_config, mode, start_date, end_date, case_status, date_basis, do_freshdesk,
                existing_tickets=existing_tickets,
            )
            for w in crawl_warnings:
                errors.append({"단계": "수집", "대상": "-", "오류 메시지": w})
        except crawler.SessionExpiredError as exc:
            logger.exception("로그인 세션이 만료되어 수집을 진행할 수 없습니다")
            errors.append({"단계": "수집", "대상": "-", "오류 메시지": str(exc)})
            notify(
                "Engage Center 로그인 세션이 만료되어 자동 동기화가 중단됐습니다.\n"
                "로컬 PC에서 다시 로그인한 뒤(python main.py) data/browser_profile 을 "
                "K8s 볼륨에 다시 복사해주세요.\n"
                f"오류 상세: {exc}",
                title="⚠ Ticket Sync 로그인 세션 만료",
            )
        except Exception as exc:
            logger.exception("티켓 수집 중 오류가 발생했습니다")
            errors.append({"단계": "수집", "대상": "-", "오류 메시지": str(exc)})

        job_status.set_progress("데이터 저장 중", 75)
        now_iso = datetime.now().isoformat(timespec="seconds")
        to_attempt_freshdesk: list[tuple[int, dict]] = []
        all_committed: list[tuple[int, dict]] = []

        store = TicketStore.from_config(app_config)
        try:
            for t in all_tickets:
                try:
                    result = store.upsert_ticket(t, now_iso, commit=False)
                except Exception as exc:
                    logger.warning("티켓 저장 실패: %s", exc, exc_info=True)
                    errors.append(
                        {"단계": "저장", "대상": t.get("ms_case_id") or t.get("title", ""), "오류 메시지": str(exc)}
                    )
                    continue

                all_committed.append((result.ticket_row_id, t))
                if result.is_new:
                    new_tickets.append(t)
                    to_attempt_freshdesk.append((result.ticket_row_id, t))
                else:
                    duplicate_count += 1
            store.commit()

            if do_freshdesk:
                if skip_dedup:
                    # 테스트 모드: 이번에 수집된 케이스 전부(신규+기존) 재등록 시도.
                    to_attempt_freshdesk = list(all_committed)
                else:
                    already_ids = {row_id for row_id, _ in to_attempt_freshdesk}
                    for row in store.get_pending_or_failed_freshdesk():
                        if row["id"] in already_ids:
                            continue
                        to_attempt_freshdesk.append((row["id"], dict(row)))

                client = FreshdeskClient(app_config.freshdesk)
                fd_total = len(to_attempt_freshdesk)
                for fd_idx, (row_id, t) in enumerate(to_attempt_freshdesk, start=1):
                    if fd_total:
                        job_status.set_progress(
                            f"Freshdesk 등록 중 ({fd_idx}/{fd_total})", 80 + round(fd_idx / fd_total * 15)
                        )
                    try:
                        fd_result = client.create_ticket(t, app_config.priority_map)
                    except Exception as exc:
                        logger.warning("Freshdesk 등록 중 예기치 못한 오류: %s", exc, exc_info=True)
                        fd_result = FreshdeskResult(success=False, error=str(exc))
                    sync_ts = datetime.now().isoformat(timespec="seconds")
                    status = "success" if fd_result.success else "failed"
                    store.mark_freshdesk_result(row_id, status, fd_result.ticket_id, fd_result.error, sync_ts)

                    result_row = dict(t)
                    result_row["freshdesk_status"] = status
                    result_row["freshdesk_ticket_id"] = fd_result.ticket_id
                    result_row["freshdesk_error"] = fd_result.error
                    freshdesk_results.append(result_row)

                    if fd_result.success:
                        freshdesk_success += 1
                        messages = t.get("_communication_messages")
                        if messages and fd_result.ticket_id:
                            note_results = client.add_conversation_notes(fd_result.ticket_id, messages)
                            failed_notes = [r for r in note_results if not r.success]
                            if failed_notes:
                                logger.warning(
                                    "티켓 #%s 대화 노트 %d/%d건 등록 실패", fd_result.ticket_id,
                                    len(failed_notes), len(note_results),
                                )
                                errors.append(
                                    {
                                        "단계": "Freshdesk 대화 노트 등록",
                                        "대상": t.get("ms_case_id") or t.get("title", ""),
                                        "오류 메시지": f"{len(failed_notes)}/{len(note_results)}건 실패: "
                                        + "; ".join(r.error or "" for r in failed_notes),
                                    }
                                )
                    else:
                        freshdesk_failed += 1
                        errors.append(
                            {
                                "단계": "Freshdesk 등록",
                                "대상": t.get("ms_case_id") or t.get("title", ""),
                                "오류 메시지": fd_result.error or "",
                            }
                        )

            if save_excel:
                job_status.set_progress("결과 파일(Excel) 저장 중", 97)
                query_info = {
                    "날짜 조회 기준": "만들어짐 기준 조회" if date_basis == "created" else "업데이트됨 기준 조회",
                    "상태 조회 기준": {"open": "열기", "closed": "완료됨", "all": "모두"}.get(case_status, case_status),
                    "조회 범위": mode,
                    "시작일": start_date or "-",
                    "종료일": end_date or "-",
                }

                excel_path = app_config.excel_dir / f"serviceshub_ticket_sync_{run_date_str}.xlsx"
                try:
                    excel_exporter.export_excel(
                        excel_path, all_tickets, new_tickets, freshdesk_results, errors, query_info
                    )
                except PermissionError:
                    # 같은 이름의 파일이 Excel 등에서 열려 있어 덮어쓸 수 없는 경우, 파일을 닫지
                    # 않아도 되도록 시각을 붙인 별도 파일로 저장한다.
                    alt_excel_path = app_config.excel_dir / (
                        f"serviceshub_ticket_sync_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
                    )
                    logger.warning(
                        "%s 파일이 다른 프로그램에서 열려 있어 저장할 수 없습니다. 대신 %s 로 저장합니다.",
                        excel_path, alt_excel_path,
                    )
                    excel_exporter.export_excel(
                        alt_excel_path, all_tickets, new_tickets, freshdesk_results, errors, query_info
                    )
                    excel_path = alt_excel_path
            else:
                job_status.set_progress("결과 정리 중", 97)

            store.record_sync_run(
                {
                    "run_at": datetime.now().isoformat(timespec="seconds"),
                    "mode": mode,
                    "start_date": start_date,
                    "end_date": end_date,
                    "case_status": case_status,
                    "date_basis": date_basis,
                    "freshdesk_env": app_config.freshdesk.name if do_freshdesk else "none",
                    "total_collected": len(all_tickets),
                    "new_count": len(new_tickets),
                    "duplicate_count": duplicate_count,
                    "freshdesk_success": freshdesk_success,
                    "freshdesk_failed": freshdesk_failed,
                    "error_count": len(errors),
                    "error_summary": "; ".join(e["오류 메시지"] for e in errors[:5]),
                    "excel_path": str(excel_path),
                    "trigger": owner,
                }
            )
        finally:
            store.close()

    return {
        "total_collected": len(all_tickets),
        "new_count": len(new_tickets),
        "duplicate_count": duplicate_count,
        "freshdesk_success": freshdesk_success,
        "freshdesk_failed": freshdesk_failed,
        "error_count": len(errors),
        "excel_path": str(excel_path) if excel_path else None,
    }


def main(argv=None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    interactive = not args.non_interactive and len(sys.argv) == 1

    if interactive:
        choices = run_wizard(args)
        mode = choices["mode"]
        start_date = choices["start_date"]
        end_date = choices["end_date"]
        date_basis = choices["date_basis"]
        case_status = choices["case_status"]
        save_excel = choices["save_excel"]
        do_freshdesk = choices["do_freshdesk"]
    else:
        mode, start_date, end_date = resolve_mode(args)
        if not mode:
            if args.non_interactive:
                logger.warning("--mode 가 지정되지 않아 기본값 'all' 로 실행합니다.")
            mode = "all"
        case_status = args.case_status or "open"
        date_basis = args.date_basis or "created"
        save_excel = args.save_excel == "true"
        do_freshdesk = args.create_freshdesk_ticket == "true"

    run_date_str = datetime.now().strftime("%Y%m%d")

    try:
        app_config = config_module.load_config(
            config_path=args.config,
            env_path=args.env_file,
            require_freshdesk_credentials=do_freshdesk,
        )
    except Exception as exc:
        print(f"설정 로딩 실패: {exc}", file=sys.stderr)
        return 1

    if args.headless is not None:
        app_config.crawler.headless = args.headless == "true"

    log_path = setup_logging(app_config.log_dir, run_date_str)
    logger.info(
        "동기화 시작 | mode=%s start=%s end=%s case_status=%s date_basis=%s "
        "create_freshdesk_ticket=%s save_excel=%s",
        mode, start_date, end_date, case_status, date_basis, do_freshdesk, save_excel,
    )

    owner = "cronjob" if args.non_interactive else "cli"
    try:
        summary = run_sync(
            app_config, mode, start_date, end_date, case_status, date_basis, do_freshdesk, run_date_str,
            owner=owner, save_excel=save_excel,
        )
    except sync_lock.SyncAlreadyRunningError as exc:
        logger.warning("다른 실행이 이미 진행 중이라 이번 실행은 건너뜁니다: %s", exc)
        print(f"\n다른 실행이 이미 진행 중입니다: {exc}")
        return 0
    except Exception:
        logger.exception("동기화 실행 중 예기치 못한 오류로 중단되었습니다")
        print("\n실행 중 오류가 발생했습니다. 로그를 확인하세요:", log_path)
        return 1

    print("\n" + "=" * 60)
    print("[실행 결과]")
    print(f"  전체 수집 건수      : {summary['total_collected']}")
    print(f"  신규 등록 대상 건수 : {summary['new_count']}")
    print(f"  중복 제외 건수      : {summary['duplicate_count']}")
    print(f"  Freshdesk 등록 성공 : {summary['freshdesk_success']}")
    print(f"  Freshdesk 등록 실패 : {summary['freshdesk_failed']}")
    print(f"  오류/실패 건수      : {summary['error_count']}")
    print(f"  Excel 저장 경로     : {summary['excel_path'] or '(저장 안 함)'}")
    print(f"  로그 저장 경로      : {log_path}")
    print("=" * 60)

    logger.info("동기화 종료 | %s", summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
