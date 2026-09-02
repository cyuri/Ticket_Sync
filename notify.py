"""로그인 세션 만료처럼 사람이 직접 개입해야 하는 상황을 알린다.

로그인은 비밀번호 + 개인 메일로 받는 MFA 2차 인증이 필요해 자동화할 수 없다. 세션이
만료되면(예: K8s 자동 배치 실행 중) 그 사실을 누군가 알아채고 로컬 PC에서 다시 로그인해
세션을 갱신해줘야 한다. 이 모듈은 그 "누군가 알아채야 하는" 순간에 웹훅(Slack/Teams/
Discord 호환) 또는 이메일(SMTP)로 알림을 보낸다.

둘 다 환경 변수로 설정하며, 설정 안 돼 있으면 조용히 아무 것도 하지 않는다(알림은 선택
기능이라 설정을 안 했다고 실행 자체가 막히면 안 된다):

  NOTIFY_WEBHOOK_URL   Slack/Teams/Discord Incoming Webhook URL
  NOTIFY_SMTP_HOST     SMTP 서버 주소 (예: smtp.gmail.com)
  NOTIFY_SMTP_PORT     SMTP 포트 (기본 587, STARTTLS)
  NOTIFY_SMTP_USER     SMTP 로그인 계정
  NOTIFY_SMTP_PASSWORD SMTP 로그인 비밀번호(또는 앱 비밀번호)
  NOTIFY_EMAIL_TO      알림 받을 이메일 주소 (콤마로 여러 개 가능)
"""
from __future__ import annotations

import logging
import os
import smtplib
from email.mime.text import MIMEText

import requests

logger = logging.getLogger("ticket_sync.notify")


def _send_webhook(title: str, message: str) -> bool:
    url = os.getenv("NOTIFY_WEBHOOK_URL")
    if not url:
        return False
    text = f"**{title}**\n{message}"
    # Slack/Teams(Incoming Webhook) 는 "text" 필드를, Discord 는 "content" 필드를 읽는다.
    # 둘 다 같이 보내면 각자 아는 필드만 읽고 모르는 필드는 무시하므로 하나의 페이로드로
    # 세 플랫폼 모두를 커버할 수 있다.
    payload = {"text": text, "content": text}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        logger.info("웹훅 알림 전송 성공")
        return True
    except requests.RequestException as exc:
        logger.warning("웹훅 알림 전송 실패: %s", exc)
        return False


def _send_email(title: str, message: str) -> bool:
    host = os.getenv("NOTIFY_SMTP_HOST")
    to_addrs = os.getenv("NOTIFY_EMAIL_TO")
    if not host or not to_addrs:
        return False
    port = int(os.getenv("NOTIFY_SMTP_PORT", "587"))
    user = os.getenv("NOTIFY_SMTP_USER", "")
    password = os.getenv("NOTIFY_SMTP_PASSWORD", "")
    recipients = [a.strip() for a in to_addrs.split(",") if a.strip()]

    msg = MIMEText(message, "plain", "utf-8")
    msg["Subject"] = title
    msg["From"] = user or "ticket-sync@localhost"
    msg["To"] = ", ".join(recipients)

    try:
        with smtplib.SMTP(host, port, timeout=15) as server:
            server.starttls()
            if user:
                server.login(user, password)
            server.sendmail(msg["From"], recipients, msg.as_string())
        logger.info("이메일 알림 전송 성공 (수신: %s)", recipients)
        return True
    except Exception as exc:
        logger.warning("이메일 알림 전송 실패: %s", exc)
        return False


def notify(message: str, title: str = "Ticket Sync 알림") -> bool:
    """설정된 채널(웹훅 우선, 그 다음 이메일)로 알림을 보낸다. 하나라도 성공하면 True.
    아무 채널도 설정 안 돼 있으면 로그에만 남기고 False 를 반환한다(호출부에서 실행을
    막지 않도록 예외를 던지지 않는다)."""
    sent_webhook = _send_webhook(title, message)
    sent_email = _send_email(title, message)
    if not sent_webhook and not sent_email:
        logger.info("알림 채널이 설정되지 않아 알림을 건너뜁니다 (내용: %s)", message)
    return sent_webhook or sent_email
