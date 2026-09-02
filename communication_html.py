"""케이스별 커뮤니케이션(메일/대화) 내용을 사람이 읽기 좋은 HTML 파일로 저장한다.
원본 내용은 요약/수정 없이 그대로 보존하고, 메시지 단위로 카드 형태로 보여준다.

메시지 목록은 crawler.collect_latest_communication_thread() 가 만든, 이미 메시지
단위로 분리된 리스트를 그대로 받는다 — 가장 최신 커뮤니케이션 행 하나의 원문(답장
메일 특유의 인용 구조)을 split_latest_message_into_thread() 로 여러 메시지로 쪼갠
결과다."""
from __future__ import annotations

import html
import re
from pathlib import Path

_INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]')

_OUTLOOK_TIP_RE = re.compile(
    r"^(?:You don't often get email from.*?Learn why this is important"
    r"|\S+에게서 전자 메일을 받지 못하는 경우가 많습니다\.\s*이 문제가 중요한 이유)"
    r"\n\t?\n?",
    re.DOTALL,
)


def safe_case_filename(ms_case_id: str) -> str:
    case_id = (ms_case_id or "unknown").strip()
    case_id = _INVALID_FILENAME_CHARS.sub("_", case_id) or "unknown"
    return f"case_{case_id}_communication.html"


_QUOTE_HEADER_START_RE = re.compile(
    r"(?m)^(?=(?:From|보낸\s*사람)\s*[:：])"
)

# 인용 헤더 블록 맨 앞의 발신자/날짜 줄만 뽑아낸다(받는 사람/참조/제목 줄은 그대로 두고
# 본문에서 제외만 한다 — 순서가 어긋나거나 일부 줄이 없어도 발신자/날짜만 있으면 동작).
_QUOTE_SENDER_RE = re.compile(r"^(?:From|보낸\s*사람)\s*[:：]\s*(.*)$", re.MULTILINE)
_QUOTE_SENT_RE = re.compile(r"^(?:Sent|보낸\s*날짜)\s*[:：]\s*(.*)$", re.MULTILINE)
_QUOTE_META_LINE_RE = re.compile(
    r"^(?:From|보낸\s*사람|Sent|보낸\s*날짜|To|받는\s*사람|Cc|참조|Subject|제목)\s*[:：].*$\n?",
    re.MULTILINE,
)


def _split_thread_blocks(body: str) -> list[str]:
    """이메일 스레드 본문을 'From: ...'(영문) 또는 '보낸 사람: ...'(한글, Outlook 한국어
    UI의 인용 헤더 표기) 로 시작하는 블록 단위로 나눈다. 구분에 실패해도 전체를 하나의
    블록으로 다뤄 내용 손실은 없게 한다."""
    if not body:
        return []
    parts = _QUOTE_HEADER_START_RE.split(body)
    return [p.strip() for p in parts if p.strip()]


def _strip_top_noise(text: str) -> str:
    """읽기 화면 맨 위에 섞여 들어오는 목록/필터 UI 텍스트("새로 고침", "요청 ID", "심각도",
    "상태" 같은 항목 라벨과 "원본"/"마지막 업데이트" 열 제목, 발신자·날짜 행)를 제거하고
    실제 메시지 본문만 남긴다.

    이 UI 텍스트 바로 뒤에는 항상 "메시지:" 라벨이 붙고, 그 다음부터가 진짜 내용이다 —
    예전에는 "외부 메시지가 모든 사용자에게 표시됩니다." 라는 Outlook 외부 발신자 경고
    문구를 먼저 찾은 뒤에만 "메시지:" 를 찾았는데, 이 경고 문구는 발신자에 따라 아예 안 뜨는
    경우가 있어서(실제로 실운영 데이터에서 확인됨) 그럴 때는 앞의 UI 텍스트가 전혀 제거되지
    않고 그대로 저장되는 문제가 있었다. "메시지:" 만으로 바로 찾도록 고쳤다.

    "메시지:" 를 못 찾으면(예: 인용된 이전 메시지처럼 이 UI 틀 없이 본문만 있는 경우)
    내용 손실을 막기 위해 원본을 그대로 반환한다."""
    msg_pos = text.find("메시지:")
    if msg_pos == -1:
        return text
    after = text[msg_pos + len("메시지:"):]
    after = _OUTLOOK_TIP_RE.sub("", after.lstrip("\t\n "), count=1)
    return after.strip() or text


