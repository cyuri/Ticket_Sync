# Engage Center -> Freshdesk 티켓 동기화 웹 UI 컨테이너 이미지.
#
# 중요: 최초 로그인(MFA)은 이 컨테이너 안에서 할 수 없다(화면이 없음, headless 강제).
# data/browser_profile 에 로컬 PC에서 미리 로그인해 만든 세션을 볼륨으로 넣어줘야 한다.
# 자세한 절차는 k8s/README.md 참고.

FROM python:3.12-slim

# 한글이 포함된 케이스 제목/본문을 다루므로 한글 폰트를 넣어둔다(문제 발생 시 디버그용
# 스크린샷/렌더링에 도움). Playwright 의 Chromium 실행에 필요한 시스템 라이브러리는
# `playwright install --with-deps` 가 알아서 설치한다.
# 컨테이너 기본 시간대는 UTC라 실행 이력이 UTC로 찍힌다 — 한국 시간(KST) 기준으로 맞춘다.
ENV TZ=Asia/Seoul

RUN apt-get update && apt-get install -y --no-install-recommends \
        fonts-nanum \
        curl \
        ca-certificates \
        tzdata \
        libnss3-tools \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# 회사(KT) 네트워크는 SSL 검사 장비(Kt Corporation/Cato Networks)가 자체 루트 인증서로
# HTTPS 트래픽을 가로챈다. 호스트 Windows는 이 인증서를 이미 신뢰하지만 컨테이너 안은
# 비어있어서 pip 다운로드는 SSL 인증서 검증 오류로, Playwright의 Chromium은 실제 사이트
# 접속 시 ERR_CERT_AUTHORITY_INVALID 로 각각 실패한다 — 그래서 빌드 시 이 인증서 묶음을
# (1) OS 신뢰 저장소(pip/curl 용)와 (2) Chromium 이 읽는 NSS 인증서 DB(브라우저 접속용)
# 양쪽에 모두 등록해준다(사내망이 아니면 이 파일이 비어 있어도 무해하게 통과한다).
COPY docker/certs/kt-corp-ca-bundle.crt /usr/local/share/ca-certificates/kt-corp-ca-bundle.crt
RUN update-ca-certificates
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
ENV PIP_CERT=/etc/ssl/certs/ca-certificates.crt
ENV NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt

RUN mkdir -p /root/.pki/nssdb \
    && certutil -N -d sql:/root/.pki/nssdb --empty-password \
    && python3 -c "\
import re; \
certs = re.findall(r'-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----', open('/usr/local/share/ca-certificates/kt-corp-ca-bundle.crt').read(), re.S); \
[open(f'/tmp/kt-cert-{i}.pem', 'w').write(c) for i, c in enumerate(certs)]" \
    && if ls /tmp/kt-cert-*.pem >/dev/null 2>&1; then \
         for f in /tmp/kt-cert-*.pem; do certutil -d sql:/root/.pki/nssdb -A -t "C,," -n "$(basename "$f")" -i "$f"; done; \
       fi \
    && rm -f /tmp/kt-cert-*.pem

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium

COPY . .

# 컨테이너에는 화면이 없으므로 항상 headless 로 강제한다(config.json 설정과 무관하게 우선 적용).
ENV TICKET_SYNC_HEADLESS=true
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD curl -f http://127.0.0.1:8000/healthz || exit 1

CMD ["uvicorn", "webapp:app", "--host", "0.0.0.0", "--port", "8000"]
