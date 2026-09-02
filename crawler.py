"""Microsoft Engage Center 로그인 + 케이스 목록(CSV 다운로드) 수집 + 상세 페이지 보강.

설계 메모
---------
Engage Center는 SPA(Ibiza/포털 프레임워크)라서 '보기 소스'로는 실제 DOM을 볼 수 없고,
Fluent UI 특성상 CSS 클래스가 배포마다 바뀔 수 있다. 그래서 이 크롤러는:

1) 테이블 행을 직접 긁는 대신, 화면의 "CSV로 다운로드" 버튼을 클릭해 목록을 파일로 받는다.
   -> 화면 구조 변경에 훨씬 덜 취약하다.
2) 날짜 범위는 화면 필터 UI를 자동화하는 대신, CSV 전체(또는 상태 필터만 유지한 범위)를
   받아온 뒤 파이썬에서 생성일 기준으로 걸러낸다.
3) CSV에 없는 항목(심각도/인시던트 관리자/작업 영역/문제/설명 등 상세 화면의 모든 항목)은
   신규 판단된 티켓에 한해 상세 페이지에 들어가 보강 수집한다. 어떤 라벨을 어떤 필드로
   저장할지는 config/selectors.json 의 case_detail.field_labels 에서 관리하므로, 화면에
   항목이 추가되면 코드가 아니라 그 설정 파일만 수정하면 된다.

로그인은 Playwright persistent context를 사용한다. MFA가 걸려 있으므로 최초 실행 시에는
headless=False 로 띄운 창에서 사람이 직접 로그인해야 하며, 이후에는 세션 쿠키가
data/browser_profile 폴더에 저장되어 자동으로 재사용된다. 세션이 만료되면 다시 수동
로그인을 요구한다(자동 크래시 방지, SessionExpiredError로 명확히 보고).
"""
from __future__ import annotations

import csv
import html
import json
import logging
import re
import time
from datetime import datetime, date
from pathlib import Path
from typing import Optional

from playwright.sync_api import (
    sync_playwright,
    Page,
    BrowserContext,
    TimeoutError as PlaywrightTimeoutError,
)

import job_status
from storage import DETAIL_ENRICHMENT_FIELDS

from config import AppConfig
import communication_html

logger = logging.getLogger("ticket_sync.crawler")


class SessionExpiredError(RuntimeError):
    pass


class PageStructureError(RuntimeError):
    """화면 구조가 바뀌어서 기대하던 요소를 찾지 못했을 때."""


class CrawlerError(RuntimeError):
    pass


def _download_dir(config: AppConfig) -> Path:
    d = config.ms_account.browser_profile_dir.parent / "downloads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def open_browser_context(config: AppConfig):
    """Playwright + persistent context 를 열어 (playwright, context, page) 를 반환한다."""
    pw = sync_playwright().start()
    try:
        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(config.ms_account.browser_profile_dir),
            headless=config.crawler.headless,
            accept_downloads=True,
        )
    except Exception as exc:
        pw.stop()
        raise CrawlerError(
            "브라우저를 실행하지 못했습니다. Playwright 브라우저가 설치되어 있는지 확인하세요 "
            "(최초 1회 'playwright install chromium' 필요)."
        ) from exc

    page = context.pages[0] if context.pages else context.new_page()
    page.set_default_timeout(config.crawler.navigation_timeout_ms)
    return pw, context, page


def _iter_frames(page: Page):
    """메인 프레임을 먼저, 그 다음 나머지 프레임들을 순서대로 준다."""
    yield page.main_frame
    for f in page.frames:
        if f is not page.main_frame:
            yield f


def _find_in_frames_with_frame(page: Page, text: str, exact: bool = False, timeout_ms: int = 5000):
    """Azure Portal은 Engage Center 같은 확장 화면을 최상위 문서가 아니라 별도 iframe 안에
    그린다. 그래서 page.get_by_text() 를 최상위 프레임에만 쓰면 iframe 안의 요소를 못 찾는다.
    이 함수는 모든 프레임(최상위 + 하위 iframe)을 순회하며 텍스트를 찾고, timeout_ms 까지
    폴링하며 재시도한다. 매칭된 (frame, locator) 를 반환하고, 못 찾으면 (None, None)."""
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        for frame in _iter_frames(page):
            try:
                loc = frame.get_by_text(text, exact=exact)
                if loc.count() > 0:
                    return frame, loc.first
            except Exception:
                continue
        if time.monotonic() >= deadline:
            return None, None
        page.wait_for_timeout(300)


def _find_locator_in_frames(page: Page, text: str, exact: bool = False, timeout_ms: int = 5000):
    """_find_in_frames_with_frame() 의 결과에서 locator 만 반환한다."""
    _, loc = _find_in_frames_with_frame(page, text, exact=exact, timeout_ms=timeout_ms)
    return loc


_POPOVER_SELECTOR = "[role='dialog'], [role='menu'], [role='listbox'], .fui-PopoverSurface"


def _find_in_popover(frame, text: str, exact: bool = True, timeout_ms: int = 6000):
    """필터 플라이아웃/팝오버 안에서만 텍스트를 찾는다. 이 앱의 팝오버는 화면마다
    role=dialog, role=menu, role=listbox, role=group(fui-PopoverSurface) 등 다양한
    역할을 쓰므로 전부 포함해서 찾는다. "만들어짐" 같은 필터 메뉴 항목이 목록 테이블의
    컬럼 제목과 텍스트가 같아서, 프레임 전체에서 찾으면 엉뚱한(테이블 쪽) 요소를 집는
    문제가 있어 팝오버 범위로 한정한다."""
    if frame is None:
        return None
    deadline = time.monotonic() + timeout_ms / 1000
    page = frame.page
    while True:
        try:
            popovers = frame.locator(_POPOVER_SELECTOR)
            for i in range(popovers.count()):
                pop = popovers.nth(i)
                try:
                    if not pop.is_visible(timeout=200):
                        continue
                except Exception:
                    continue
                loc = pop.get_by_text(text, exact=exact)
                if loc.count() > 0:
                    return loc.first
        except Exception:
            pass
        if time.monotonic() >= deadline:
            return None
        page.wait_for_timeout(300)


def _last_visible_popover(frame):
    """가장 마지막(가장 나중에 열린, 최상위) 팝오버를 반환한다 - 보통 방금 연 캘린더 등."""
    try:
        popovers = frame.locator(_POPOVER_SELECTOR)
        for i in range(popovers.count() - 1, -1, -1):
            pop = popovers.nth(i)
            try:
                if pop.is_visible(timeout=300):
                    return pop
            except Exception:
                continue
    except Exception:
        pass
    return None


_LOGIN_PASSTHROUGH_TEXTS = [
    "Stay signed in", "로그인 상태를 유지", "로그인 상태 유지",
    "Yes", "예",
    "Next", "다음",
    "Continue", "계속",
    "OK", "확인",
]

# 비밀번호 제출 버튼은 우리가 비밀번호를 입력하는 게 아니라, 이미 채워져 있을 때만
# (브라우저/윈도우 자격 증명 자동완성 등으로) 눌러준다. 빈 비밀번호로 잘못 제출되는 걸
# 막기 위해 _password_prefilled() 로 확인한 경우에만 이 후보들을 시도한다.
_PASSWORD_SUBMIT_TEXTS = ["로그인", "Sign in"]


def _password_prefilled(page: Page) -> bool:
    """비밀번호 입력창에 이미 값이 채워져 있는지 확인한다."""
    for frame in _iter_frames(page):
        try:
            pw_input = frame.locator("input[type='password']")
            if pw_input.count() > 0 and (pw_input.first.input_value(timeout=300) or "").strip():
                return True
        except Exception:
            continue
    return False


