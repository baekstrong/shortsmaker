# 사용자 작업 흐름과 자동화 검토

출처: 2026-09-09 사용자 설명. 아래 현재 흐름만 확정이며 자동화 항목은 검토 제안이다.

## 현재 흐름
1. 사용자가 롱폼을 직접 편집한다.
2. 완성된 롱폼을 주제별로 나눈다. 보통 5~8개지만 개수는 고정하지 않는다.
3. 각 구간을 쇼츠화한다. 150% 확대 후 가운데 정렬하고 상단 후킹 메시지를 넣는다.
4. 쇼츠를 각각 인코딩한 뒤 Buffer로 발행 예약한다.

## 자동화 가능한 항목 (제안, 구현 범위 미확정)
- 완성 영상 가져오기·폴더 감지, 음성 전사 및 시간 정보 생성.
- 주제별 구간 제안, 문장 경계 보정, 독립적으로 이해되는지 검사, 중복 구간 검출.
- 세로 템플릿, 확대·정렬, 상단 후킹 문구 후보 생성·배치·줄바꿈.
- 별도 자막 생성은 하지 않는다. 원본에 포함된 자막을 그대로 사용한다.
- 미리보기와 일괄 검수, 구간/문구/화면 위치 수정.
- 개별 인코딩, 파일명·폴더 정리, 실패 재시도, 수정 결과만 재출력.
- 발행 제목·본문·해시태그 초안, 영상 호스팅, Buffer 예약·대기열 등록, 상태 확인 및 중복 등록 방지.
- 성과 기반 개선은 후속 검토. Buffer 지표 API는 실험적이므로 의존 여부를 별도 검증한다.

## Buffer 확인
2026-09-09 공식 문서 기준 예약 시간 지정 및 다음 대기열 슬롯 등록 지원. 영상은 직접 업로드 대신 공개 접근 가능한 URL로 전달한다. 실제 계정·채널 지원과 미디어 제약은 연동 시 확인한다.
- https://developers.buffer.com/guides/posts-and-scheduling.html
- https://support.buffer.com/en-us/articles/what-is-buffers-api-GtIYIQilz5

## 미정
- 실제 영상으로 확대·배치 템플릿의 시각적 일치 확인.
- 주제별 전체 구간 유지 또는 쇼츠 길이로 추가 압축 여부.
- 발행 일정과 Buffer 실계정 검증. 후킹 문구는 사용자 컨펌 필수.

## 확정된 화면·후킹 흐름 (2026-09-09 후속 사용자 설명)
- 가로 롱폼을 세로 쇼츠 화면에 맞춰 넣은 상태에서 150% 확대하고 가운데 정렬해 좌우를 자른다. 기존 영상 자막도 함께 커져 보이게 하는 목적이다. 별도 자막 생성은 불필요하다.
- 영상마다 후킹 문구 약 10개를 제안하고 그중 AI 추천 문구를 표시한다. 사용자가 선택·컨펌해 확정한다. AI 추천만으로 자동 확정하지 않는다.

## Buffer 재검증 (2026-09-09)
- 공식 발표: 새 GraphQL 공개 API는 2026-05-27 발표. 기존 REST API와 구분해야 한다. https://buffer.com/resources/buffer-api-is-here/
- 공식 영상 예제: createPost의 assets.video.url로 영상을 붙이고 addToQueue로 예약 가능. https://developers.buffer.com/examples/create-video-post.html
- 지정 시간 예약: customScheduled와 dueAt 사용. https://developers.buffer.com/guides/posts-and-scheduling.html
- 파일 업로드 API는 없다. 인증 없이 접근 가능한 HTTPS 영상 파일 URL이 필요하며 발행 완료까지 유지해야 한다. 공유 미리보기 링크·만료 URL은 실패 원인이 될 수 있다. https://developers.buffer.com/guides/hosting-media.html
- 코덱·길이·채널 권한 및 연결 상태도 확인 필요. https://support.buffer.com/en-us/articles/troubleshooting-video-uploads-in-buffer-LK0CldlFNB
- 판정: 공식 문서상 영상 예약 지원 확인. 사용자 계정으로 API 요청·실제 발행은 아직 시험하지 않았으므로 운영 가능성 검증 완료가 아니다.
- 사용자에게 과거 API 자동화 실패 경험이 있음. 실패 시점·채널·오류 메시지는 미확인으로 원인을 단정하지 않는다.
- 다음 검증 제안: 계정/채널 조회 → 짧은 영상 예약 → 예약 상태 확인 → 실제 발행 확인. 실발행 시험은 대상과 일정을 합의한 후 수행한다.

## 확정된 AI 설정·발행 채널 (2026-09-09)
출처: 사용자 후속 요청.
- AI 기본 설정은 Astra, 추론 강도 medium으로 한다.
- 모델을 고정하지 않고 사용자가 선택할 수 있도록 한다. 선택 가능한 모델 목록과 실제 연동 식별자는 구현 시 확인한다.
- Buffer 발행 대상은 Instagram과 YouTube만으로 한다. 쇼츠 결과물의 대상 형식은 Instagram Reels 및 YouTube Shorts다.
- 현재는 요구사항 기록 단계이며 모델 연동·Buffer 실계정 발행 검증은 아직 수행하지 않았다.

## Buffer 인증 준비 (2026-09-09)
- 사용자가 API 키 발급 완료를 확인했다. 실제 API 발행 성공을 의미하지 않는다.
- 키는 Git에서 제외되는 로컬 `.env.local`의 `BUFFER_API_KEY`에 저장한다. `.env.example`은 비밀값 없는 설정 예시다.
- 다음 단계는 키 설정 후 읽기 전용 계정·채널 조회. 실제 예약·발행 테스트는 영상과 일정을 정한 뒤 진행한다.

## 2026-09-09 Buffer 실계정 읽기 검증 성공
- 로컬 API 키로 공식 GraphQL API 계정·채널 조회 성공. 키 값은 출력·기록하지 않았다.
- Instagram: easystrength101. YouTube: 백관장_직장인 체력 상담소.
- 두 채널 모두 isQueuePaused=false. 대기열은 일시정지 상태가 아니지만 발행 권한·미디어 처리·실제 발행 성공까지 보장하는 검증은 아니다.
- 예약 생성·게시물 발행은 수행하지 않았다. 다음 단계: 테스트 영상과 발행 일정을 정해 두 채널 예약·실발행 검증.
- 출처: https://api.buffer.com 읽기 전용 account/organizations 및 channels 쿼리 응답(2026-09-09).
