# Buffer 조회 한도 조사 및 개선 (2026-09-16)

- 실제 응답: HTTP 429, RateLimit `250-in-1day` remaining 0, `100-in-15min` remaining 98. 최근 24시간 한도 소진을 확인했다. 최초 Retry-After 12248초, 후속 확인 11745초.
- 코드 문제: 서버 시작 직후 및 매시간 전체 게시물을 1건당 1회 조회. 발행 성공도 반복 조회. 조회 실패는 예약 상태 unknown으로 덮어쓰고 작업 자체는 succeeded로 기록했다.
- 로컬 예약 기록은 8개 영상 × 3채널 = 24건. 기존 구조로 24시간 계속 켜두면 자동 조회만 최대 576회이며 시작 시 추가 실행도 있었다. 현재 서버 기록에는 전날 UTC 기준 자동 조회 작업 6회, 당일 2회가 있어 자동 실행을 확인했다. 전체 API 호출 이력이 없어 250회 전부를 이 기기에 귀속할 수는 없다.
- 개선: 공식 GraphQL alias 방식으로 최대 30건을 한 HTTP 요청으로 조회. 24건은 한 요청이다. 자동 확인은 발행 시각 전 scheduled와 완료된 sent를 제외하며 최근 1시간 조회 시도는 저장된 시각을 기준으로 제외한다.
- 실패 시 기존 상태를 유지하고 오류만 기록한 뒤 작업 실패로 보고한다. 원격 예약 변경이나 자동 재전송은 하지 않는다.
- 검증: publishing/workflow 28개 통과. 24건 단일 요청 구조, 완료/미래/최근 자동 조회 생략, 조회 실패 시 상태·미디어 보존 확인. 제한 중이므로 실제 묶음 조회 성공은 미검증.
- 공식 근거: https://developers.buffer.com/guides/api-limits.html (rolling 24h 및 응답 헤더), https://developers.buffer.com/guides/efficient-api-usage.html (post aliases 최대30개, 여러 조회를 한 요청으로 합산).