def _try_click_login_passthrough(page: Page, config: AppConfig) -> bool:
    """이미 인증된 세션에서 계정 선택 타일이나 '로그인 상태 유지' 같은 확인 버튼만
    자동으로 눌러서 통과시킨다. 비밀번호 입력창에 이미 값이 채워져 있으면 그 제출
    버튼("로그인"/"Sign in")도 눌러주지만, 비밀번호를 우리가 직접 입력하지는 않는다.
    MFA 코드/승인 화면은 절대 자동으로 다루지 않는다. 한 번에 최대 하나만 클릭한다."""
    if _password_prefilled(page):
        # 비밀번호 화면에서도 상단에 계정 이름이 계속 보이는 경우가 많아서, 일반
        # 후보와 같이 두면 로그인 버튼까지 못 가고 계정 이름만 계속 클릭하게 된다.
        # 비밀번호가 채워져 있으면 로그인 제출 버튼만 우선 시도한다.
        candidates = list(_PASSWORD_SUBMIT_TEXTS)
    else:
        username = config.ms_account.username
        candidates = ([username] if username else []) + _LOGIN_PASSTHROUGH_TEXTS
    for text in candidates:
        for frame in _iter_frames(page):
            try:
                loc = frame.get_by_text(text, exact=False)
                if loc.count() == 0:
                    continue
                target = loc.first
                if not target.is_visible(timeout=200):
                    continue
                # 텍스트 노드 자체보다, 그걸 감싸는 클릭 가능한 타일/버튼/링크가 있으면 그걸 누른다.
                clickable = target.locator(
                    "xpath=ancestor-or-self::*[self::button or self::a or @role='button'][1]"
                )
                click_target = clickable.first if clickable.count() > 0 else target
                click_target.click(timeout=1000)
                logger.info("로그인 화면에서 '%s' 를 자동으로 클릭했습니다.", text)
                return True
            except Exception:
                continue
    return False


def _wait_for_login_marker(page: Page, config: AppConfig, marker_text: str, timeout_ms: int) -> bool:
    """marker_text 가 나타날 때까지 대기하면서, 그 사이에 계정 선택/로그인 상태 유지처럼
    비밀번호·MFA 입력이 필요 없는 확인 화면은 자동으로 클릭해 넘겨준다. 같은 화면이 아직
    안 넘어갔을 때 너무 자주 다시 클릭하면 로그만 지저분해지므로 클릭 시도 간격을 둔다."""
    deadline = time.monotonic() + timeout_ms / 1000
    last_click_attempt = 0.0
    click_interval_s = 1.5
    while True:
        for frame in _iter_frames(page):
            try:
                if frame.get_by_text(marker_text, exact=False).count() > 0:
                    return True
            except Exception:
                continue
        if time.monotonic() >= deadline:
            return False
        if time.monotonic() - last_click_attempt >= click_interval_s:
            _try_click_login_passthrough(page, config)
            last_click_attempt = time.monotonic()
        page.wait_for_timeout(300)


def ensure_logged_in(page: Page, config: AppConfig):
    """케이스 관리 화면 접근을 시도하고, 로그인이 안 되어 있으면 수동 로그인을 기다린다."""
    nav = config.selectors["navigation"]
    marker_text = nav["logged_in_marker_text"]
    login_timeout_s = int(nav.get("login_timeout_seconds", 300))

    try:
        page.goto(config.ms_account.case_management_url, wait_until="domcontentloaded")
    except PlaywrightTimeoutError as exc:
        raise CrawlerError(f"Engage Center 접속에 실패했습니다 (네트워크/URL 확인 필요): {exc}") from exc

    if _wait_for_login_marker(page, config, marker_text, timeout_ms=8000):
        logger.info("기존 세션으로 로그인 확인됨")
        return

    logger.warning(
        "로그인 세션이 없거나 만료된 것으로 보입니다. 계정 선택/로그인 상태 유지 확인처럼 "
        "비밀번호 없이 넘어갈 수 있는 화면은 자동으로 눌러드리며, 비밀번호나 MFA가 필요하면 "
        "브라우저 창에서 직접 완료해주세요. 최대 %s초 대기합니다.",
        login_timeout_s,
    )
    if not _wait_for_login_marker(page, config, marker_text, timeout_ms=login_timeout_s * 1000):
        raise SessionExpiredError(
            "제한 시간 내에 로그인이 완료되지 않았습니다. 프로그램을 다시 실행해 로그인을 완료해주세요."
        )

    logger.info("수동 로그인 완료 확인됨. 세션이 저장되어 다음 실행부터는 재사용됩니다.")


def _apply_status_filter(page: Page, config: AppConfig, case_status: str):
    """케이스 상태 필터를 연다(open) 또는 완료됨(closed) 단일 값으로 적용한다.
    "모두"는 화면에 옵션이 없어서 여기서 다루지 않고, 상위(collect_tickets)에서
    "열기"와 "완료됨"을 각각 따로 조회해서 합치는 방식으로 처리한다.

    이 메뉴 항목은 실제 <input> 체크박스가 아니라 aria-checked 없이 아이콘으로만
    체크 표시를 하는 Fluent 컴포넌트라서 현재 체크 상태를 DOM에서 안정적으로 읽어올
    수 없었다. 대신 이 함수가 항상 로그인 직후, 즉 화면이 기본값("열기"만 체크됨)인
    상태에서 호출된다는 점을 이용해, 그 기본값을 기준으로 필요한 항목만 클릭한다.

    case_status: "open"(열기만, 기본값이라 클릭 없이 그대로 둠) |
                 "closed"(완료됨만, "열기" 해제 + "완료됨" 체크)."""
    if case_status not in ("open", "closed"):
        case_status = "open"

    want_closed = case_status == "closed"

    timeout_ms = config.crawler.navigation_timeout_ms

    # 그냥 "상태"로 찾으면 좌측 내비게이션의 "IT 상태" 메뉴에도 걸려서 잘못 클릭될 수
    # 있으므로, 케이스 목록 칩에만 있는 콜론까지 포함해서 찾는다 ("상태 : 열기" 등).
    frame, chip = _find_in_frames_with_frame(page, "상태 :", exact=False, timeout_ms=timeout_ms)
    if chip is None:
        logger.warning("'상태' 필터를 찾지 못해 상태 필터(%s)를 적용하지 못했습니다.", case_status)
        return
    chip.click()
    page.wait_for_timeout(600)

    if want_closed:
        open_item = _find_in_popover(frame, "열기", exact=False, timeout_ms=5000)
        if open_item is not None:
            open_item.click()
            page.wait_for_timeout(300)
        else:
            logger.warning("상태 필터에서 '열기' 항목을 찾지 못했습니다.")

        closed_item = _find_in_popover(frame, "완료됨", exact=False, timeout_ms=5000)
        if closed_item is not None:
            closed_item.click()
            page.wait_for_timeout(300)
        else:
            logger.warning("상태 필터에서 '완료됨' 항목을 찾지 못했습니다.")

    # 결과 건수가 많은 상태(예: 완료됨)로 바꾸면 목록이 훨씬 크게 다시 로드되면서
    # 팝오버가 Apply 클릭 없이도 스스로 닫히는 경우가 있다. 그런 경우 Apply를
    # 계속 찾으면 실패하므로, 먼저 팝오버가 이미 닫혔는지부터 확인한다.
    popover_still_open = _find_in_popover(frame, "열기", exact=False, timeout_ms=1000) is not None
    if popover_still_open:
        apply_btn = _find_in_popover(frame, "Apply", exact=False, timeout_ms=6000)
        if apply_btn is None:
            apply_btn = _find_in_popover(frame, "적용", exact=False, timeout_ms=3000)
        if apply_btn is not None:
            try:
                apply_btn.click(timeout=6000)
            except Exception:
                logger.warning(
                    "상태 필터 Apply 버튼 클릭에 실패했습니다 (%s). 팝오버가 이미 닫혔을 수 있어 계속 진행합니다.",
                    case_status,
                )
        else:
            logger.warning("상태 필터의 적용/Apply 버튼을 찾지 못했습니다. 계속 진행합니다.")
            page.keyboard.press("Escape")

    # Apply 이후(또는 자동 반영 이후) 목록이 새로고침되는데, 이게 끝나기 전에 다음
    # 필터를 조작하면 캘린더/플라이아웃 상태가 꼬이므로 팝오버가 닫히고 네트워크가
    # 가라앉을 때까지 기다린다.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if _find_in_popover(frame, "열기", exact=False, timeout_ms=300) is None:
            break
    try:
        page.wait_for_load_state("networkidle", timeout=2500)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(500)
    logger.info("상태 필터 적용: case_status=%s (완료됨 체크=%s)", case_status, want_closed)


