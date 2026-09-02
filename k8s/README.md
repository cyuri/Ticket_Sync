# Azure Kubernetes Service(AKS) 배포 가이드

## 0. 전체 그림

이 앱은 두 부분으로 나뉜다 — **한쪽만** AKS로 옮긴다.

1. **웹 대시보드**(`Deployment`, 이 폴더의 매니페스트) — 사람이 브라우저로 접속해 케이스/이력을
   보고, 실행 버튼을 누르고, Freshdesk 연결/예약을 설정하는 화면. Playwright도, VPN도 필요
   없다. **AKS로 옮기는 건 이것뿐이다.**
2. **`local_runner.py`(로컬 실행 에이전트)** — 실제 Engage Center 로그인/크롤링/Freshdesk
   등록을 수행한다. 사내망(VPN) 연결 + 개인 메일 MFA 로그인이 전제라서 **AKS로 옮길 수
   없다** — 지금 이 PC에 그대로 남겨두고 계속 켜둔다.

AKS(퍼블릭 Azure) 파드는 이 PC(사설 네트워크)에 직접 닿을 방법이 없으므로, **Cloudflare
Tunnel**로 이 PC의 포트 2개(local_runner.py:8787, rqlite:4001)를 바깥에서 접속 가능한
HTTPS 주소로 노출한다(방화벽/포트포워딩 불필요, 아웃바운드 연결만 씀). 그리고 지금까지
`data/tickets.db`(SQLite 파일)를 웹 컨테이너와 local_runner.py가 로컬 폴더 마운트로 같이
보던 것을, **rqlite**(SQLite 엔진을 그대로 쓰면서 HTTP로 네트워크 접속을 지원하는 오픈소스)
로 바꿔서 양쪽이 같은 rqlite에 붙게 한다.

```
[사용자 브라우저] --HTTPS--> [AKS: ticket-sync Deployment]
                                    │
                                    │ TICKET_SYNC_LOCAL_RUNNER_URL, TICKET_SYNC_RQLITE_URL
                                    ▼
                          [Cloudflare Tunnel 엣지]
                                    │ (이 PC의 아웃바운드 연결로 유지되는 터널)
                                    ▼
                [이 Windows PC]
                  ├─ local_runner.py :8787  (VPN 연결, 실제 Engage Center 크롤링)
                  └─ rqlite(docker)  :4001  (tickets.db 대신 — 양쪽이 공유하는 DB)
```

`docker-compose.yml`(로컬에서 `docker compose up`) 은 지금처럼 SQLite 파일 모드 그대로
쓸 수 있다 — rqlite/터널은 AKS 배포를 실제로 쓸 때만 필요하다.

## 1. 로그인 세션 만들기 (변경 없음)

```powershell
cd ticket_sync
python main.py
```

대화형 마법사에서 아무 조건이나 골라 한 번 실행하면, 실제 브라우저 창이 뜨고 평소처럼
로그인 + MFA를 완료해 `data/browser_profile`에 세션이 저장된다. 이건 지금까지와 완전히
동일하고, **AKS 배포와 무관하게 이 PC에서만** 필요하다(local_runner.py가 이 세션을 그대로
재사용한다). 세션이 만료되면 이 절차를 다시 한 번만 반복하면 된다 — 재배포/재시작 불필요.

## 2. 이 PC에 rqlite 띄우기

인증 파일을 하나 만든다(`k8s/rqlite-auth.json`, Git에 커밋하지 않는다):

```json
[
  {"username": "ticket-sync", "password": "REPLACE_ME", "perms": ["all"]}
]
```

띄운다(최초 1회):

```powershell
docker run -d --name ticket-sync-rqlite --restart unless-stopped `
  -p 4001:4001 `
  -v ${PWD}\data\rqlite:/rqlite/file `
  -v ${PWD}\k8s\rqlite-auth.json:/rqlite/auth.json:ro `
  rqlite/rqlite -auth /rqlite/auth.json
```