def own_message_body(body: str) -> str:
    """개별 커뮤니케이션 행을 열었을 때 나오는 본문에서, 그 아래 인용되어 함께 보이는
    이전 메시지들('From:'/'보낸 사람:' 으로 시작하는 부분)을 잘라내고 이 메시지 자신의
    새 내용만 남긴다. 완료 케이스 최종 노트(get_single_communication)처럼 그 메시지
    자신의 새 내용만 필요할 때 쓴다 — 이전 대화까지 포함해 전체 스레드를 복원하려면
    split_latest_message_into_thread() 를 쓴다."""
    blocks = _split_thread_blocks(body)
    own = blocks[0] if blocks else body
    return _strip_top_noise(own)


def _parse_quoted_block(block: str) -> tuple[str, str, str]:
    """인용 블록 맨 앞의 '보낸 사람:'/'보낸 날짜:' 줄에서 발신자·날짜를 뽑아내고,
    '받는 사람:'/'참조:'/'제목:' 등 나머지 메타 줄은 본문에서만 제거한다(발신자/날짜가
    아니라도 화면에 그대로 보이면 대화 카드 본문이 메타 정보로 시작해 지저분해지기 때문).
    발신자 줄을 못 찾으면(형식이 다르거나 실패) 통째로 본문으로 두고 발신자/날짜는 빈 값."""
    sender_match = _QUOTE_SENDER_RE.search(block)
    sender = sender_match.group(1).strip() if sender_match else ""
    sent_match = _QUOTE_SENT_RE.search(block)
    sent = sent_match.group(1).strip() if sent_match else ""
    body = _QUOTE_META_LINE_RE.sub("", block, count=5).strip()
    return sender, sent, body


def split_latest_message_into_thread(
    raw_body: str, latest_source: str, latest_updated: str
) -> list[dict]:
    """가장 최신 커뮤니케이션 행 하나의 원문(인용 부분을 잘라내지 않은 그대로)을 받아,
    답장 특유의 '구분선 아래 이전 메일 인용' 구조를 이용해 여러 메시지를 주고받은 대화
    형식으로 재구성한다. Engage Center의 최신 메일 본문은 그 아래에 이전 대화가 전부
    인용되어 함께 들어있으므로(각 인용 블록은 '보낸 사람:'/'보낸 날짜:' 로 시작), 행마다
    직접 열어보지 않고도 최신 메시지 하나만으로 전체 대화를 복원할 수 있다.

    반환값은 collect_all_communications() 와 동일한 형식
    (order/source/updated_at/body, 오래된 것부터)이라 호출부를 그대로 재사용할 수 있다."""
    raw_body = _strip_top_noise(raw_body)
    blocks = _split_thread_blocks(raw_body)
    if not blocks:
        return []

    # blocks[0] = 인용 헤더 없는 가장 최신 답장 자신의 새 내용 — 목록 행 자체의
    # 발신자/업데이트 날짜를 그대로 쓴다.
    parsed = [{"source": latest_source, "updated_at": latest_updated, "body": blocks[0]}]
    # blocks[1:] 은 각각 '보낸 사람:'/'보낸 날짜:' 인용 헤더로 시작하는 이전 메시지들.
    for block in blocks[1:]:
        sender, sent, own_body = _parse_quoted_block(block)
        parsed.append({"source": sender, "updated_at": sent, "body": own_body})

    parsed.reverse()  # 오래된 것부터 순서대로
    for order, m in enumerate(parsed, start=1):
        m["order"] = order
    return parsed