_MONTH_NAMES_EN = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

_YEAR_CHANGE_LABEL_RE = re.compile(r"^(\d{4}), change year$")


def _find_button_by_aria_label(container, exact_label: str = None, label_pattern=None):
    """container 안의 <button> 을 순회하며 aria-label 로 찾는다. 이 앱의 커스텀 캘린더
    위젯은 get_by_role() 의 접근성 이름 계산과 잘 맞물리지 않아(실제 aria-label이 있어도
    매칭 실패) DOM 속성을 직접 읽는 방식을 쓴다."""
    try:
        buttons = container.locator("button")
        n = buttons.count()
    except Exception:
        return None
    for i in range(n):
        btn = buttons.nth(i)
        try:
            label = (btn.get_attribute("aria-label") or "").strip()
        except Exception:
            continue
        if exact_label is not None and label == exact_label:
            return btn
        if label_pattern is not None and label_pattern.match(label):
            return btn
    return None


def _select_calendar_date(
    page: Page, frame, open_button_text: str, target: date, timeout_ms: int, max_attempts: int = 3
) -> bool:
    """날짜 선택 버튼(예: '시작 날짜 선택')을 열고 캘린더에서 target 날짜를 클릭한다.
    같은 해로는 신뢰도 높게 이동하지만, 캘린더의 연도 이동 UI는 실제 확인이 부족해
    다른 해로 넘어가야 하는 경우 최선을 다해 시도하고 실패 시 경고만 남긴다.
    클릭 자체는 성공해도 화면에 반영이 안 되는 경우가 있어(대형 목록 로딩과 겹칠 때 등),
    버튼의 placeholder 문구가 실제 날짜로 바뀌었는지 확인하고, 안 바뀌었으면 재시도한다."""
    for attempt in range(1, max_attempts + 1):
        open_btn = _find_in_popover(frame, open_button_text, exact=False, timeout_ms=timeout_ms)
        if open_btn is None:
            logger.warning("'%s' 버튼을 찾지 못했습니다.", open_button_text)
            return False
        open_btn.click()
        page.wait_for_timeout(700)

        cal = _last_visible_popover(frame)
        if cal is None:
            logger.warning("날짜 선택 캘린더를 찾지 못했습니다 (%d번째 시도).", attempt)
            page.keyboard.press("Escape")
            page.wait_for_timeout(400)
            continue

        year_btn = _find_button_by_aria_label(cal, label_pattern=_YEAR_CHANGE_LABEL_RE)
        if year_btn is not None:
            try:
                label = year_btn.get_attribute("aria-label") or ""
                current_year = int(_YEAR_CHANGE_LABEL_RE.match(label).group(1))
            except Exception:
                current_year = target.year
            if current_year != target.year:
                year_btn.click()
                page.wait_for_timeout(600)
                cal = _last_visible_popover(frame) or cal
                year_option = _find_button_by_aria_label(cal, exact_label=str(target.year))
                if year_option is not None:
                    year_option.click()
                    page.wait_for_timeout(600)
                    cal = _last_visible_popover(frame) or cal
                else:
                    logger.warning(
                        "캘린더에서 %s년을 직접 찾지 못했습니다 (현재 %s년 표시 중). "
                        "화면에 보이는 연도로 계속 진행합니다.", target.year, current_year,
                    )

        month_name = _MONTH_NAMES_EN[target.month - 1]
        month_btn = _find_button_by_aria_label(cal, exact_label=month_name)
        if month_btn is not None:
            month_btn.click()
            page.wait_for_timeout(500)
            cal = _last_visible_popover(frame) or cal

        day_label = f"{target.day}, {month_name}, {target.year}"
        day_btn = _find_button_by_aria_label(cal, exact_label=day_label)
        if day_btn is None:
            logger.warning(
                "캘린더에서 '%s' 날짜를 찾지 못했습니다 (%d번째 시도).", day_label, attempt
            )
            page.keyboard.press("Escape")
            page.wait_for_timeout(400)
            continue
        day_btn.click()
        page.wait_for_timeout(600)

        # 실제로 선택이 반영됐는지 확인: placeholder 문구("...선택")가 그대로 남아있으면
        # 클릭이 화면에 반영되지 않은 것이므로 다시 시도한다.
        still_placeholder = _find_in_popover(frame, open_button_text, exact=False, timeout_ms=800)
        if still_placeholder is None:
            return True
        logger.warning(
            "'%s' 선택이 화면에 반영되지 않았습니다 (%d/%d번째 시도). 다시 시도합니다.",
            open_button_text, attempt, max_attempts,
        )
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)

    return False


def _apply_created_date_range_filter(page: Page, config: AppConfig, start_date_str: str, end_date_str: Optional[str]):
    """필터 추가 > 만들어짐 > 사용자 지정 범위 > 절대 날짜 로 생성일 날짜 범위를 설정한다.
    시작일은 사용자가 입력한 날짜, 종료일은 없으면 오늘로 처리한다."""
    timeout_ms = config.crawler.navigation_timeout_ms
    start = _parse_date(start_date_str)
    end = _parse_date(end_date_str) if end_date_str else None
    end = end or date.today()
    if not start:
        logger.warning("시작일을 해석하지 못해 생성일 화면 필터를 적용하지 않습니다: %s", start_date_str)
        return

    frame, add_filter = _find_in_frames_with_frame(page, "필터 추가", exact=False, timeout_ms=timeout_ms)
    if add_filter is None:
        logger.warning("'필터 추가' 버튼을 찾지 못해 생성일 화면 필터를 적용하지 못했습니다.")
        return
    add_filter.click()
    page.wait_for_timeout(700)

    created = _find_in_popover(frame, "만들어짐", exact=True, timeout_ms=5000)
    if created is None:
        logger.warning("'필터 추가' 메뉴에서 '만들어짐' 항목을 찾지 못했습니다.")
        page.keyboard.press("Escape")
        return
    created.click()
    page.wait_for_timeout(700)

    custom_range = _find_in_popover(frame, "사용자 지정 범위", exact=False, timeout_ms=5000)
    if custom_range is not None:
        custom_range.click()
        page.wait_for_timeout(500)

    ok_start = _select_calendar_date(page, frame, "시작 날짜 선택", start, timeout_ms)
    ok_end = _select_calendar_date(page, frame, "종료 날짜 선택", end, timeout_ms)

    apply_btn = _find_in_popover(frame, "적용", exact=False, timeout_ms=4000)
    if apply_btn is not None:
        apply_btn.click()
        page.wait_for_timeout(800)
        if ok_start and ok_end:
            logger.info("생성일 화면 필터 적용: %s ~ %s", start, end)
        else:
            logger.warning("생성일 화면 필터를 적용했지만 시작/종료 날짜 선택이 일부 실패했을 수 있습니다.")
    else:
        logger.warning("생성일 필터의 '적용' 버튼을 찾지 못했습니다.")
        page.keyboard.press("Escape")


def export_case_csv(page: Page, context: BrowserContext, config: AppConfig) -> Path:
    """'CSV로 다운로드' 버튼을 눌러 목록을 파일로 받아온다.
    상태/생성일 필터는 collect_tickets() 에서 _apply_status_filter() /
    _apply_created_date_range_filter() 로 이미 적용된 상태이므로 여기서는 건드리지 않는다."""
    case_list_cfg = config.selectors["case_list"]
    button_text = case_list_cfg["csv_download_button_text"]

    button = _find_locator_in_frames(page, button_text, exact=False, timeout_ms=config.crawler.navigation_timeout_ms)
    if button is None:
        raise PageStructureError(
            f"'{button_text}' 버튼을 찾지 못했습니다. 화면 구조가 바뀌었을 수 있으니 "
            f"config/selectors.json 의 csv_download_button_text 를 확인하세요."
        )

    try:
        with page.expect_download(timeout=config.crawler.download_timeout_ms) as download_info:
            button.click()
        download = download_info.value
    except PlaywrightTimeoutError as exc:
        raise CrawlerError(
            "CSV 다운로드가 시간 내에 시작되지 않았습니다. 네트워크 상태나 화면 로딩 지연을 확인하세요."
        ) from exc

    dest = _download_dir(config) / download.suggested_filename
    download.save_as(str(dest))
    logger.info("케이스 목록 CSV 다운로드 완료: %s", dest)
    return dest