단일 노드라 별도 클러스터 구성은 필요 없다(내결함성은 없음 — 지금 SQLite 파일 하나와 같은
위험 수준). `data/rqlite/`에 실제 데이터가 쌓이므로, 지금 있는 `data/tickets.db`를 그대로
옮기고 싶다면 아래 방법 중 하나로 스키마+데이터를 rqlite에 넣어주면 된다(가장 간단한 방법:
이 PC에서 `TICKET_SYNC_DB_BACKEND=rqlite`로 앱을 한 번 띄우면 `storage.py`가 스키마는
자동으로 만든다 — 기존 데이터를 그대로 이어받고 싶으면 `.db` 파일을 rqlite로 옮기는 별도
마이그레이션 스크립트가 필요하니, 새로 시작해도 괜찮은 경우가 아니면 먼저 상의하세요).

## 3. 이 PC에 Cloudflare Tunnel 띄우기

```powershell
winget install --id Cloudflare.cloudflared
cloudflared tunnel login
cloudflared tunnel create ticket-sync
```

터널 설정 파일(예: `%USERPROFILE%\.cloudflared\config.yml`)에 두 라우트를 등록한다:

```yaml
tunnel: ticket-sync
credentials-file: C:\Users\<사용자>\.cloudflared\<tunnel-id>.json

ingress:
  - hostname: runner.example.com
    service: http://localhost:8787
  - hostname: rqlite.example.com
    service: http://localhost:4001
  - service: http_status:404
```

DNS 연결 후 실행(계속 켜둔다 — local_runner.py와 마찬가지로 이 PC에 상주):

```powershell
cloudflared tunnel route dns ticket-sync runner.example.com
cloudflared tunnel route dns ticket-sync rqlite.example.com
cloudflared tunnel run ticket-sync
```

## 4. local_runner.py 에 인증/rqlite 설정 추가

`local_runner.py`는 지금까지 인증이 전혀 없었다(같은 PC 안에서만 호출됐으니 안전했음).
터널로 외부에 노출하므로, 이 PC의 `.env`에 아래를 추가한다:

```
TICKET_SYNC_RUNNER_TOKEN=<AKS Secret과 동일한 값>
TICKET_SYNC_DB_BACKEND=rqlite
TICKET_SYNC_RQLITE_URL=http://localhost:4001
TICKET_SYNC_RQLITE_USER=ticket-sync
TICKET_SYNC_RQLITE_PASSWORD=<위 rqlite-auth.json과 동일한 값>
```

세션 만료 알림(선택 기능, 실제 로그인/실행이 이 PC에서 일어나므로 여기 설정한다 —
K8s Secret이 아니다)도 원하면 같은 `.env`에 추가:

```
NOTIFY_WEBHOOK_URL=...   # Slack/Teams/Discord Incoming Webhook (아무거나 호환)
NOTIFY_SMTP_HOST=... / NOTIFY_SMTP_PORT=... / NOTIFY_SMTP_USER=... / NOTIFY_SMTP_PASSWORD=...
NOTIFY_EMAIL_TO=...
```

`local_runner.py`를 재시작(`dist/RestartLocalRunner.exe` 또는 직접 재실행)하면 반영된다.

## 5. 사전 준비 (kubectl/레지스트리)

- 이 이미지를 올릴 컨테이너 레지스트리(ACR 권장 — `az acr create`)
- `kubectl` 이 대상 AKS 클러스터를 가리키고 있는 상태(`az aks get-credentials`)

## 6. 이미지 빌드 & 푸시

```bash
az acr login --name <레지스트리이름>
docker build -t <레지스트리이름>.azurecr.io/ticket-sync:latest .
docker push <레지스트리이름>.azurecr.io/ticket-sync:latest
```

`k8s/deployment.yaml`의 `image:` 값을 방금 푸시한 주소로 바꾼다.

이 단계부터는 **CI/CD로 자동화해도 되는 부분**이다 — `.github/workflows/build-and-deploy.yml`
참고(GitHub 저장소 Settings > Secrets 에 `REGISTRY_USERNAME`/`REGISTRY_PASSWORD`/
`KUBE_CONFIG` 등록, `env.IMAGE_NAME`을 ACR 주소로 변경).

## 7. Secret/ConfigMap 준비 후 배포