def render_communication_html(ms_case_id: str, title: str, messages: list[dict]) -> str:
    """이미 메시지 단위로 분리된 리스트(오래된 것부터 순서대로, 각 항목은
    order/sender_email/sent/body_html 를 가짐)를 카드형 타임라인 HTML 문서로 렌더링한다."""
    cards = []
    for msg in reversed(messages):  # 화면에는 최신 메시지가 맨 위 카드로 보이게 한다
        meta_bits = [b for b in (msg.get("sender_email"), msg.get("sent")) if b]
        meta_html = (
            f'<div class="thread-card-meta">{html.escape(" · ".join(meta_bits))}</div>' if meta_bits else ""
        )
        cards.append(
            f'<div class="thread-card">'
            f'<div class="thread-card-index">{msg.get("order", "")}</div>'
            f"{meta_html}"
            f'<div class="thread-card-body">{msg.get("body_html", "")}</div>'
            f"</div>"
        )
    cards_html = "\n".join(cards) if cards else '<p class="empty">커뮤니케이션 내역 없음</p>'

    latest = messages[-1] if messages else {}

    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>{html.escape(title or ms_case_id or "커뮤니케이션")}</title>
<style>
  :root {{ color-scheme: light dark; }}
  * {{ box-sizing: border-box; }}
  body {{
    font-family: "Segoe UI", "Malgun Gothic", sans-serif;
    max-width: 900px;
    margin: 0 auto;
    padding: 32px 20px 80px;
    background: #f5f6f8;
    color: #1b1b1f;
    line-height: 1.65;
  }}
  header {{
    margin-bottom: 24px;
    padding-bottom: 16px;
    border-bottom: 2px solid #d8dbe0;
  }}
  h1 {{ font-size: 1.35rem; margin: 0 0 10px; word-break: break-word; }}
  .meta {{ display: flex; flex-wrap: wrap; gap: 16px; font-size: 0.88rem; color: #4b4f56; }}
  .meta span b {{ color: #1b1b1f; }}
  main {{ position: relative; padding-left: 8px; }}
  .thread-card {{
    background: #fff;
    border: 1px solid #e1e3e8;
    border-radius: 10px;
    padding: 22px 24px 20px;
    margin: 18px 0 18px 20px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.06);
    position: relative;
  }}
  .thread-card::before {{
    content: "";
    position: absolute;
    left: -21px;
    top: 0;
    bottom: -18px;
    width: 2px;
    background: #dfe2e7;
  }}
  .thread-card:last-child::before {{ bottom: 50%; }}
  .thread-card-index {{
    position: absolute;
    top: 18px;
    left: -34px;
    width: 26px;
    height: 26px;
    border-radius: 50%;
    background: #2b579a;
    color: #fff;
    font-size: 0.78rem;
    font-weight: 600;
    display: flex;
    align-items: center;
    justify-content: center;
  }}
  .thread-card-meta {{ font-size: 0.78rem; color: #6b7280; margin-bottom: 8px; }}
  .thread-card-body {{
    font-size: 0.92rem;
    white-space: normal;
    word-break: break-word;
  }}
  .empty {{ color: #888; font-style: italic; }}
  @media (prefers-color-scheme: dark) {{
    body {{ background: #1b1c1f; color: #e7e7ea; }}
    header {{ border-bottom-color: #33353a; }}
    .meta {{ color: #a9adb5; }}
    .meta span b {{ color: #e7e7ea; }}
    .thread-card {{ background: #26272b; border-color: #33353a; box-shadow: none; }}
    .thread-card::before {{ background: #3a3c42; }}
    .thread-card-meta {{ color: #9aa0ab; }}
  }}
</style>
</head>
<body>
<header>
  <h1>{html.escape(title or "(제목 없음)")}</h1>
  <div class="meta">
    <span>케이스 번호: <b>{html.escape(ms_case_id or "-")}</b></span>
    <span>최근 발신자: <b>{html.escape(latest.get("sender_email") or "-")}</b></span>
    <span>마지막 업데이트: <b>{html.escape(latest.get("sent") or "-")}</b></span>
  </div>
</header>
<main>
{cards_html}
</main>
</body>
</html>
"""


def save_communication_html(output_dir: Path, ms_case_id: str, title: str, messages: list[dict]) -> Path:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    dest = output_dir / safe_case_filename(ms_case_id)
    content = render_communication_html(ms_case_id, title, messages)
    dest.write_text(content, encoding="utf-8")
    return dest