def export_case_csv_with_retry(
    page: Page, context: BrowserContext, config: AppConfig, max_attempts: int = 3
) -> Path:
    """export_case_csv() 를 재시도와 함께 실행한다. 상태 필터를 "완료됨"처럼 결과가
    수천 건인 값으로 바꾸면 서버 쪽 CSV 생성 자체가 오래 걸려 간헐적으로 타임아웃이
    나는 경우가 있어, 실패하면 잠시 기다렸다가 다시 시도한다."""
    last_exc: Optional[Exception] = None
    for attempt in range(1, max_attempts + 1):
        try:
            return export_case_csv(page, context, config)
        except CrawlerError as exc:
            last_exc = exc
            if attempt < max_attempts:
                logger.warning(
                    "CSV 다운로드 실패 (%d/%d번째 시도): %s. 잠시 후 다시 시도합니다.",
                    attempt, max_attempts, exc,
                )
                page.wait_for_timeout(3000)
            else:
                logger.warning("CSV 다운로드가 %d번 시도 모두 실패했습니다.", max_attempts)
    raise last_exc


def _read_csv_rows(csv_path: Path) -> list[dict]:
    """MS 계열 다운로드는 종종 BOM(utf-8-sig) 또는 cp949 인코딩을 쓰므로 순차 시도한다."""
    last_exc = None
    for encoding in ("utf-8-sig", "cp949", "utf-8"):
        try:
            with open(csv_path, "r", encoding=encoding, newline="") as f:
                reader = csv.DictReader(f)
                rows = [dict(row) for row in reader]
            return rows
        except (UnicodeDecodeError, csv.Error) as exc:
            last_exc = exc
            continue
    raise PageStructureError(f"CSV 파일을 읽을 수 없습니다 ({csv_path}): {last_exc}")


def parse_case_csv(csv_path: Path, column_map: dict) -> list[dict]:
    raw_rows = _read_csv_rows(csv_path)
    if not raw_rows:
        return []

    header = list(raw_rows[0].keys())
    unmapped = [h for h in header if h not in column_map]
    if unmapped:
        logger.warning(
            "CSV 헤더 중 매핑되지 않은 컬럼이 있습니다 (config/column_map.json 확인 필요): %s",
            unmapped,
        )

    tickets = []
    for row in raw_rows:
        ticket = {}
        for header_name, value in row.items():
            field_name = column_map.get(header_name)
            if field_name:
                ticket[field_name] = (value or "").strip()
        tickets.append(ticket)
    return tickets


_DATE_PATTERNS = [
    "%Y-%m-%d", "%Y.%m.%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S", "%Y.%m.%d %H:%M",
    "%m/%d/%Y", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M",
]


def _parse_date(value: str) -> Optional[date]:
    if not value:
        return None
    value = value.strip()
    for pattern in _DATE_PATTERNS:
        try:
            return datetime.strptime(value[: len(pattern) + 2], pattern).date()
        except ValueError:
            continue
    match = re.match(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})", value)
    if match:
        y, m, d = map(int, match.groups())
        try:
            return date(y, m, d)
        except ValueError:
            return None
    return None


def filter_by_date_range(
    tickets: list[dict],
    mode: str,
    start_date: Optional[str],
    end_date: Optional[str],
    date_field: str = "created_at",
) -> list[dict]:
    """date_field: "created_at"(만들어짐 기준) 또는 "modified_at"(업데이트됨 기준)."""
    if mode == "all":
        return tickets

    if mode == "today":
        today = date.today()
        result = []
        for t in tickets:
            d = _parse_date(t.get(date_field, ""))
            if d == today:
                result.append(t)
        return result

    if mode == "range":
        start = _parse_date(start_date) if start_date else None
        end = _parse_date(end_date) if end_date else None
        result = []
        for t in tickets:
            d = _parse_date(t.get(date_field, ""))
            if d is None:
                continue
            if start and d < start:
                continue
            if end and d > end:
                continue
            result.append(t)
        return result

    raise ValueError(f"알 수 없는 조회 모드: {mode}")


def _extract_value_near_label(page: Page, label_text: str) -> Optional[str]:
    """라벨 텍스트 다음에 오는 값을 시도해서 읽어온다 (구조가 라벨/값 쌍이라고 가정하는 근사치).
    - 라벨은 정확히 일치(exact=True)하는 요소만 찾는다. 부분일치로 찾으면 "문제 세부 정보"
      같은 섹션 제목이 "문제" 라벨보다 먼저 걸려서 완전히 다른(훨씬 큰) 블록을 읽어오게 된다.
    - 라벨 바로 다음 형제가 ":" 같은 구분자 요소인 레이아웃도 있어서, 값처럼 보이는 텍스트가
      나올 때까지 몇 단계 더 following 요소를 시도한다.
    상세 화면 구조를 정확히 알게 되면 config/selectors.json 을 채워 더 정확한 셀렉터로 교체하세요."""
    try:
        # 상세 페이지는 이 함수 호출 전에 이미 networkidle/domcontentloaded 대기를 거쳤으므로,
        # 실제로 존재하는 라벨은 거의 즉시 잡힌다 — 여기 타임아웃은 주로 "이 케이스 유형에는
        # 없는 항목"을 확인하는 데 걸리는 시간이라, 필드 개수(최대 11개)만큼 곱해지면 커진다.
        label = _find_locator_in_frames(page, label_text, exact=True, timeout_ms=1200)
        if label is None or not label.is_visible(timeout=500):
            return None
        for n in (1, 2):
            try:
                sibling = label.locator(f"xpath=following::*[{n}]")
                text = _strip_icon_glyphs(sibling.inner_text(timeout=1500).strip())
            except Exception:
                continue
            if text and text not in (":", "："):
                # "설명"처럼 여러 문단(Question/Answer 여러 쌍)으로 된 값은 화면에 비동기로
                # 이어서 채워지는 경우가 드물게 있어서, 첫 non-empty 값만 보고 그대로 반환하면
                # 아직 다 안 채워진 값(앞부분 몇 문단만)을 캡처할 위험이 있다(실제로 "설명"
                # 필드가 앞부분 질문/답변 2개만 등록된 사례로 확인됨). 짧은 한 줄짜리 값은
                # 이 위험이 없으니 그대로 반환하고, 긴/여러 줄 값만 안정될 때까지 재확인한다.
                if len(text) > 200 or "\n" in text:
                    return _wait_for_stable_inner_text(page, sibling, text)
                return text
        return None
    except Exception:
        return None


def _wait_for_stable_inner_text(page: Page, locator, initial_text: str, timeout_s: float = 3.0) -> str:
    """긴/여러 줄 값이 더 이어서 채워지지 않는지 짧게 재확인한다. 텍스트 길이가 그대로인
    것을 두 번 연속 확인하면 다 채워진 것으로 보고 반환한다."""
    last = initial_text
    stable_count = 0
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline and stable_count < 2:
        page.wait_for_timeout(300)
        try:
            current = _strip_icon_glyphs(locator.inner_text(timeout=800).strip())
        except Exception:
            break
        if current == last:
            stable_count += 1
        else:
            stable_count = 0
            last = current
    return last