`k8s/secret.example.yaml`을 복사해 `k8s/secret.yaml`로 만들고, 3번에서 정한
`TICKET_SYNC_RUNNER_TOKEN`/`TICKET_SYNC_RQLITE_USER`/`TICKET_SYNC_RQLITE_PASSWORD`를
그대로 채운다(이 파일은 Git에 커밋하지 않는다). `k8s/deployment.yaml`의
`TICKET_SYNC_LOCAL_RUNNER_URL`/`TICKET_SYNC_RQLITE_URL`도 3번에서 만든 실제 터널
호스트네임으로 바꾼다.

```bash
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/secret.yaml
kubectl apply -f k8s/deployment.yaml
kubectl apply -f k8s/service.yaml
```

```bash
kubectl port-forward svc/ticket-sync 8000:80
# 브라우저에서 http://localhost:8000 접속(실제로는 LoadBalancer/Ingress로 접속 — service.yaml 참고)
```

## 8. 확인

- `kubectl get pods` → `ticket-sync` 파드가 `Running`.
- 대시보드 접속 후 케이스 목록이 보이는지(=rqlite로 DB를 정상적으로 공유하는지 확인).
- "지금 실행" 버튼을 눌러 실제로 이 PC의 local_runner.py에서 실행되는지(=터널+토큰 인증
  확인). 실패하면: (a) 터널이 켜져 있는지, (b) `TICKET_SYNC_RUNNER_TOKEN`이 양쪽에 같은지,
  (c) `kubectl logs deployment/ticket-sync`로 오류 메시지 확인.

## 9. 자동 배치는 어떻게 도나?

K8s `CronJob`은 쓰지 않는다 — 파드는 애초에 Engage Center에 접근할 수 없어서(VPN 없음)
직접 크롤링하는 CronJob은 성립하지 않는다. 대신 웹 UI의 `/schedule` 화면에서 켜는 **앱
내장 스케줄러**(`scheduler.py`, APScheduler)가 지금처럼 그대로 동작한다 — 정해진 시각이
되면 이 스케줄러가 (파드 안에서) 타이머 역할만 하고, 실제 실행은 터널 너머 local_runner.py
에게 위임한다. 웹 파드가 떠 있는 동안에만 스케줄이 동작한다.

## 10. 알려진 제약사항

`/download/{run_id}`(Excel 내보내기 다운로드)는 로컬 `output/` 폴더의 파일을 직접 읽는데,
지금은 그 파일이 이 PC(local_runner.py가 실행한 결과)에만 있고 AKS 파드는 접근할 수 없다.
**이번 배포 범위에서는 고치지 않았다** — 필요해지면 local_runner.py에 파일을 스트리밍해
주는 프록시 엔드포인트를 추가하는 후속 작업으로 남겨둔다. 그 전까지는 AKS에서 띄운 화면의
Excel 다운로드 버튼은 실패한다.

## 11. CI/CD를 써야 할까?

**결론: 이미지 빌드/배포에는 쓰고, 실제 로그인/동기화 실행에는 쓰지 않는다** —
지금까지와 동일한 원칙이다. 로그인(MFA)과 실제 실행은 이 PC의 local_runner.py 몫이고,
CI/CD는 "코드가 바뀌면 이미지를 새로 빌드해 레지스트리에 올리고 Deployment가 그 이미지를
쓰도록 갱신"하는 역할만 한다(`.github/workflows/build-and-deploy.yml`).

## 알아둘 점

- **웹 대시보드(Deployment)는 항상 파드 1개만 띄운다** (`replicas: 1`) — 예약 스케줄러가
  파드마다 중복 등록되는 걸 피하기 위함.
- rqlite/Cloudflare Tunnel/local_runner.py 는 전부 **이 PC에서 계속 켜둬야** 하는 상주
  프로세스다 — 이 PC를 끄면 AKS의 웹 UI는 뜨지만 "지금 실행"/케이스 조회 등 실제 기능이
  전부 안 된다(터널/DB가 죽어있으므로).
- `TICKET_SYNC_HEADLESS`는 이제 이 파드와 무관하다(파드가 Playwright를 직접 안 씀) —
  local_runner.py 쪽 설정(`config.json`)만 신경 쓰면 된다.
