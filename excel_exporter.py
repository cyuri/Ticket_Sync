"""동기화 결과를 시트별로 나눈 Excel 파일로 저장."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
from openpyxl.styles import Font

TICKET_COLUMNS = [
    ("ms_case_id", "Microsoft Case ID"),
    ("title", "제목"),
    ("status", "상태"),
    ("status_message", "상태 메시지"),
    ("severity", "심각도"),
    ("created_at", "생성일"),
    ("modified_at", "최종 수정일"),
    ("product", "제품/서비스"),
    ("product_family", "제품군"),
    ("category", "범주"),
    ("problem_type", "문제"),
    ("requester", "요청자"),
    ("assignee", "담당자"),
    ("incident_manager", "인시던트 관리자"),
    ("workspace", "작업 영역"),
    ("country_region", "국가/지역"),
    ("timezone", "표준 시간대"),
    ("case_owner", "지원 요청 소유자"),
    ("contact_method", "기본 연락 방법"),
    ("contract_id", "계약 ID"),
    ("case_type", "케이스 유형"),
    ("closed_at", "종료일"),
    ("tenant_id", "테넌트 ID"),
    ("subscription_id", "구독 ID"),
    ("tenant", "테넌트"),
    ("case_url", "케이스 URL"),
    ("summary", "상세 내용"),
    ("communication_html_path", "커뮤니케이션 HTML 링크"),
]

FRESHDESK_RESULT_COLUMNS = [
    ("ms_case_id", "Microsoft Case ID"),
    ("title", "제목"),
    ("freshdesk_status", "Freshdesk 등록 상태"),
    ("freshdesk_ticket_id", "Freshdesk 티켓 ID"),
    ("freshdesk_error", "오류 메시지"),
]


def _to_dataframe(tickets: list[dict], columns: list[tuple[str, str]]) -> pd.DataFrame:
    if not tickets:
        return pd.DataFrame(columns=[label for _, label in columns])
    rows = [{label: t.get(key, "") for key, label in columns} for t in tickets]
    return pd.DataFrame(rows, columns=[label for _, label in columns])


def _autofit(writer: pd.ExcelWriter, sheet_name: str, df: pd.DataFrame):
    worksheet = writer.sheets[sheet_name]
    for i, col in enumerate(df.columns, start=1):
        max_len = max([len(str(col))] + [len(str(v)) for v in df[col].astype(str)]) if len(df) else len(str(col))
        worksheet.column_dimensions[worksheet.cell(row=1, column=i).column_letter].width = min(max_len + 4, 80)


def _add_hyperlinks(writer: pd.ExcelWriter, sheet_name: str, df: pd.DataFrame, column_label: str):
    """지정한 컬럼에 파일 경로가 들어있으면 클릭 가능한 하이퍼링크로 바꾼다."""
    if column_label not in df.columns:
        return
    worksheet = writer.sheets[sheet_name]
    col_idx = list(df.columns).index(column_label) + 1
    for row_idx, value in enumerate(df[column_label], start=2):
        if not value:
            continue
        cell = worksheet.cell(row=row_idx, column=col_idx)
        try:
            cell.hyperlink = Path(str(value)).resolve().as_uri()
        except Exception:
            continue
        cell.value = "열기"
        cell.font = Font(color="0563C1", underline="single")


def export_excel(
    output_path: Path,
    all_tickets: list[dict],
    new_tickets: list[dict],
    freshdesk_results: list[dict],
    errors: list[dict],
    query_info: dict | None = None,
) -> Path:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        if query_info:
            df_query = pd.DataFrame(list(query_info.items()), columns=["항목", "값"])
            df_query.to_excel(writer, sheet_name="조회 조건", index=False)
            _autofit(writer, "조회 조건", df_query)

        df_all = _to_dataframe(all_tickets, TICKET_COLUMNS)
        df_all.to_excel(writer, sheet_name="전체 수집 티켓", index=False)
        _autofit(writer, "전체 수집 티켓", df_all)
        _add_hyperlinks(writer, "전체 수집 티켓", df_all, "커뮤니케이션 HTML 링크")

        df_new = _to_dataframe(new_tickets, TICKET_COLUMNS)
        df_new.to_excel(writer, sheet_name="신규 등록 대상", index=False)
        _autofit(writer, "신규 등록 대상", df_new)
        _add_hyperlinks(writer, "신규 등록 대상", df_new, "커뮤니케이션 HTML 링크")

        df_fd = _to_dataframe(freshdesk_results, FRESHDESK_RESULT_COLUMNS)
        df_fd.to_excel(writer, sheet_name="Freshdesk 등록 결과", index=False)
        _autofit(writer, "Freshdesk 등록 결과", df_fd)

        df_err = pd.DataFrame(errors, columns=["단계", "대상", "오류 메시지"]) if errors else pd.DataFrame(
            columns=["단계", "대상", "오류 메시지"]
        )
        df_err.to_excel(writer, sheet_name="오류 및 실패 내역", index=False)
        _autofit(writer, "오류 및 실패 내역", df_err)

    return output_path