def _scroll_list_frames(page: Page):
    """가상화된(virtualized) 목록 화면에서 아직 렌더링 안 된 다음 행들이 나타나도록 모든
    프레임을 아래로 스크롤한다. 문서 자체가 스크롤되는 경우와, 행을 감싸는 내부 컨테이너가
    따로 스크롤되는(문서 자체는 안 움직이는) 가상 목록 그리드 두 경우를 모두 시도한다 —
    스크롤이 안 되는 프레임에서는 그냥 무시하고 계속한다."""
    for frame in _iter_frames(page):
        try:
            frame.evaluate("window.scrollBy(0, 500)")
        except Exception:
            pass
        try:
            frame.evaluate(
                "() => { document.querySelectorAll(\"[role='row']\").forEach(el => { "
                "let node = el.parentElement; "
                "for (let i = 0; i < 6 && node; i++) { "
                "if (node.scrollHeight > node.clientHeight + 4) { node.scrollTop += 500; break; } "
                "node = node.parentElement; } }); }"
            )
        except Exception:
            pass


def _click_case_row_by_title(page: Page, title: str, timeout_ms: int) -> bool:
    """목록 화면에서 제목 텍스트로 케이스 행을 찾아 클릭한다 (URL을 추측하지 않는다).

    목록이 가상화된 그리드라서 대상 행이 현재 화면/DOM에 아예 없을 수 있다 — 그런 경우
    가만히 기다리기만 해서는 timeout_ms 를 통째로 날리고도 절대 못 찾는다. 그래서 못
    찾을 때마다 목록을 아래로 스크롤해 다음 배치를 렌더링시키고 다시 찾기를 반복한다."""
    if not title:
        return False
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        link = _find_locator_in_frames(page, title, exact=True, timeout_ms=400)
        if link is not None:
            try:
                link.click()
                return True
            except Exception:
                return False
        if time.monotonic() >= deadline:
            return False
        _scroll_list_frames(page)
        page.wait_for_timeout(300)


def _strip_icon_glyphs(text: str) -> str:
    """아이콘 폰트(Fluent UI 등)가 텍스트로 함께 추출되는 유니코드 개인 사용 영역(PUA)
    글리프를 제거한다. 실제 내용(문장/이메일 본문)에는 이 영역 문자가 쓰이지 않으므로
    제거해도 데이터 손실이 없다."""
    if not text:
        return text
    cleaned = "".join(ch for ch in text if not (0xE000 <= ord(ch) <= 0xF8FF))
    return cleaned.strip()


_KOREAN_DATETIME_RE = re.compile(r"(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\.\s*(오전|오후)\s*(\d{1,2}):(\d{2})")


def _parse_korean_datetime(text: str) -> Optional[datetime]:
    """'2026. 6. 30. 오전 09:39' 같은 커뮤니케이션 목록의 날짜 문구를 datetime 으로 변환한다."""
    if not text:
        return None
    m = _KOREAN_DATETIME_RE.search(text)
    if not m:
        return None
    year, month, day, ampm, hour, minute = m.groups()
    hour = int(hour)
    if ampm == "오후" and hour != 12:
        hour += 12
    elif ampm == "오전" and hour == 12:
        hour = 0
    try:
        return datetime(int(year), int(month), int(day), hour, int(minute))
    except ValueError:
        return None


def _find_communication_frame(page: Page, timeout_ms: int):
    """'커뮤니케이션' 탭을 클릭한 뒤, 실제 목록("원본" 컬럼)이 들어있는 프레임을 찾는다.
    이 화면도 다른 화면들처럼 여러 iframe 중 하나에 목록이 들어있다."""
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        for frame in _iter_frames(page):
            try:
                if frame.get_by_text("원본", exact=True).count() > 0:
                    return frame
            except Exception:
                continue
        if time.monotonic() >= deadline:
            return None
        page.wait_for_timeout(300)


def _click_load_more_until_done(frame, page: Page, max_clicks: int = 200) -> int:
    """'더 로드' 버튼이 사라지거나 비활성화될 때까지 반복 클릭해서 전체 커뮤니케이션
    목록을 로드한다.

    클릭 직후 목록이 다시 그려지는 짧은 순간에는 '더 로드' 버튼이 일시적으로 화면에서
    사라지는데(로딩 스켈레톤으로 교체됨), 예전 코드는 클릭 후 900ms 만 고정으로 기다리고
    바로 버튼 유무를 확인해서, 아직 실제로는 더 불러올 항목이 남아 있는데도 "다 불러왔다"고
    착각하고 일찍 멈추는 버그가 있었다(실제로 8건 중 5건만 로드된 채 멈춘 사례로 확인됨).
    그래서 클릭 후에는 데이터 행 개수가 더 이상 늘지 않고 안정될 때까지 기다린 다음에야
    버튼이 남아있는지 다시 확인한다."""
    def _data_row_count() -> int:
        return frame.locator("[data-automation-key='source']").count()

    clicks = 0
    while clicks < max_clicks:
        try:
            load_more = frame.get_by_text("더 로드", exact=False).first
            if load_more.count() == 0 or not load_more.is_visible(timeout=500):
                break
        except Exception:
            break
        try:
            if (load_more.get_attribute("aria-disabled") or "").lower() == "true":
                break
        except Exception:
            pass

        before = _data_row_count()
        try:
            load_more.scroll_into_view_if_needed(timeout=3000)
            load_more.click(timeout=3000)
        except Exception:
            break
        clicks += 1

        # 행 개수가 늘어나는 동안은 계속 대기 시간을 늘려가며 기다리고, 4초간 더 늘지
        # 않으면 이번 클릭으로 인한 로딩이 끝난 것으로 본다(예전엔 8초였는데, 실제 로딩은
        # 훨씬 빨리 끝나서 매번 안정 확인만으로 낭비되는 시간이 컸다 — 그래도 완전히 없애지
        # 않고 4초를 남겨서, 이 값을 도입한 계기였던 "일부만 로드된 채 멈추는" 문제는 계속 방지한다).
        last_count = before
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            page.wait_for_timeout(400)
            current = _data_row_count()
            if current > last_count:
                last_count = current
                deadline = time.monotonic() + 4.0
    return clicks



def _list_communication_entries(frame) -> list[tuple[str, str, Optional[datetime]]]:
    """커뮤니케이션 목록의 '원본'/'마지막 업데이트' 행을 모두 읽어 (발신자, 업데이트 문구,
    파싱된 일시) 목록으로 반환한다."""
    rows = frame.locator("[role='row']")
    entries = []
    for i in range(rows.count()):
        row = rows.nth(i)
        src_cell = row.locator("[data-automation-key='source']")
        upd_cell = row.locator("[data-automation-key='updated']")
        if src_cell.count() == 0 or upd_cell.count() == 0:
            continue
        try:
            src_text = _strip_icon_glyphs(src_cell.inner_text(timeout=800).strip())
            upd_text = upd_cell.inner_text(timeout=800).strip()
        except Exception:
            continue
        entries.append((src_text, upd_text, _parse_korean_datetime(upd_text)))
    return entries


def _wait_for_communication_body_ready(page: Page, frame, timeout_s: float = 6.0) -> str:
    """행을 클릭한 뒤 읽기 창 본문을 곧바로 한 번만 읽으면, 아직 "메시지:" 이하 실제 내용은
    로드되기 전이고 주변 UI(요청 ID/심각도/목록 등)만 채워진 상태를 그대로 캡처해버리는
    문제가 실제로 있었다(노트에 이메일 주소/필드 목록만 남고 실제 대화 내용은 통째로 빠짐).
    "메시지:" 라벨이 나타나고 텍스트 길이가 한 번 더 안정될 때까지 짧게 재확인한다. 못
    찾으면(진짜로 대화 내용이 없는 케이스일 수 있음) timeout_s 후 마지막으로 읽은 값을
    그대로 반환한다."""
    deadline = time.monotonic() + timeout_s
    last = ""
    stable_since_ready = False
    while time.monotonic() < deadline:
        try:
            current = frame.locator("body").inner_text(timeout=1500)
        except Exception:
            current = last
        ready = "메시지:" in current
        if ready and current == last:
            stable_since_ready = True
        else:
            stable_since_ready = False
        last = current
        if ready and stable_since_ready:
            return current
        page.wait_for_timeout(300)
    return last


def _fetch_one_communication_body(
    frame, page: Page, src_text: str, upd_text: str, timeout_ms: int, strip_quote: bool = True
) -> Optional[str]:
    """목록에서 (src_text, upd_text) 에 해당하는 '원본' 행을 다시 찾아 클릭해서 본문만
    반환한다(못 찾거나 실패하면 None). get_single_communication() 과
    collect_latest_communication_thread() 이 공유하는 "행 하나 열어서 본문 읽기" 로직.

    strip_quote=False 면 이 메시지 본문 아래에 인용된 이전 메시지까지 그대로 남겨서
    반환한다(최신 메시지 하나로 전체 대화를 복원하는 collect_latest_communication_thread()
    가 이 원문을 그대로 필요로 하기 때문)."""
    row_locator = None
    rows = frame.locator("[role='row']")
    for i in range(rows.count()):
        row = rows.nth(i)
        src_cell = row.locator("[data-automation-key='source']")
        upd_cell = row.locator("[data-automation-key='updated']")
        if src_cell.count() == 0 or upd_cell.count() == 0:
            continue
        try:
            if (
                _strip_icon_glyphs(src_cell.inner_text(timeout=500).strip()) == src_text
                and upd_cell.inner_text(timeout=500).strip() == upd_text
            ):
                row_locator = src_cell
                break
        except Exception:
            continue
    if row_locator is None:
        logger.warning("커뮤니케이션 행을 다시 찾지 못했습니다 (source=%s, updated=%s)", src_text, upd_text)
        return None

    try:
        row_locator.click(timeout=timeout_ms)
    except Exception as exc:
        logger.warning("커뮤니케이션 항목 클릭 실패 (source=%s): %s", src_text, exc)
        return None
    try:
        page.wait_for_load_state("networkidle", timeout=2500)
    except PlaywrightTimeoutError:
        pass

    try:
        full_text = _wait_for_communication_body_ready(page, frame)
    except Exception as exc:
        logger.warning("커뮤니케이션 본문을 읽지 못했습니다 (source=%s): %s", src_text, exc)
        full_text = ""

    marker = "더 로드"
    idx = full_text.rfind(marker)
    body_text = full_text[idx + len(marker):].strip() if idx != -1 else full_text
    body_text = _strip_icon_glyphs(body_text)
    if not strip_quote:
        return body_text
    # 이 메시지 자신의 내용만 남기고, 그 아래 인용된 이전 메시지들은 잘라낸다.
    return communication_html.own_message_body(body_text)


def get_full_communication_thread(page: Page, config: AppConfig, title: str) -> list[dict]:
    """목록 화면에 이미 해당 케이스가 보이는 상태에서, 제목으로 클릭해 들어가 커뮤니케이션
    전체 스레드(가장 최신 행에 인용되어 함께 들어있는 이전 대화까지 전부)를 읽고 다시
    목록으로 돌아온다. get_single_communication() 이 최신 답장 자신의 새 내용 1건만 남기는
    것과 달리, 이 함수는 그 답장에 인용된 이전 대화까지 전부 포함한 메시지 목록을 그대로
    돌려준다 — 완료 케이스 최종 노트에 최신 답장 하나만이 아니라 대화 전체를 남기고 싶을 때
    쓴다."""
    timeout_ms = config.crawler.navigation_timeout_ms
    if not _click_case_row_by_title(page, title, timeout_ms):
        logger.warning("커뮤니케이션 전체 조회: 목록에서 '%s' 케이스를 찾지 못했습니다.", title)
        return []
    try:
        return collect_latest_communication_thread(page, config)
    finally:
        _navigate_back_to_case_list(page, config)


def collect_latest_communication_thread(page: Page, config: AppConfig) -> list[dict]:
    """현재 열려 있는 케이스 상세 화면의 '커뮤니케이션' 탭에서 가장 최신 날짜 행 하나만
    열어 원문(인용 부분을 자르지 않은 그대로)을 읽은 뒤, 그 안에 인용되어 함께 들어있는
    이전 메시지들을 '보낸 사람:'/'보낸 날짜:' 인용 헤더 기준으로 나눠 대화 주고받은
    형식으로 재구성한다. 답장 메일은 그 아래에 이전 대화가 전부 인용되어 들어있으므로,
    행마다 하나씩 직접 클릭하는 것보다 대화가 많은 케이스에서 훨씬 빠르다.

    반환값의 형식(order/source/updated_at/body, 오래된 것부터)은 이 함수 도입 전
    행마다 직접 클릭해 모으던 방식과 동일해서 호출부는 그대로 재사용할 수 있다."""
    timeout_ms = config.crawler.navigation_timeout_ms

    comm_tab = _find_locator_in_frames(page, "커뮤니케이션", exact=False, timeout_ms=timeout_ms)
    if comm_tab is None:
        logger.warning("'커뮤니케이션' 탭을 찾지 못했습니다.")
        return []
    comm_tab.click()
    page.wait_for_timeout(800)

    frame = _find_communication_frame(page, timeout_ms)
    if frame is None:
        logger.info("커뮤니케이션 목록을 찾지 못했습니다 (커뮤니케이션 내역이 없을 수 있음).")
        return []

    def _wait_for_data_rows(timeout_s: float = 8.0) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if frame.locator("[data-automation-key='source']").count() > 0:
                return True
            page.wait_for_timeout(400)
        return False

    _wait_for_data_rows()
    clicks = _click_load_more_until_done(frame, page)
    if clicks > 0:
        _wait_for_data_rows()

    entries = _list_communication_entries(frame)
    if not entries:
        return []
    entries.sort(key=lambda e: (e[2] is None, e[2] or datetime.min))
    src_text, upd_text, _ = entries[-1]

    raw_body = _fetch_one_communication_body(frame, page, src_text, upd_text, timeout_ms, strip_quote=False)
    if raw_body is None:
        return []
    return communication_html.split_latest_message_into_thread(raw_body, src_text, upd_text)


def _navigate_back_to_case_list(page: Page, config: AppConfig, max_backs: int = 8):
    """상세/커뮤니케이션 화면에서 목록 화면으로 돌아간다. 커뮤니케이션 탭까지 들어갔다
    나오는 경우 히스토리가 여러 단계라서, 목록의 CSV 다운로드 버튼이 보일 때까지
    뒤로가기를 반복한다. 상세/커뮤니케이션 화면은 URL에 항상 "case/"가 들어있고 목록
    화면은 그렇지 않으므로, URL 조건도 같이 확인해야 상세 화면에 남아있는 오래된 프레임의
    잔여 텍스트를 목록으로 잘못 인식하는 걸 막을 수 있다. 상태/날짜 필터는 이미 화면에
    적용된 상태이므로 여기서는 건드리지 않는다 (필터를 다시 지우면 이후 케이스들이
    잘못된 목록에서 찾아지게 된다)."""
    for _ in range(max_backs):
        if "case/" not in page.url and _find_locator_in_frames(
            page, "CSV로 다운로드", exact=False, timeout_ms=1500
        ) is not None:
            break
        try:
            page.go_back(wait_until="domcontentloaded", timeout=config.crawler.navigation_timeout_ms)
        except Exception:
            break
        page.wait_for_timeout(300)
    try:
        page.wait_for_load_state("networkidle", timeout=1500)
    except PlaywrightTimeoutError:
        pass


def enrich_ticket_from_detail_page(
    page: Page, ticket: dict, config: AppConfig, do_freshdesk: bool = False,
    cached_communication_messages: Optional[list] = None,
) -> dict:
    """목록에서 케이스 제목을 클릭해 상세 페이지로 들어가 항목을 모두 읽어온 뒤,
    다시 목록 화면으로 돌아간다. 어떤 필드를 어떤 라벨로 찾을지는 config/selectors.json 의
    case_detail.field_labels 에서 관리하므로, 화면에 항목이 추가/변경되어도 이 함수는
    고칠 필요가 없다. 실패해도 전체 실행을 막지 않고, 해당 필드만 비워둔 채 경고를 남긴다.

    cached_communication_messages 가 주어지면(이미 케이스 목록에 있던 완료된 케이스가
    modified_at 변경으로 다시 열린 경우) 커뮤니케이션 전체를 다시 재구성하지 않는다 —
    가장 최근 업데이트 1건만 열어서, 이미 저장돼 있던 대화 뒤에 새 메시지만 이어붙인다
    (같은 메시지가 이미 마지막에 있으면 중복으로 보고 건너뛴다). 처음 수집하는 케이스거나
    캐시가 없으면 기존처럼 최신 메시지 하나로 전체 스레드를 복원한다."""
    title = ticket.get("title", "")
    timeout_ms = config.crawler.navigation_timeout_ms
    url_before_click = page.url

    if not _click_case_row_by_title(page, title, timeout_ms):
        logger.warning("목록에서 '%s' 제목의 케이스를 찾지 못해 상세 보강을 건너뜁니다.", title)
        return ticket

    try:
        # 상세 화면은 URL의 해시(#...)만 바뀌는 SPA 전환이라 domcontentloaded 는 곧바로
        # 반환될 수 있고, 그 시점엔 아직 page.url 이 갱신되기 전일 수 있다. URL이 실제로
        # 바뀔 때까지 짧게 대기한다.
        deadline = time.monotonic() + timeout_ms / 1000
        while page.url == url_before_click and time.monotonic() < deadline:
            page.wait_for_timeout(200)
        page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        ticket["case_url"] = page.url

        # 상세 화면의 각 항목은 비동기로 채워지는 경우가 많다(로딩 스켈레톤 표시).
        # 이 포탈은 백그라운드 폴링/텔레메트리가 계속 돌아서 완전한 networkidle 에 거의
        # 도달하지 못한다 — 어차피 타임아웃돼도 그냥 진행하므로(catch), 짧게만 시도한다
        # (예전엔 navigation_timeout_ms(30초)를 통째로 기다려서 티켓당 큰 지연의 주범이었다).
        try:
            page.wait_for_load_state("networkidle", timeout=2000)
        except PlaywrightTimeoutError:
            pass

        detail_cfg = config.selectors.get("case_detail", {})
        field_labels = detail_cfg.get("field_labels", {})
        for field_name, label_text in field_labels.items():
            if ticket.get(field_name) or not label_text:
                continue
            value = _extract_value_near_label(page, label_text)
            if value:
                ticket[field_name] = value
            else:
                logger.debug(
                    "상세 페이지에서 '%s' 값을 찾지 못했습니다 (case=%s)", field_name, ticket.get("ms_case_id")
                )

        messages = None
        if cached_communication_messages:
            # 이미 케이스 목록에 있던 완료된 케이스 — 전체 재구성 대신, 커뮤니케이션 목록의
            # 가장 최근 업데이트 1건(맨 위 행)만 클릭해서 그 대화 내용만 가져와 기존 대화
            # 뒤에 이어붙인다.
            try:
                latest = get_single_communication(page, config, title, position="latest")
            except Exception as exc:
                latest = None
                logger.warning(
                    "최신 커뮤니케이션 조회 중 오류 (case=%s): %s", ticket.get("ms_case_id"), exc, exc_info=True
                )
            messages = list(cached_communication_messages)
            if latest:
                last = messages[-1] if messages else None
                is_duplicate = (
                    last is not None
                    and last.get("sender_email") == (latest.get("source") or None)
                    and last.get("sent") == (latest.get("updated_at") or None)
                )
                if not is_duplicate:
                    next_order = (last.get("order", len(messages)) + 1) if last else 1
                    messages.append(
                        {
                            "order": next_order,
                            "sender_name": None,
                            "sender_email": latest.get("source") or None,
                            "sent": latest.get("updated_at") or None,
                            "body_html": html.escape(latest.get("body", "")).replace("\n", "<br>"),
                        }
                    )
        else:
            # 가장 최신 커뮤니케이션 행 하나만 열어서, 그 안에 인용된 이전 대화까지 함께
            # 복원한다(답장 메일은 그 아래에 이전 메시지가 전부 인용되어 들어있으므로 행마다
            # 직접 클릭하지 않아도 전체 대화가 나온다 — 대화가 많은 케이스에서 훨씬 빠르다).
            # Freshdesk 등록 여부와 무관하게 항상 수집해서 DB(communication_messages, JSON)에
            # 저장해두면 웹 UI의 케이스 상세 화면에서 언제든 대화 내역을 대화형으로 볼 수 있다.
            try:
                raw_messages = collect_latest_communication_thread(page, config)
            except Exception as exc:
                raw_messages = []
                logger.warning(
                    "커뮤니케이션 수집 중 오류 (case=%s): %s", ticket.get("ms_case_id"), exc, exc_info=True
                )
            if raw_messages:
                messages = [
                    {
                        "order": m["order"],
                        "sender_name": None,
                        "sender_email": m.get("source") or None,
                        "sent": m.get("updated_at") or None,
                        "body_html": html.escape(m.get("body", "")).replace("\n", "<br>"),
                    }
                    for m in raw_messages
                ]

        if messages:
            # Freshdesk 대화 노트 등록용(DB 에는 저장 안 되는 임시 필드).
            ticket["_communication_messages"] = messages
            # 웹 UI 케이스 상세 화면 + 다음 실행에서도 다시 볼 수 있도록 DB 에 저장.
            try:
                ticket["communication_messages"] = json.dumps(messages, ensure_ascii=False)
            except Exception as exc:
                logger.warning(
                    "커뮤니케이션 메시지 직렬화 실패 (case=%s): %s", ticket.get("ms_case_id"), exc, exc_info=True
                )

            if not do_freshdesk:
                # Freshdesk 에 등록하는 실행에서는 HTML 파일 저장을 생략한다(같은 내용이
                # 대화 노트로 그대로 등록되므로 별도 파일이 중복 산출물이 되기 때문).
                try:
                    dest = communication_html.save_communication_html(
                        config.excel_dir, ticket.get("ms_case_id", ""), ticket.get("title", ""), messages
                    )
                    ticket["communication_html_path"] = str(dest)
                except Exception as exc:
                    logger.warning(
                        "커뮤니케이션 HTML 저장 실패 (case=%s): %s", ticket.get("ms_case_id"), exc, exc_info=True
                    )
    finally:
        _navigate_back_to_case_list(page, config)

    return ticket


def _collect_for_single_status(
    page: Page,
    context: BrowserContext,
    config: AppConfig,
    mode: str,
    start_date: Optional[str],
    end_date: Optional[str],
    single_status: str,
    date_basis: str,
    warnings: list[str],
    do_freshdesk: bool = False,
    existing_tickets: Optional[dict] = None,
) -> list[dict]:
    """단일 상태(open 또는 closed)로 목록을 조회하고 상세까지 보강한다.
    "모두" 처리는 이 함수를 open/closed 각각에 대해 두 번 호출하는 상위 로직에서 담당한다."""
    try:
        _apply_status_filter(page, config, single_status)
    except Exception as exc:
        warnings.append(f"상태 필터 적용 실패({single_status}): {exc}")
        logger.warning("상태 필터 적용 중 오류: %s", exc, exc_info=True)

    date_field = "modified_at" if date_basis == "updated" else "created_at"

    # "만들어짐" 기준일 때만 화면의 날짜 필터(필터추가>만들어짐>사용자 지정 범위)를 쓴다.
    # "업데이트됨" 기준은 화면에 해당 필터가 없으므로 상태만 걸어서 받은 뒤, 목록의
    # "업데이트됨" 값을 기준으로 파이썬에서 날짜 범위를 걸러낸다.
    if date_basis == "created" and mode == "range" and start_date:
        try:
            _apply_created_date_range_filter(page, config, start_date, end_date)
        except Exception as exc:
            warnings.append(f"생성일 화면 필터 적용 실패: {exc}")
            logger.warning("생성일 화면 필터 적용 중 오류: %s", exc, exc_info=True)

    csv_path = export_case_csv_with_retry(page, context, config)
    all_tickets = parse_case_csv(csv_path, config.column_map)
    logger.info(
        "CSV에서 총 %d건 파싱됨 (날짜 필터 전, status=%s)", len(all_tickets), single_status
    )

    filtered = filter_by_date_range(all_tickets, mode, start_date, end_date, date_field=date_field)
    total = len(filtered)
    logger.info(
        "날짜 필터(%s 기준) 적용 후 %d건 (상세 보강 대상, status=%s)", date_field, total, single_status
    )

    if config.crawler.enable_detail_page_enrichment:
        for i, t in enumerate(filtered, start=1):
            if total:
                job_status.set_progress(
                    f"상세 정보 수집 중 ({i}/{total})", 10 + round(i / total * 60)
                )
            cached = (existing_tickets or {}).get((t.get("ms_case_id") or "").strip())
            # communication_messages 가 비어있으면(NULL) "실제로 메시지가 0개였다"와
            # "지난번 수집이 실패/누락됐다"를 구분할 방법이 없다 — 안전하게 후자로 간주해서
            # 캐시를 믿지 않고 다시 수집한다(대화 내역이 통째로 비어 보이는 문제 방지).
            if (
                cached
                and cached.get("modified_at")
                and cached["modified_at"] == t.get("modified_at")
                and cached.get("communication_messages")
            ):
                for field in DETAIL_ENRICHMENT_FIELDS:
                    if cached.get(field):
                        t[field] = cached[field]
                logger.info(
                    "변경 없음(modified_at 동일) 확인되어 상세 보강 건너뜀 %d/%d (case=%s)",
                    i, total, t.get("ms_case_id"),
                )
                continue
            # 완료된 케이스이면서 이미 케이스 목록에 있던(캐시가 존재하는) 경우에는, 전체
            # 커뮤니케이션을 다시 재구성하지 않고 가장 최근 업데이트 1건만 기존 대화 뒤에
            # 이어붙인다(신규 케이스거나 이전에 저장된 대화가 없으면 기존처럼 전체 복원).
            cached_messages = None
            if single_status == "closed" and cached and cached.get("communication_messages"):
                try:
                    cached_messages = json.loads(cached["communication_messages"])
                except Exception:
                    cached_messages = None
            try:
                filtered[i - 1] = enrich_ticket_from_detail_page(
                    page, t, config, do_freshdesk, cached_communication_messages=cached_messages,
                )
                logger.info("상세 보강 진행 %d/%d 완료 (case=%s)", i, total, t.get("ms_case_id"))
            except Exception as exc:
                warnings.append(f"상세 보강 실패 (case={t.get('ms_case_id')}): {exc}")
                logger.warning("상세 보강 중 오류: %s", exc, exc_info=True)

    return filtered


def collect_tickets(
    config: AppConfig,
    mode: str,
    start_date: Optional[str],
    end_date: Optional[str],
    case_status: str = "open",
    date_basis: str = "created",
    do_freshdesk: bool = False,
    existing_tickets: Optional[dict] = None,
) -> tuple[list[dict], list[str]]:
    """전체 수집 파이프라인. (tickets, warnings) 를 반환한다.

    case_status="all" 이면 화면에 "모두" 옵션이 따로 없으므로, "열기"로 한 번,
    "완료됨"으로 한 번 각각 조회한 뒤 Microsoft Case ID 기준으로 중복을 제거해서
    합친다 (상세 보강은 각 상태로 목록이 필터된 상태에서 제목을 클릭해야 하므로,
    두 조회를 합친 뒤 한꺼번에 보강할 수 없고 상태별로 따로 진행해야 한다).

    existing_tickets: storage.TicketStore.get_enrichment_cache() 로 만든
    {ms_case_id: {"modified_at": ..., 상세 필드...}} 매핑. CSV의 modified_at 이
    여기 값과 같은 케이스는 케이스 내용이 그대로라는 뜻이므로, 상세 페이지를 다시
    열지 않고 이 값을 재사용한다(가장 느린 단계인 상세 보강을 건너뛰어 속도를 크게
    높인다)."""
    if date_basis not in ("created", "updated"):
        date_basis = "created"

    statuses_to_query = ["open", "closed"] if case_status == "all" else [case_status]

    warnings: list[str] = []
    pw = context = page = None
    try:
        pw, context, page = open_browser_context(config)
        ensure_logged_in(page, config)

        merged: list[dict] = []
        seen_case_ids: set[str] = set()
        for single_status in statuses_to_query:
            tickets = _collect_for_single_status(
                page, context, config, mode, start_date, end_date, single_status, date_basis, warnings,
                do_freshdesk, existing_tickets,
            )
            for t in tickets:
                case_id = (t.get("ms_case_id") or "").strip()
                if case_id:
                    if case_id in seen_case_ids:
                        continue
                    seen_case_ids.add(case_id)
                merged.append(t)

        return merged, warnings
    finally:
        try:
            if context:
                context.close()
        finally:
            if pw:
                pw.stop()


def list_case_ids_by_status(page: Page, context: BrowserContext, config: AppConfig, status: str) -> dict[str, str]:
    """상태 필터만 적용해서 {케이스ID: 제목} 매핑만 빠르게 받아온다(상세 페이지 보강 없음).
    예약 실행의 완료 케이스 감지처럼 "이 케이스가 지금 이 상태 목록에 있는지"만 필요할 때 쓴다
    — collect_tickets() 처럼 한 건씩 상세 페이지를 여는 비용 없이 CSV 한 번으로 끝난다. 제목도
    같이 주는 이유는, 이후 get_latest_communication_for_case() 가 목록에서 케이스를 다시 찾아
    클릭할 때 제목이 필요하기 때문이다."""
    _apply_status_filter(page, config, status)
    csv_path = export_case_csv_with_retry(page, context, config)
    tickets = parse_case_csv(csv_path, config.column_map)
    return {
        t["ms_case_id"].strip(): t.get("title", "")
        for t in tickets
        if t.get("ms_case_id", "").strip()
    }


def get_single_communication(
    page: Page, config: AppConfig, title: str, position: str = "latest"
) -> Optional[dict]:
    """목록 화면에 이미 해당 케이스가 보이는 상태에서, 제목으로 클릭해 들어가 커뮤니케이션
    중 딱 하나(position="latest": 가장 최신, "oldest": 가장 오래된)만 읽고 다시 목록으로
    돌아온다(없으면 None). 완료 케이스 최종 노트(최신 1건) 용으로 쓴다.

    행을 전부 클릭해서 다 읽은 뒤 하나만 골라 쓰지 않는다 — 목록의 '마지막 업데이트'
    날짜만으로 대상 행을 먼저 정하고, 그 행 하나만 클릭해서 본문을 읽으므로 대화가 많은
    케이스에서도 훨씬 빠르다."""
    timeout_ms = config.crawler.navigation_timeout_ms
    if not _click_case_row_by_title(page, title, timeout_ms):
        logger.warning("커뮤니케이션 단일 조회: 목록에서 '%s' 케이스를 찾지 못했습니다.", title)
        return None
    try:
        comm_tab = _find_locator_in_frames(page, "커뮤니케이션", exact=False, timeout_ms=timeout_ms)
        if comm_tab is None:
            logger.warning("'커뮤니케이션' 탭을 찾지 못했습니다.")
            return None
        comm_tab.click()
        page.wait_for_timeout(800)

        frame = _find_communication_frame(page, timeout_ms)
        if frame is None:
            logger.info("커뮤니케이션 목록을 찾지 못했습니다 (커뮤니케이션 내역이 없을 수 있음).")
            return None

        def _wait_for_data_rows(timeout_s: float = 8.0) -> bool:
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                if frame.locator("[data-automation-key='source']").count() > 0:
                    return True
                page.wait_for_timeout(400)
            return False

        _wait_for_data_rows()
        clicks = _click_load_more_until_done(frame, page)
        if clicks > 0:
            _wait_for_data_rows()

        entries = _list_communication_entries(frame)
        if not entries:
            return None
        entries.sort(key=lambda e: (e[2] is None, e[2] or datetime.min))  # 오래된 -> 최신
        src_text, upd_text, _ = entries[0] if position == "oldest" else entries[-1]

        own_body = _fetch_one_communication_body(frame, page, src_text, upd_text, timeout_ms)
        if own_body is None:
            return None
        return {"source": src_text, "updated_at": upd_text, "body": own_body}
    finally:
        _navigate_back_to_case_list(page, config)
