"use strict";
const $ = (id) => document.getElementById(id);
const statusNames = {
  pending: "준비 중",
  creating: "예약 생성 중",
  scheduled: "예약됨",
  sending: "발행 중",
  sent: "발행 완료",
  error: "발행 실패",
  unknown: "확인 필요",
  rejected: "예약 실패",
  cancelled: "취소됨",
};
const esc = (s) =>
  String(s ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
let state = { projects: [], jobs: [], models: [] },
  pid = localStorage.getItem("project"),
  cid = null,
  shownRevision = -1,
  videoProject = null,
  shownBusy = null;
let calendarPlan = null, calendarMonth = "", calendarRequest = 0;
let calendarProject = null;
let channels = [],
  records = [],
  audio = null,
  mutating = false,
  polling = false;
let selectedRegionId = null;
let selectedShotId = null;
let shotTimelineKey = "", shotFrameCache = { key:"", times:[] };
let shotScrub = null;
let clipInteraction = null, clipTimelineKey = "", sourcePreview = false, selectFirstShot = true;
let presentedTime = null;
let trimDraft = null;
let frameDraft = null;
let dragging = null;
let sound = localStorage.getItem("sound") !== "off";
const seen = new Set(JSON.parse(localStorage.getItem("seenJobs") || "[]"));
const project = () => state.projects.find((p) => p.id === pid);
const clip = () => project()?.clips.find((c) => c.id === cid);
const busy = () =>
  state.jobs.some(
    (j) => j.project_id === pid && ["running", "queued"].includes(j.status),
  );
function toast(message) {
  $("toast").textContent = message;
  $("toast").hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => ($("toast").hidden = true), 6500);
}
async function api(path, body) {
  const r = await fetch(
    path,
    body === undefined
      ? {}
      : {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-Shortsmaker": "1" },
          body: JSON.stringify(body),
        },
  ).catch(() => { throw Error("앱 서버에 연결할 수 없습니다. 시작.command를 실행하고 터미널을 열어 두세요. 입력한 문구는 화면에 유지됩니다."); });
  const data = await r.json();
  if (!r.ok) throw Error(data.error || "요청 실패");
  return data;
}
function safe(fn) {
  return async (...args) => {
    try {
      await fn(...args);
    } catch (e) {
      toast(e.message);
    }
  };
}
function time(t) {
  return `${String(Math.floor(t / 60)).padStart(2, "0")}:${String(Math.floor(t % 60)).padStart(2, "0")}`;
}
const savedVolume = Number(localStorage.getItem("soundVolume") ?? 80);
function keptRanges(c) {
  let cursor = c.start;
  const ranges = [];
  for (const cut of c.cuts || []) {
    if (cut.start > cursor) ranges.push({ start: cursor, end: cut.start });
    cursor = cut.end;
  }
  if (cursor < c.end) ranges.push({ start: cursor, end: c.end });
  return ranges;
}
const editedDuration = (c) => keptRanges(c).reduce((total, r) => total + r.end-r.start, 0);
const editedAt = (c, at) => keptRanges(c).reduce((total, r) => total + Math.max(0, Math.min(at, r.end)-r.start), 0);
function playbackStart(c, at) {
  const ranges = keptRanges(c);
  if (at < c.start || at >= ranges.at(-1).end) return ranges[0].start;
  const next = ranges.find(r => at < r.end);
  return Math.max(at, next.start);
}
let soundVolume = Number.isFinite(savedVolume) ? Math.max(10, Math.min(100, savedVolume)) : 80;
const soundPresets = {
  chime: { label: "맑은 3음", type: "sine", notes: [[660, 0.14], [880, 0.14], [1046.5, 0.18]] },
  bell: { label: "초인종", type: "sine", notes: [[830.6, 0.18], [659.3, 0.24]] },
  bright: { label: "경쾌한 멜로디", type: "triangle", notes: [[523.3, 0.1], [659.3, 0.1], [784, 0.1], [1046.5, 0.18]] },
  soft: { label: "차분한 알림", type: "sine", notes: [[440, 0.2], [554.4, 0.24]] },
  signal: { label: "또렷한 신호", type: "triangle", notes: [[880, 0.1], [880, 0.1], [1174.7, 0.16]] },
};
const savedSound = localStorage.getItem("soundPreset");
let soundPreset = Object.hasOwn(soundPresets, savedSound) ? savedSound : "chime";
let soundUntil = 0;
const soundNodes = new Set();
async function unlockAudio() {
  if (!audio) audio = new AudioContext();
  if (audio.state !== "running") await audio.resume();
}
async function beep(preview = false) {
  if (!preview && (!sound || !audio)) return;
  try {
    await unlockAudio();
    if (audio.state !== "running") return;
    if (preview) {
      for (const osc of soundNodes) osc.stop();
      soundNodes.clear();
    } else if (audio.currentTime < soundUntil) return;
    const start = audio.currentTime + 0.02;
    const preset = soundPresets[soundPreset];
    let offset = 0;
    preset.notes.forEach(([frequency, duration]) => {
      const osc = audio.createOscillator(), gain = audio.createGain();
      const at = start + offset;
      offset += duration;
      osc.type = preset.type;
      osc.frequency.value = frequency;
      osc.connect(gain);
      gain.connect(audio.destination);
      const level = 0.4 * (soundVolume / 100);
      gain.gain.setValueAtTime(0, at);
      gain.gain.linearRampToValueAtTime(level, at + 0.008);
      // Keep the same peak volume; finish with a short release and a silent gap.
      gain.gain.setValueAtTime(level, at + duration - 0.055);
      gain.gain.exponentialRampToValueAtTime(0.001, at + duration - 0.02);
      gain.gain.linearRampToValueAtTime(0, at + duration - 0.015);
      soundNodes.add(osc);
      osc.onended = () => { soundNodes.delete(osc); osc.disconnect(); gain.disconnect(); };
      osc.start(at);
      osc.stop(at + duration);
    });
    soundUntil = start + offset;
  } catch (error) {
    if (preview) toast("알림음을 재생하지 못했습니다. 브라우저의 소리 허용 설정을 확인해 주세요.");
  }
}
document.addEventListener("pointerdown", () => { unlockAudio().catch(() => {}); });
document.addEventListener("keydown", () => { unlockAudio().catch(() => {}); });
function renderSound() {
  $("sound").textContent = sound ? "♪ 알림 켜짐" : "♪ 알림 꺼짐";
  $("sound").setAttribute("aria-pressed", String(sound));
  $("sound-preset").value = soundPreset;
  $("sound-volume").value = soundVolume;
  $("sound-volume-value").textContent = `${soundVolume}%`;
}
$("sound").onclick = () => {
  sound = !sound;
  localStorage.setItem("sound", sound ? "on" : "off");
  renderSound();
};
$("sound-volume").oninput = (event) => {
  soundVolume = Number(event.target.value);
  localStorage.setItem("soundVolume", String(soundVolume));
  renderSound();
};
$("sound-preset").innerHTML = Object.entries(soundPresets)
  .map(([value, preset]) => `<option value="${value}">${preset.label}</option>`).join("");
$("sound-preset").onchange = (event) => {
  soundPreset = event.target.value;
  localStorage.setItem("soundPreset", soundPreset);
  beep(true);
};
$("sound-test").onclick = () => beep(true);
renderSound();
async function refresh() {
  if (polling) return;
  polling = true;
  try {
    const next = await api("/api/state");
    next.projects = next.projects.map((p) => {
      const local = state.projects.find((x) => x.id === p.id);
      return local && local.revision > p.revision ? local : p;
    });
    state = next;
    if (!project()) pid = state.projects[0]?.id;
    for (const j of state.jobs) {
      for (const event of j.stage_events || []) {
        if (!seen.has(event.id)) {
          if (refresh.ready) beep();
          seen.add(event.id);
        }
      }
      if (
        ["succeeded", "failed", "cancelled", "interrupted"].includes(
          j.status,
        ) &&
        !seen.has(j.id)
      ) {
        if (refresh.ready) {
          if (!j.stage_events?.length || j.status !== "succeeded") beep();
          if (j.status === "failed") toast(j.message);
        }
        seen.add(j.id);
      }
    }
    localStorage.setItem("seenJobs", JSON.stringify([...seen].slice(-500)));
    refresh.ready = true;
    render();
  } finally {
    polling = false;
  }
}
function selectProject(id) {
  cancelClipInteraction();
  sourcePreview = false;
  selectFirstShot = true;
  flushTitleDraft();
  cancelDrag();
  selectedRegionId = null;
  selectedShotId = null;
  trimDraft = null;
  pid = id;
  cid = null;
  shownRevision = -1;
  localStorage.setItem("project", id);
  $("video").pause();
  render();
  safe(loadReservations)();
}
function renderJobs(jobs) {
  const container = $("job-status");
  for (const node of [...container.children]) {
    if (!jobs.some((j) => j.id === node.dataset.job)) node.remove();
  }
  jobs.forEach((j, index) => {
    let node = [...container.children].find((n) => n.dataset.job === j.id);
    if (!node) {
      node = document.createElement("div");
      node.dataset.job = j.id;
      node.innerHTML = '<div class="job-fill" aria-hidden="true"></div><div class="job-copy"><strong class="job-state"></strong><span class="job-message"></span></div><strong class="job-percent"></strong><button class="job-action"></button>';
    }
    if (container.children[index] !== node) container.insertBefore(node, container.children[index] || null);
    const running = j.status === "running";
    const active = running || j.status === "queued";
    const known = typeof j.progress === "number" && Number.isFinite(j.progress);
    const percent = j.status === "succeeded" ? 100 : known ? Math.max(0, Math.min(100, j.progress)) : null;
    node.className = `job ${j.status}${running && percent === null ? " indeterminate" : ""}`;
    node.style.setProperty("--job-progress", `${percent ?? 0}%`);
    node.setAttribute("aria-busy", String(active));
    const labels = { running: "작업 진행 중", queued: "작업 대기 중", succeeded: "작업 완료", failed: "작업 실패", interrupted: "작업 중단됨", cancelled: "작업 취소됨" };
    node.querySelector(".job-state").textContent = labels[j.status] || j.status;
    node.querySelector(".job-message").textContent = j.message;
    const meter = node.querySelector(".job-percent");
    meter.textContent = percent !== null && j.status !== "queued" ? `${Math.floor(percent)}%` : running ? "처리 중" : "";
    meter.setAttribute("aria-label", running ? "현재 단계 진행률" : "진행률");
    if (running) {
      meter.setAttribute("role", "progressbar");
      meter.setAttribute("aria-valuemin", "0");
      meter.setAttribute("aria-valuemax", "100");
    } else meter.removeAttribute("role");
    if (percent !== null) meter.setAttribute("aria-valuenow", String(Math.floor(percent)));
    else meter.removeAttribute("aria-valuenow");
    const button = node.querySelector(".job-action");
    delete button.dataset.cancel;
    delete button.dataset.retry;
    button.hidden = !(active || ["failed", "interrupted", "cancelled"].includes(j.status));
    button.textContent = active ? "중단" : "재시도";
    if (active) button.dataset.cancel = j.id;
    else if (!button.hidden) button.dataset.retry = j.id;
  });
}
function render() {
  const p = project();
  $("projects").innerHTML = state.projects
    .map(
      (p) =>
        `<div class="project-row ${p.id === pid ? "active" : ""}"><button data-project="${p.id}" class="project-select">${esc(p.name)}<small>${time(p.metadata.duration)} · 쇼츠 ${p.clips.length}개</small></button><button class="project-delete" data-delete-project="${p.id}" aria-label="${esc(p.name)} 프로젝트 목록에서 삭제" title="프로젝트 목록에서 삭제" ${state.jobs.some((j) => j.project_id === p.id && ["running", "queued"].includes(j.status)) ? "disabled" : ""}>삭제</button></div>`,
    )
    .join("");
  $("empty").hidden = !!p;
  $("workspace").hidden = !p;
  $("settings-button").disabled = !p;
  if (!p) { cancelClipInteraction(); return; }
  $("project-name").textContent = p.name;
  $("project-meta").textContent =
    `${p.metadata.width} × ${p.metadata.height} · ${time(p.metadata.duration)} · 원본 보존 · ${p.model}`;
  $("clip-count").textContent = p.clips.length;
  $("summary").textContent =
    p.summary ||
    "내용 분석을 시작하면 AI가 구간을 제안합니다. 직접 추가할 수도 있습니다.";
  if (!clip()) { cid = p.clips[0]?.id; selectFirstShot = true; }
  renderClipTimeline();
  $("clips").innerHTML = p.clips
    .map(
      (c, i) =>
        `<article tabindex="0" role="button" data-clip="${c.id}" class="clip-card ${c.id === cid ? "active" : ""} ${!c.included ? "excluded" : ""}"><div class="row"><span class="badge">SHORT ${String(i + 1).padStart(2, "0")}</span><input type="checkbox" data-include="${c.id}" ${c.included ? "checked" : ""} aria-label="${i + 1}번 쇼츠 작업에 포함" ${busy() ? "disabled" : ""}></div><h3>${esc(c.title)}</h3><small>${time(c.start)} – ${time(c.end)} · ${editedDuration(c).toFixed(1)}초${c.cuts?.length ? ` · ${c.cuts.length}곳 삭제, ${(c.end-c.start-editedDuration(c)).toFixed(1)}초 단축` : ""}</small><div><small class="${editedDuration(c) > 180 ? "warning" : ""}">${editedDuration(c) > 180 ? "3분 초과 · 추가 분할 필요" : c.confirmed ? "✓ 문구 확정" : "문구 확인 대기"}${c.frame_suggestions?.length ? ` · 설명 화면 ${c.frame_suggestions.length}곳` : ""}</small></div></article>`,
    )
    .join("");
  // Jobs are newest first; a resumed job replaces the previous status card.
  const relevant = state.jobs.filter((j) => j.project_id === pid).slice(0, 1);
  renderJobs(relevant);
  for (const b of document.querySelectorAll(
    "[data-stage],#prepare-auto,#schedule-open,#add-clip,#undo,[data-align]",
  ))
    b.disabled = busy();
  if ((shownRevision !== p.revision || shownBusy !== busy()) && !mutating) {
    renderEditor();
    shownRevision = p.revision;
    shownBusy = busy();
  }
  if (videoProject !== pid) {
    videoProject = pid;
    $("video").src = `/api/projects/${pid}/source`;
    $("video").onloadedmetadata = () => {
      if (clip()) $("video").currentTime = keptRanges(clip())[0].start;
      updatePreview();
    };
  }
  updatePreview();
  if (selectFirstShot && !sourceShots().length) ensureClipShots();
}
function fieldValue(id, value) {
  if (document.activeElement !== $(id)) $(id).value = value;
}
function renderEditor() {
  const c = clip();
  $("editor").hidden = !c;
  $("hooks-pane").hidden = !c;
  $("editor-empty").hidden = !!c;
  $("preview-placeholder").hidden = !!c;
  $("download").disabled = !c || !c.render_current;
  if (!c) { $("title-image").hidden = true; titleRequest++; titleURL = ""; }
  $("video").style.visibility = c ? "visible" : "hidden";
  renderShotTimeline();
  if (!c) return;
  const v = $("video");
  if (
    !sourcePreview && videoProject === pid &&
    v.readyState > 0 &&
    (v.currentTime < c.start || v.currentTime > c.end)
  )
    v.currentTime = keptRanges(c)[0].start;
  $("content-edit-legacy").hidden = !(c.cuts || []).length;
  $("content-cuts").innerHTML = (c.cuts || []).map((cut, i) => `<div class="content-cut"><div><strong>원본 ${cut.start.toFixed(2)} ~ ${cut.end.toFixed(2)}초 삭제</strong><p>${esc(cut.reason)}</p></div><button data-restore-cut="${i}" class="quiet">복원</button></div>`).join("");
  const draft = titleDrafts.get(`${pid}/${cid}`);
  fieldValue("hook", draft?.changes.hook ?? c.hook);
  fieldValue("yellow", draft?.changes.yellow ?? c.yellow);
  fieldValue("font-size", draft?.changes.font_size ?? c.font_size);
  $("confirm-hook").textContent = c.confirmed
    ? "✓ 문구 확정됨"
    : "이 문구로 확정";
  $("recommendation").textContent =
    c.recommendation_reason || "AI가 검수한 후보 중 가장 좋은 문구를 추천합니다.";
  $("hooks").innerHTML = c.hooks
    .map(
      (h, i) =>
        `<button class="hook-option ${i === c.recommended_index ? "recommended" : ""} ${h.text === c.hook ? "selected" : ""}" data-hook="${i}">${i === c.recommended_index ? "<strong>✦ AI 추천</strong>" : ""}${esc(h.text)}${h.approach ? `<small class="hook-evaluation">${esc(h.approach)} · ${esc(h.evaluation || "")}</small>` : ""}</button>`,
    )
    .join("");
  const f = editingFrame();
  fieldValue("zoom", f.zoom);
  fieldValue("center", f.center);
  $("zoom-label").value = Math.round(f.zoom * 100) + "%";
  $("center-label").value = Math.round(f.center * 100) + "%";
  const regions = informationRegions();
  $("frame-suggestions").innerHTML = !Array.isArray(c.frame_suggestions) && !regions.length
    ? '<p class="muted">설명 화면 찾기를 실행하면 그림·글·각도 표시의 등장 구간을 잡아줍니다.</p>'
    : regions.length ? regions.map((s, i) => {
      const applied = c.frame_overrides?.find(f => f.id === s.id);
      return `<div data-region-id="${esc(s.id)}" class="frame-suggestion ${editingRegion()?.id === s.id ? "selected" : ""}">
        <button class="information-inspect" data-inspect="${i}">${s.thumbnail ? `<img src="/api/projects/${pid}/clips/${c.id}/information/${s.id}.jpg?v=${project().revision}" alt="설명 자료가 등장하는 원본 화면" loading="lazy">` : ""}
        <strong>${esc(s.subject || "설명 자료")}</strong><span>소재 내 ${preciseTime(s.start-c.start)} ~ ${preciseTime(s.end-c.start)}</span><small>원본 ${time(s.start)} ~ ${time(s.end)}</small></button>
        <p>${esc(s.reason)}</p><small>추천: ${esc(s.direction)} · ${Math.round(s.zoom*100)}%${s.confidence < .8 ? " · 확인 필요" : ""}</small>
        <div class="region-time-editor">
          <small>위치 적용 시간 · 소재 시작부터 초 단위</small>
          <div class="row">
            <label>적용 시작 (초)<input data-region-start type="number" min="0" max="${Number((c.end-c.start).toFixed(3))}" step="any" data-frame-step value="${Number((s.start-c.start).toFixed(6))}"></label>
            <label>적용 끝 (초)<input data-region-end type="number" min="0" max="${Number((c.end-c.start).toFixed(3))}" step="any" data-frame-step value="${Number((s.end-c.start).toFixed(6))}"></label>
          </div>
          <div class="row"><button data-region-now="start">현재 재생 위치를 시작으로</button><button data-region-now="end">현재 재생 위치를 끝으로</button><button data-save-region-time>시간 적용</button></div>
        </div>
        <div class="row"><button data-inspect="${i}">이 구간 위치 조정</button>${s.id ? `<button data-apply-region="${i}">${applied ? "추천 위치 다시 적용" : "추천 위치 적용"}</button>` : ""}</div>
        ${applied ? `<small>✓ 직접 선택한 위치 적용됨</small>` : '<small>추천만 표시 중 · 추천 위치 적용을 눌러야 화면이 바뀝니다.</small>'}
      </div>`;
    }).join("")
    : '<p class="muted">설명 그림·글·각도 표시를 찾지 못했습니다. 필요하면 다시 분석해 주세요.</p>';
  $("information-markers").innerHTML = regions.map((s,i) => `<button data-inspect="${i}" title="${esc(s.subject || s.reason)}">${time(s.start-c.start)}–${time(s.end-c.start)} 설명</button>`).join("");
  configureFrameInputs();
  loadTitlePreview();
  for (const el of document.querySelectorAll("#editor button,#editor input,#editor textarea,#hooks-pane button,#hooks-pane input,#hooks-pane textarea"))
    if (busy()) el.disabled = true;
    else el.disabled = false;
  $("download").disabled = busy() || !c.render_current;
}
let titleRequest = 0, titleURL = "", titleLoadedURL = "", titleObjectURL = null, titleRetryAt = 0;
async function loadTitlePreview(force = false) {
  if (!clip()) return;
  const url = `/api/projects/${pid}/clips/${cid}/title.png?v=${project().revision}`;
  if (!force && url === titleURL) return;
  titleURL = url;
  const request = ++titleRequest;
  $("title-image").hidden = true;
  $("preview-error").hidden = true;
  try {
    const response = await fetch(url, { cache: "no-cache" });
    if (!response.ok) {
      const data = await response.json();
      throw Error(data.error || "후킹 미리보기를 불러오지 못했습니다.");
    }
    const blob = await response.blob();
    if (request !== titleRequest) return;
    const objectURL = URL.createObjectURL(blob);
    const img = new Image();
    img.src = objectURL;
    try { await img.decode(); } catch (e) { URL.revokeObjectURL(objectURL); throw e; }
    if (request !== titleRequest) { URL.revokeObjectURL(objectURL); return; }
    if (titleObjectURL) URL.revokeObjectURL(titleObjectURL);
    titleObjectURL = objectURL;
    titleLoadedURL = url;
    $("title-image").src = objectURL;
    $("title-image").hidden = sourcePreview;
    titleRetryAt = 0;
  } catch (e) {
    if (request !== titleRequest) return;
    $("preview-error").textContent = e instanceof TypeError
      ? "앱 서버 연결이 끊겼습니다. 시작.command를 실행한 뒤 미리보기 재시도를 눌러 주세요."
      : e.message;
    $("preview-error").hidden = false;
    titleRetryAt = Date.now() + 3000;
  }
}
$("retry-preview").onclick = () => {
  loadTitlePreview(true);
  $("video").load();
};
$("reconnect-source").onclick = safe(async () => {
  const target = pid;
  const chosen = await api("/api/choose-file", {});
  if (chosen.cancelled) return;
  await editQueue;
  await api(`/api/projects/${target}/reconnect`, { path: chosen.path });
  if (pid === target) videoProject = null;
  await refresh();
  toast("원본을 다시 연결했습니다. 기존 쇼츠 편집은 유지됩니다.");
});
$("video").onerror = () => {
  $("source-error").textContent = "원본을 읽지 못했습니다. 앱 서버·파일 다운로드 상태를 확인하거나 같은 원본을 다시 연결해 주세요.";
  $("source-error").hidden = false;
};
$("video").addEventListener("loadedmetadata", () => { $("source-error").hidden = true; });
setInterval(() => {
  if (titleRetryAt && Date.now() >= titleRetryAt) { titleRetryAt = 0; loadTitlePreview(true); }
}, 1000);
window.addEventListener("beforeunload", e => {
  if (titleDrafts.size || trimDraft || clipInteraction?.edge) { e.preventDefault(); e.returnValue = ""; }
});
function sourceFps() {
  const value = String(project()?.metadata?.fps || "30"), [n, d = "1"] = value.split("/");
  const fps = Number(n) / Number(d);
  return Number.isFinite(fps) && fps > 0 ? fps : 30;
}
function sourceShots() {
  return (clip()?.source_shots || []).filter(s => Number.isFinite(s.start) && Number.isFinite(s.end) && s.end > s.start);
}
function sourceFrameTimes() {
  const key = `${pid}/${cid}/${project()?.revision}`;
  if (shotFrameCache.key !== key) {
    const c = clip();
    const times = [...new Set(sourceShots().flatMap(s => s.frame_times || []))]
      .filter(t => Number.isFinite(t) && t < c.end-1e-6).sort((a,b) => a-b);
    // A trim may begin inside a decoded frame. Keep that visible first frame.
    const first = lowerFrameIndex(times, c?.start || 0);
    shotFrameCache = { key, times:times.slice(Math.max(0, first-(times[first] > c?.start+1e-6 || first === times.length ? 1 : 0))) };
  }
  return shotFrameCache.times;
}
function lowerFrameIndex(times, at) {
  let low = 0, high = times.length;
  while (low < high) { const middle = (low+high) >>> 1; if (times[middle] < at) low = middle+1; else high = middle; }
  return low;
}
function nearestFrame(at) {
  const times = sourceFrameTimes(), index = lowerFrameIndex(times, at);
  if (!times.length) return Math.round(at*sourceFps())/sourceFps();
  if (!index) return times[0];
  if (index === times.length) return times.at(-1);
  return at-times[index-1] < times[index]-at ? times[index-1] : times[index];
}
function adjacentFrame(at, direction) {
  const times = sourceFrameTimes();
  if (!times.length) return (Math.round(at*sourceFps())+direction)/sourceFps();
  const index = direction > 0 ? lowerFrameIndex(times, at+1e-5) : lowerFrameIndex(times, at-1e-5)-1;
  return times[Math.max(0, Math.min(times.length-1, index))];
}
function previewTime() {
  const v = $("video");
  return !v.seeking && presentedTime?.pid === pid && presentedTime.cid === cid ? presentedTime.mediaTime : v.currentTime;
}
function containsFrame(range, at) {
  // Metadata stores decimal timestamps; tolerate their sub-microsecond rounding only.
  return at >= range.start-1e-6 && at < range.end-1e-6;
}
function preciseTime(at) {
  const milliseconds = Math.round(Math.max(0, at)*1000);
  return `${String(Math.floor(milliseconds/60000)).padStart(2,"0")}:${String(Math.floor(milliseconds/1000)%60).padStart(2,"0")}.${String(milliseconds%1000).padStart(3,"0")}`;
}
function snapRegionTime(at) {
  const c = clip();
  if (!c) return at;
  let value = Math.max(c.start, Math.min(c.end, at));
  if ($("snap-shots").checked) {
    const edges = [...new Set([c.start, c.end, ...sourceShots().flatMap(s => [s.start, s.end])])];
    const nearest = edges.reduce((best, edge) => Math.abs(edge-value) < Math.abs(best-value) ? edge : best, edges[0]);
    if (Math.abs(nearest-value) <= Math.max(2/sourceFps(), .08)) return nearest;
  }
  if (Math.abs(value-c.start)<1e-6 || Math.abs(value-c.end)<1e-6) return value;
  return Math.max(c.start, Math.min(c.end, nearestFrame(value)));
}
function seekFrame(at, select = false) {
  sourcePreview = false;
  const c = clip(), v = $("video");
  if (!c) return;
  v.pause();
  const target = at <= c.start+1e-6 ? c.start : Math.max(c.start, Math.min(adjacentFrame(c.end, -1), nearestFrame(at)));
  presentedTime = null;
  // Seek just inside the frame so six-decimal PTS rounding cannot show its predecessor.
  v.currentTime = target+1e-6;
  if (select) {
    const shot = sourceShots().find(s => containsFrame(s, target));
    if (shot) { selectedShotId = shot.id; selectedRegionId = null; trimDraft = null; renderShotTimeline(); }
  }
  updatePreview(target);
}
function editingShot() { return sourceShots().find(s => s.id === selectedShotId) || null; }
function frameScopeKey() { return selectedShotId ? `shot:${selectedShotId}` : selectedRegionId ? `region:${selectedRegionId}` : "whole"; }
function currentFrameDraft() {
  return frameDraft?.pid === pid && frameDraft?.cid === cid && frameDraft.scopeKey === frameScopeKey() ? frameDraft : null;
}
function selectShot(id) {
  const shot = sourceShots().find(s => s.id === id);
  if (!shot || busy() || mutating) return;
  cancelDrag();
  selectedRegionId = null; trimDraft = null; selectedShotId = id;
  seekFrame(shot.start);
  shownRevision = -1; render();
  const node = [...$("shot-strip").children].find(el => el.dataset.shotId === id);
  node?.scrollIntoView({ block:"nearest", inline:"nearest" });
}
function configureFrameInputs() {
  const inputs = document.querySelectorAll("[data-frame-step],#selected-region-start,#selected-region-end,#custom-region-start,#custom-region-end");
  for (const input of inputs) {
    input.step = "any";
    input.title = "소재 시작부터 초 단위 · 방향키로 한 프레임 이동";
    input.onkeydown = event => {
      if (!["ArrowUp","ArrowDown"].includes(event.key) || !clip()) return;
      event.preventDefault();
      input.value = Number((adjacentFrame(clip().start+Number(input.value), event.key === "ArrowUp" ? 1 : -1)-clip().start).toFixed(6));
    };
  }
}
// The overview uses absolute source time; the cut editor below uses clip-relative time.
function selectClip(id) {
  if (!project()?.clips.some(c => c.id === id)) return;
  cancelClipInteraction();
  flushTitleDraft();
  cancelDrag();
  endShotScrub();
  selectedRegionId = selectedShotId = null;
  trimDraft = null;
  sourcePreview = false;
  selectFirstShot = true;
  cid = id;
  $('clip-trim-status').textContent = '쇼츠 구간을 선택하면 아래에서 원본 컷마다 화면 구도를 조정할 수 있습니다.';
  shownRevision = -1;
  seekFrame(keptRanges(clip())[0].start);
  render();
  ensureClipShots();
}
const requestedClipShots = new Set();
async function ensureClipShots() {
  const c = clip();
  if (!c || sourceShots().length || busy() || mutating || clipInteraction) return;
  const key = `${pid}/${cid}/${c.start}/${c.end}`;
  if (requestedClipShots.has(key)) return;
  requestedClipShots.add(key);
  const target = {pid, cid};
  try {
    flushTitleDraft();
    await editQueue;
    if (pid !== target.pid || cid !== target.cid || busy() || clipInteraction) { requestedClipShots.delete(key); return; }
    await run('shots', {clip_ids:[target.cid],automatic:true});
  } catch (error) { toast(`원본 컷을 찾지 못했습니다. 원본 컷 찾기로 다시 시도하세요. ${error.message}`); }
}
function compactClipOffset(c, duration, width) {
  return c.start/duration*width+76 > width ? -75 : 9;
}
function overviewRows(clips, duration, width) {
  const ends = [];
  return clips.map((c, index) => ({c,index})).sort((a,b) => a.c.start-b.c.start || a.index-b.index).map(item => {
    // Separate overlapping clips, including handles on very short adjacent clips.
    const units = duration/Math.max(1,width);
    const compact = (item.c.end-item.c.start)/units < 42;
    const offset = compact ? compactClipOffset(item.c,duration,width) : 0;
    const left = Math.min(item.c.start,item.c.start+offset*units);
    let row = ends.findIndex(end => end <= left);
    if (row < 0) row = ends.length;
    ends[row] = Math.max(item.c.end,item.c.start+(compact ? offset+66 : 76)*units);
    return {...item,row};
  });
}
function timelineImages(start, end, width, limit=24) {
  const count = Math.max(1,Math.min(limit,Math.ceil(width/100)));
  return Array.from({length:count},(_,i) => {
    const at = Number((start+(end-start)*(i+.5)/count).toFixed(6));
    return `<span class="timeline-thumb" title="${preciseTime(at)}"><img src="/api/projects/${pid}/timeline.jpg?time=${at}" alt="" loading="lazy" decoding="async" draggable="false"></span>`;
  }).join('');
}
function renderClipTimeline() {
  const p = project();
  if (clipInteraction && (!p || clipInteraction.pid !== pid || clipInteraction.cid !== cid || busy() || mutating)) cancelClipInteraction();
  $('clip-timeline-editor').hidden = !p;
  if (!p) return;
  const duration = p.metadata.duration, width = $('clip-timeline').clientWidth || 600;
  const c = clip();
  $('clip-timeline-summary').textContent = `원본 전체 ${preciseTime(duration)} · 쇼츠 ${p.clips.length}개${c ? ` · 선택: 쇼츠 ${p.clips.indexOf(c)+1} · ${preciseTime(c.start)} ~ ${preciseTime(c.end)}` : ''}`;
  if (clipInteraction) return;
  const key = `${pid}/${cid}/${p.revision}/${busy()}/${width}`;
  if (key === clipTimelineKey) return;
  clipTimelineKey = key;
  const tick = timelineTick(duration, width);
  $('clip-ruler').innerHTML = Array.from({length:Math.floor(duration/tick)+1}, (_,i) => `<span style="left:${100*i*tick/duration}%">${time(i*tick)}</span>`).join('')+`<span class="clip-ruler-end">${time(duration)}</span>`;
  const filmstripKey = `${pid}/${duration}/${Math.ceil(width/100)}`;
  if ($('source-filmstrip').dataset.key !== filmstripKey) {
    $('source-filmstrip').dataset.key = filmstripKey;
    $('source-filmstrip').innerHTML = timelineImages(0,duration,width);
  }
  const rows = overviewRows(p.clips, duration, width);
  $('clip-strip').style.height = `${Math.max(1,...rows.map(r=>r.row+1))*72}px`;
  $('clip-strip').innerHTML = rows.map(({c,index,row}) => `<div class="clip-range${c.id===cid?' active':''}${!c.included?' excluded':''}${(c.end-c.start)/duration*width<42?' compact':''}" data-clip-id="${esc(c.id)}" style="left:${100*c.start/duration}%;width:${100*(c.end-c.start)/duration}%;top:${row*72+4}px;--compact-offset:${compactClipOffset(c,duration,width)}px" title="쇼츠 ${index+1} · ${esc(c.title)} · ${preciseTime(c.start)} ~ ${preciseTime(c.end)}">
    <button class="clip-range-body" data-select-clip="${esc(c.id)}" aria-pressed="${c.id===cid}" aria-label="쇼츠 ${index+1} 선택: ${esc(c.title)}"><span class="clip-range-images" aria-hidden="true">${timelineImages(c.start,c.end,(c.end-c.start)/duration*width,8)}</span><span class="clip-range-caption"><span class="clip-range-label">쇼츠 ${index+1}${!c.included?' · 제외':''}</span><span class="clip-range-time">${(c.end-c.start).toFixed(1)}초</span></span></button>
    <button class="clip-trim-handle" data-edge="start" aria-label="쇼츠 ${index+1} 시작 조정" title="드래그로 시작 조정 · 방향키로 한 프레임" ${busy()?'disabled':''}></button>
    <button class="clip-trim-handle" data-edge="end" aria-label="쇼츠 ${index+1} 끝 조정" title="드래그로 끝 조정 · 방향키로 한 프레임" ${busy()?'disabled':''}></button></div>`).join('');
}
function sourceTimeAt(event) {
  const rect = $('clip-timeline').getBoundingClientRect();
  return Math.max(0,Math.min(project().metadata.duration, (event.clientX-rect.left)/Math.max(1,rect.width)*project().metadata.duration));
}
function snapSourceTime(at) {
  const times = sourceFrameTimes();
  const snapped = times.length && at >= times[0] && at <= times.at(-1) ? nearestFrame(at) : Math.round(at*sourceFps())/sourceFps();
  return Number(Math.max(0,Math.min(project().metadata.duration,snapped)).toFixed(6));
}
function seekSource(at) {
  const v = $('video');
  sourcePreview = true;
  v.pause();
  presentedTime = null;
  v.currentTime = Math.max(0,Math.min(project().metadata.duration-1e-6,at))+1e-6;
  updatePreview(at);
}
function trimBounds(c, edge, at) {
  const start = edge==='start' ? Math.max(0, Math.min(c.end-.1,at)) : c.start;
  const end = edge==='end' ? Math.min(project().metadata.duration, Math.max(c.start+.1,at)) : c.end;
  return {start:Number(start.toFixed(6)),end:Number(end.toFixed(6))};
}
function moveClipInteraction(event) {
  const drag = clipInteraction;
  if (!drag || event.pointerId !== drag.pointerId) return;
  if (pid !== drag.pid || cid !== drag.cid || busy() || mutating) { cancelClipInteraction(); return; }
  event.preventDefault();
  if (drag.edge && !drag.moved && Math.abs(event.clientX-drag.startX)<1) return;
  drag.moved = true;
  const at = snapSourceTime(sourceTimeAt(event)-drag.offset);
  if (!drag.edge) { seekSource(at); return; }
  Object.assign(drag,trimBounds(drag.original,drag.edge,at));
  const node = [...$('clip-strip').children].find(n=>n.dataset.clipId===drag.cid);
  if (node) {
    node.style.left = `${100*drag.start/project().metadata.duration}%`;
    node.style.width = `${100*(drag.end-drag.start)/project().metadata.duration}%`;
    node.querySelector('.clip-range-time').textContent = `${(drag.end-drag.start).toFixed(1)}초`;
  }
  $('clip-trim-status').textContent = `${preciseTime(drag.start)} ~ ${preciseTime(drag.end)} · ${(drag.end-drag.start).toFixed(3)}초 · 놓으면 저장`;
  seekSource(drag[drag.edge]);
}
function releaseClipInteraction() {
  const drag = clipInteraction;
  clipInteraction = null;
  if (drag && $('clip-timeline').hasPointerCapture(drag.pointerId)) $('clip-timeline').releasePointerCapture(drag.pointerId);
  $('clip-timeline').classList.remove('trimming');
  return drag;
}
function cancelClipInteraction(event) {
  if (!clipInteraction || (event?.pointerId !== undefined && event.pointerId !== clipInteraction.pointerId)) return;
  const drag = releaseClipInteraction();
  if (drag.edge && drag.pid===pid && drag.cid===cid) seekFrame(Math.max(clip().start,Math.min(clip().end,drag.before)));
  clipTimelineKey = '';
  $('clip-trim-status').textContent = '';
  renderClipTimeline();
}
async function saveClipBounds(drag) {
  if (drag.start===drag.original.start && drag.end===drag.original.end) return;
  flushTitleDraft();
  await editQueue;
  const c = state.projects.find(p=>p.id===drag.pid)?.clips.find(c=>c.id===drag.cid);
  if (!c || c.start!==drag.original.start || c.end!==drag.original.end) throw Error('다른 편집으로 쇼츠 범위가 바뀌었습니다. 새 범위에서 다시 조정해 주세요.');
  await edit('update',{changes:{start:drag.start,end:drag.end}}, {pid:drag.pid,cid:drag.cid});
}
async function finishClipInteraction(event) {
  if (!clipInteraction || event.pointerId !== clipInteraction.pointerId) return;
  moveClipInteraction(event);
  const drag = releaseClipInteraction();
  if (!drag) return;
  if (!drag.edge) return;
  try {
    await saveClipBounds(drag);
    if (pid===drag.pid && cid===drag.cid) {
      selectedShotId = selectedRegionId = null;
      selectFirstShot = true;
      shownRevision = -1;
      seekFrame(drag.edge==='start' ? clip().start : adjacentFrame(clip().end,-1));
      render();
      $('clip-trim-status').textContent = '길이를 저장했습니다. 되돌리기로 복원할 수 있습니다.';
      ensureClipShots();
    }
  } catch (error) {
    if (pid===drag.pid && cid===drag.cid) { seekFrame(drag.before); clipTimelineKey=''; render(); }
    $('clip-trim-status').textContent = '저장하지 못했습니다. 기존 구간을 유지합니다.';
    toast(error.message);
  }
}
$('clip-strip').onclick = event => {
  const button = event.target.closest('[data-select-clip]');
  if (button) selectClip(button.dataset.selectClip);
};
$('clip-strip').onpointerdown = event => {
  const handle = event.target.closest('[data-edge]');
  if (!handle || event.button!==0 || event.isPrimary===false || busy() || mutating || clipInteraction) return;
  event.preventDefault();
  const id = handle.closest('[data-clip-id]').dataset.clipId;
  if (id!==cid) selectClip(id);
  const c = clip(), edge=handle.dataset.edge;
  clipInteraction={pid,cid,edge,startX:event.clientX,moved:false,pointerId:event.pointerId,original:{start:c.start,end:c.end},start:c.start,end:c.end,before:previewTime(),offset:sourceTimeAt(event)-c[edge]};
  $('clip-timeline').setPointerCapture(event.pointerId);
  $('clip-timeline').classList.add('trimming');
  $('clip-timeline-scroll').focus({preventScroll:true});
  $('video').pause();
};
function startSourceScrub(event) {
  if (!clip() || event.button!==0 || event.isPrimary===false || busy() || mutating || clipInteraction) return;
  event.preventDefault();
  clipInteraction={pid,cid,pointerId:event.pointerId,offset:event.currentTarget===$('clip-playhead') ? sourceTimeAt(event)-previewTime() : 0};
  $('clip-timeline').setPointerCapture(event.pointerId);
  moveClipInteraction(event);
}
$('clip-ruler').onpointerdown = startSourceScrub;
$('source-filmstrip').onpointerdown = startSourceScrub;
$('clip-playhead').onpointerdown = startSourceScrub;
$('clip-timeline').onpointermove = moveClipInteraction;
$('clip-timeline').onpointerup = finishClipInteraction;
$('clip-timeline').onpointercancel = cancelClipInteraction;
$('clip-timeline').onlostpointercapture = cancelClipInteraction;
window.addEventListener('blur',()=>cancelClipInteraction());
$('clip-timeline-scroll').onkeydown = safe(async event => {
  if (event.key==='Escape') return cancelClipInteraction();
  const handle = event.target.closest('[data-edge]');
  if (!handle || !['ArrowLeft','ArrowRight'].includes(event.key) || busy() || mutating) return;
  event.preventDefault();
  const id = handle.closest('[data-clip-id]').dataset.clipId;
  if (id!==cid) selectClip(id);
  const c=clip(), edge=handle.dataset.edge, direction=event.key==='ArrowLeft'?-1:1;
  const next = adjacentFrame(c[edge],direction);
  const at = direction*(next-c[edge])>1e-6 ? next : snapSourceTime(c[edge]+direction/sourceFps());
  const drag={pid,cid,original:{start:c.start,end:c.end},...trimBounds(c,edge,at)};
  await saveClipBounds(drag);
  if (pid!==drag.pid || cid!==drag.cid) return;
  selectedShotId = selectedRegionId = null;
  selectFirstShot = true;
  shownRevision = -1;
  seekFrame(edge==='start'?clip().start:adjacentFrame(clip().end,-1));
  render();
  const range = [...$('clip-strip').children].find(node=>node.dataset.clipId===cid);
  range?.querySelector(`[data-edge="${edge}"]`)?.focus({preventScroll:true});
  ensureClipShots();
});

function timelineTick(duration, width) {
  const target = Math.max(1/sourceFps(), duration*100/Math.max(1,width), duration/200);
  const power = Math.pow(10, Math.floor(Math.log10(target)));
  return [1,2,5,10].find(multiplier => multiplier*power >= target)*power;
}
function renderShotTimeline() {
  const c = clip(), shots = sourceShots();
  if (shotScrub && (!c || shotScrub.pid !== pid || shotScrub.cid !== cid || busy() || mutating)) endShotScrub();
  $("shot-editor").hidden = !c;
  if (!c) return;
  if (selectedShotId && !shots.some(s => s.id === selectedShotId)) {
    selectedShotId = (shots.find(s => containsFrame(s,previewTime())) || shots[0])?.id || null;
  }
  if (selectFirstShot && shots.length) { selectedShotId = shots[0].id; selectFirstShot = false; }
  const disabled = busy() || mutating;
  $("detect-shots").disabled = disabled;
  $("detect-shots").textContent = c.source_shots_manual ? "원본 컷 확인" : shots.length ? "원본 컷 다시 찾기" : "원본 컷 찾기";
  $("detect-shots").title = c.source_shots_manual ? "직접 고친 컷 경계를 유지하고 미리보기를 확인합니다." : "원본 프레임을 비교해 장면 전환을 찾습니다.";
  $("shot-empty").hidden = !!shots.length;
  $("shot-summary").textContent = `쇼츠 ${project().clips.indexOf(c)+1} · ${c.title} · ` + (shots.length ? `${shots.length}개 컷 · ${sourceFps().toFixed(3).replace(/0+$/, "").replace(/\.$/, "")} fps · 시간은 쇼츠 시작 기준` : "원본 컷을 찾으면 컷별로 구도를 조정할 수 있습니다.");
  const key = `${pid}/${cid}/${project().revision}/${$("shot-zoom").value}`;
  if (key !== shotTimelineKey) {
    shotTimelineKey = key;
    const duration = c.end-c.start;
    const width = Math.max($("shot-timeline-scroll").clientWidth || 600, duration*12*Number($("shot-zoom").value));
    $("shot-timeline").style.width = `${width}px`;
    const tick = timelineTick(duration, width);
    const count = Math.min(200, Math.floor(duration/tick));
    $("shot-ruler").innerHTML = Array.from({length:count+1}, (_,i) => `<span style="left:${100*i*tick/duration}%">${preciseTime(i*tick)}</span>`).join("");
    $("shot-strip").innerHTML = shots.map((shot,index) => {
      const overrides = (c.frame_overrides || []).filter(f => f.start < shot.end-1e-6 && f.end > shot.start+1e-6);
      const changed = overrides.length > 0;
      const mixed = changed && !(overrides.length === 1 && overrides[0].start <= shot.start+1e-6 && overrides[0].end >= shot.end-1e-6);
      return `<button class="shot-cell${changed ? " adjusted" : ""}" data-shot-id="${esc(shot.id)}" style="left:${100*(shot.start-c.start)/duration}%;width:${100*(shot.end-shot.start)/duration}%" aria-label="원본 컷 ${index+1}, ${preciseTime(shot.start-c.start)}부터 ${preciseTime(shot.end-c.start)}까지${mixed ? ", 구도 혼합" : changed ? ", 구도 적용됨" : ""}" title="컷 ${index+1} · ${preciseTime(shot.start-c.start)} ~ ${preciseTime(shot.end-c.start)}${mixed ? " · 구도 혼합" : ""}">${shot.thumbnail ? `<img src="/api/projects/${pid}/clips/${cid}/shots/${encodeURIComponent(shot.id)}.jpg?v=${project().revision}" alt="" loading="lazy">` : '<span class="shot-no-image">원본 컷</span>'}<span class="shot-cell-label">${index+1}${mixed ? " · 혼합" : changed ? " · 적용" : ""}</span></button>`;
    }).join("");
  }
  const selected = editingShot(), selectedIndex = shots.indexOf(selected);
  for (const node of $("shot-strip").children) {
    node.classList.toggle("selected", node.dataset.shotId === selectedShotId);
    node.setAttribute("aria-pressed", String(node.dataset.shotId === selectedShotId));
    node.disabled = disabled;
  }
  $("selected-shot").hidden = !selected;
  if (selected) {
    $("selected-shot-title").textContent = `선택한 원본 컷 ${selectedIndex+1}`;
    $("selected-shot-time").textContent = `${preciseTime(selected.start-c.start)} ~ ${preciseTime(selected.end-c.start)} · ${(selected.end-selected.start).toFixed(3)}초`;
  }
  for (const button of document.querySelectorAll("[data-shot-boundary]")) {
    const first = button.dataset.shotBoundary === "start" && selectedIndex === 0;
    const last = button.dataset.shotBoundary === "end" && selectedIndex === shots.length-1;
    button.disabled = disabled || !selected || first || last;
    button.title = first || last ? "위 전체 영상 타임라인에서 쇼츠의 양끝을 드래그하세요." : "인접한 두 컷의 공통 경계를 이동합니다.";
  }
  for (const id of ["previous-frame","next-frame","split-shot"]) $(id).disabled = disabled || (id === "split-shot" && !shots.length);
  $("previous-shot").disabled = disabled || !shots.length;
  $("next-shot").disabled = disabled || !shots.length;
  $("merge-shot").disabled = disabled || !selected || selectedIndex === shots.length-1;
  for (const button of document.querySelectorAll("[data-shot-align],#shot-frame-controls")) button.disabled = disabled || !selected;
  updateShotPlayhead(previewTime());
}
function updateShotPlayhead(at) {
  const c = clip();
  if (!c) return;
  at = Math.max(c.start, Math.min(c.end, at));
  $("shot-playhead").style.left = `${100*Math.max(0,Math.min(c.end-c.start,at-c.start))/(c.end-c.start)}%`;
  $("shot-playhead").setAttribute("aria-valuemin", "0");
  $("shot-playhead").setAttribute("aria-valuemax", String(c.end-c.start));
  $("shot-playhead").setAttribute("aria-valuenow", String(at-c.start));
  $("shot-playhead").setAttribute("aria-valuetext", preciseTime(at-c.start));
  const frameTimes = sourceFrameTimes();
  const number = frameTimes.length ? Math.max(1,lowerFrameIndex(frameTimes, at+1e-6)) : Math.max(1,Math.round((at-c.start)*sourceFps())+1);
  $("shot-position").textContent = `${preciseTime(at-c.start)} · ${number} 프레임`;
  const shots = sourceShots();
  for (const node of $("shot-strip").children) {
    const shot = shots.find(s => s.id === node.dataset.shotId);
    node.classList.toggle("playing", !!shot && containsFrame(shot, at));
  }
}
function moveShot(direction) {
  const shots = sourceShots(), at = previewTime();
  let index = shots.findIndex(s => containsFrame(s, at));
  if (index < 0) index = direction > 0 ? -1 : shots.length;
  selectShot(shots[Math.max(0,Math.min(shots.length-1,index+direction))]?.id);
}
$("detect-shots").onclick = safe(() => run("shots", { clip_ids:[cid] }));
$("shot-strip").onclick = event => { const cell = event.target.closest("[data-shot-id]"); if (cell) selectShot(cell.dataset.shotId); };
function scrubShotAt(event) {
  if (!shotScrub || event.pointerId !== shotScrub.pointerId) return;
  if (!clip() || shotScrub.pid !== pid || shotScrub.cid !== cid || busy() || mutating) { endShotScrub(); return; }
  event.preventDefault();
  const rect = $("shot-timeline").getBoundingClientRect();
  if (rect.width > 0) seekFrame(clip().start+(event.clientX-rect.left-shotScrub.offset)/rect.width*(clip().end-clip().start));
}
function endShotScrub(event) {
  if (!shotScrub || (event && event.pointerId !== shotScrub.pointerId)) return;
  const pointerId = shotScrub.pointerId;
  shotScrub = null;
  const timeline = $("shot-timeline");
  timeline.classList.remove("scrubbing");
  if (timeline.hasPointerCapture(pointerId)) timeline.releasePointerCapture(pointerId);
}
function startShotScrub(event) {
  if (!clip() || busy() || mutating || event.button !== 0 || event.isPrimary === false || shotScrub) return;
  event.preventDefault();
  const c = clip(), timeline = $("shot-timeline"), rect = timeline.getBoundingClientRect();
  if (rect.width <= 0) return;
  const at = Math.max(c.start, Math.min(c.end, previewTime()));
  // Keep the grabbed frame in place even when pressing the edge of its hit area.
  const offset = event.currentTarget === $("shot-playhead") ? event.clientX-rect.left-(at-c.start)/(c.end-c.start)*rect.width : 0;
  shotScrub = {pid, cid, pointerId:event.pointerId, offset};
  timeline.setPointerCapture(event.pointerId);
  timeline.classList.add("scrubbing");
  $("shot-timeline-scroll").focus({preventScroll:true});
  scrubShotAt(event);
}
$("shot-ruler").onpointerdown = startShotScrub;
$("shot-playhead").onpointerdown = startShotScrub;
$("shot-timeline").onpointermove = scrubShotAt;
$("shot-timeline").onpointerup = event => { scrubShotAt(event); endShotScrub(event); };
$("shot-timeline").onpointercancel = endShotScrub;
$("shot-timeline").onlostpointercapture = endShotScrub;
window.addEventListener("blur", () => endShotScrub());
$("shot-zoom").oninput = () => {
  const at = previewTime(); renderShotTimeline();
  const scroll = $("shot-timeline-scroll"), width = $("shot-timeline").clientWidth;
  scroll.scrollLeft = width*(at-clip().start)/(clip().end-clip().start)-scroll.clientWidth/2;
};
$("previous-shot").onclick = () => moveShot(-1);
$("next-shot").onclick = () => moveShot(1);
$("previous-frame").onclick = () => seekFrame(adjacentFrame(previewTime(), -1), true);
$("next-frame").onclick = () => seekFrame(adjacentFrame(previewTime(), 1), true);
$("shot-timeline-scroll").onkeydown = event => {
  if (busy() || mutating || !["ArrowLeft","ArrowRight","ArrowUp","ArrowDown"," "].includes(event.key)) return;
  event.preventDefault();
  if (event.key === " ") { if (!event.repeat) $("play").click(); }
  else if (event.key === "ArrowUp" || event.key === "ArrowDown") moveShot(event.key === "ArrowUp" ? -1 : 1);
  else seekFrame(adjacentFrame(previewTime(), event.key === "ArrowLeft" ? -1 : 1), true);
};
for (const button of document.querySelectorAll("[data-shot-align]")) button.onclick = safe(() => {
  if (editingShot()) return saveFrame({ ...editingFrame(), center:Number(button.dataset.shotAlign), ...(Number(button.dataset.shotAlign) === .5 ? {vertical:.5} : {}) });
});
$("shot-frame-controls").onclick = () => { $("frame-controls").scrollIntoView({block:"nearest", behavior:"smooth"}); $("zoom").focus({preventScroll:true}); };
$("split-shot").onclick = safe(async () => {
  if (!clip() || busy() || mutating) return;
  const target = {pid,cid}, at = nearestFrame(previewTime());
  await edit("shot_split", {at}, target);
  if (pid === target.pid && cid === target.cid) {
    const added = sourceShots().find(s => Math.abs(s.start-at)<1e-6);
    if (added) selectShot(added.id);
    toast("현재 프레임에 구도용 컷 경계를 추가했습니다. 영상 길이는 그대로입니다.");
  }
});
$("merge-shot").onclick = safe(async () => {
  const shot = editingShot();
  if (!shot || busy() || mutating) return;
  await edit("shot_merge", {shot_id:shot.id});
  toast("두 컷을 합쳤습니다. 기존 구도는 유지되며 정렬을 지정하면 합친 컷 전체에 적용됩니다.");
});
for (const button of document.querySelectorAll("[data-shot-boundary]")) button.onclick = safe(async () => {
  const shot = editingShot();
  if (!shot || busy() || mutating) return;
  const target = {pid,cid}, edge = button.dataset.shotBoundary;
  const at = button.hasAttribute("data-at-now") ? nearestFrame(previewTime()) : adjacentFrame(shot[edge], Number(button.dataset.shift));
  await edit("shot_boundary", {shot_id:shot.id, edge, at}, target);
  if (pid === target.pid && cid === target.cid) { seekFrame(at); renderShotTimeline(); }
});

function previewOverrides() {
  const frames = clip()?.frame_overrides || [];
  if (trimDraft?.pid !== pid || trimDraft?.cid !== cid) return frames;
  const existing = frames.find(f => f.id === trimDraft.id);
  return existing ? [...frames.filter(f => f.id !== trimDraft.id), { ...existing, start:trimDraft.start, end:trimDraft.end }] : frames;
}
function updateRegionTimeline() {
  const c = clip(), region = informationRegions().find(s => s.id === selectedRegionId);
  $("preview-region").hidden = !region;
  if (!region || !c) return;
  const duration = c.end-c.start;
  const start = region.start-c.start, end = region.end-c.start;
  $("preview-region-name").textContent = region.subject || "직접 조정한 설명 구간";
  $("preview-region-time").textContent = `${preciseTime(start)} ~ ${preciseTime(end)}`;
  for (const edge of ["start", "end"]) {
    const input = $(`region-trim-${edge}`);
    input.min = 0; input.max = duration; input.step = "any";
    input.value = edge === "start" ? start : end;
    input.disabled = busy();
    const number = $(`selected-region-${edge}`), key = `${pid}/${cid}/${region.id}/${start}/${end}`;
    if (number.dataset.regionKey !== key) { number.value = Number((edge === "start" ? start : end).toFixed(6)); number.dataset.regionKey = key; }
  }
  $("region-trim-fill").style.left = `${100*start/duration}%`;
  $("region-trim-fill").style.width = `${100*(end-start)/duration}%`;
}
for (const edge of ["start", "end"]) {
  $(`region-trim-${edge}`).oninput = () => {
    const c = clip(), region = informationRegions().find(s => s.id === selectedRegionId);
    if (!c || !region || busy()) return;
    const other = (c.frame_overrides || []).filter(f => f.id !== region.id);
    const lower = Math.max(c.start, ...other.filter(f => f.end <= region.start).map(f => f.end));
    const upper = Math.min(c.end, ...other.filter(f => f.start >= region.end).map(f => f.start));
    const value = snapRegionTime(c.start + Number($(`region-trim-${edge}`).value));
    const start = edge === "start" ? Math.max(lower, Math.min(adjacentFrame(region.end, -1), value)) : region.start;
    const end = edge === "end" ? Math.min(upper, Math.max(adjacentFrame(region.start, 1), value)) : region.end;
    trimDraft = { pid, cid, id:region.id, start, end };
    $("video").pause();
    $("video").currentTime = edge === "start" ? start : Math.max(start, adjacentFrame(end, -1));
    $("region-trim-status").textContent = "조정 중 · 놓으면 자동 저장";
    const card = document.querySelector(`[data-region-id="${CSS.escape(region.id)}"]`);
    if (card) {
      card.querySelector("[data-region-start]").value = Number((start-c.start).toFixed(6));
      card.querySelector("[data-region-end]").value = Number((end-c.start).toFixed(6));
    }
    updatePreview();
  };
  $(`region-trim-${edge}`).onchange = safe(async () => {
    const draft = trimDraft;
    if (!draft || busy()) return;
    $("region-trim-status").textContent = "저장 중…";
    try {
      await edit("frame_region_time", { suggestion_id:draft.id, start:draft.start, end:draft.end }, draft);
      if (trimDraft === draft) {
        trimDraft = null;
        $("region-trim-status").textContent = "저장됨";
        updatePreview();
      }
    } catch (e) {
      if (trimDraft === draft) {
        trimDraft = null;
        $("region-trim-status").textContent = "저장 실패 · 다시 조정해 주세요";
        shownRevision = -1; render();
      }
      throw e;
    }
  });
}
$("preview").onclick = e => {
  if (e.target !== $("video")) $("preview").focus({ preventScroll:true });
};
document.querySelector(".preview-pane").addEventListener("keydown", e => {
  if (e.code !== "Space" || e.defaultPrevented) return;
  if (e.target.closest("button,textarea,[contenteditable=true],input:not([type=range])")) return;
  e.preventDefault();
  if (!e.repeat) $("play").click();
});
function informationRegions() {
  const c = clip();
  if (!c) return [];
  const regions = (c.frame_suggestions || []).filter(s => Number.isFinite(s.start) && Number.isFinite(s.end) && s.id).map(s => {
    const applied = previewOverrides().find(f => f.id === s.id);
    const draft = trimDraft?.pid === pid && trimDraft?.cid === cid && trimDraft.id === s.id ? trimDraft : null;
    return { ...s, ...(applied ? { start:applied.start, end:applied.end } : {}), ...(draft ? { start:draft.start, end:draft.end } : {}) };
  });
  for (const f of previewOverrides()) if (!regions.some(s => s.id === f.id))
    regions.push({ ...f, time:(f.start+f.end)/2, subject:"직접 조정한 설명 구간", reason:"이전에 저장한 구간 위치", direction:"저장된 위치", confidence:1 });
  return regions.sort((a,b) => a.start-b.start);
}
function editingRegion() {
  const regions = informationRegions();
  const selected = regions.find(s => s.id === selectedRegionId);
  if (selected) return selected;
  return null;
}
function activeFrame(at = previewTime()) {
  if (clip()) at = Math.max(clip().start, Math.min(clip().end-2e-6, at));
  if (currentFrameDraft()) return currentFrameDraft().frame;
  const c = clip();
  const f = previewOverrides().find(f => containsFrame(f, at)) || c?.manual_frame;
  return f ? { zoom:f.zoom, center:f.center, vertical:f.vertical ?? .5 } : { zoom:1.5, center:.5, vertical:.5 };
}
function editingFrame() {
  if (currentFrameDraft()) return currentFrameDraft().frame;
  const region = editingRegion(), shot = editingShot(), c = clip();
  const f = shot ? c?.frame_overrides?.find(f => containsFrame(f, shot.start)) || c?.manual_frame
    : c?.frame_overrides?.find(f => f.id === region?.id) || c?.manual_frame;
  return f ? { zoom:f.zoom, center:f.center, vertical:f.vertical ?? .5 } : { zoom:1.5, center:.5, vertical:.5 };
}
function frameGeometry(frame) {
  const m = project().metadata;
  const z = Math.max(1, Math.min(frame.zoom, 2, m.width / m.height));
  const sw = Math.round(1080 * z / 2) * 2;
  const sh = Math.round(sw * m.height / m.width / 2) * 2;
  const x = Math.floor(Math.max(0, Math.min(sw - 1080, frame.center * sw - 540)) / 2) * 2;
  const y = Math.floor((1920 - sh) * (frame.vertical ?? 0.5) / 2) * 2;
  return { z, sw, sh, x, y };
}
function updatePreview(at) {
  const p = project(),
    c = clip(),
    v = $("video");
  updateRegionTimeline();
  if (!p || !c) return;
  const t = Math.max(sourcePreview ? 0 : c.start, Math.min(sourcePreview ? p.metadata.duration : c.end, Number.isFinite(at) ? at : previewTime()));
  $("clip-playhead").style.left = `${100*t/p.metadata.duration}%`;
  $("clip-playhead").hidden = false;
  $("clip-playhead").setAttribute("aria-valuemin", "0");
  $("clip-playhead").setAttribute("aria-valuemax", String(p.metadata.duration));
  $("clip-playhead").setAttribute("aria-valuenow", String(t));
  $("clip-playhead").setAttribute("aria-valuetext", preciseTime(t));
  $("shot-playhead").hidden = sourcePreview && (t<c.start || t>=c.end);
  $("title-image").hidden = sourcePreview || titleLoadedURL !== titleURL || !titleObjectURL;
  updateShotPlayhead(t);
  const s = sourcePreview && (t<c.start || t>=c.end) ? {zoom:1,center:.5,vertical:.5} : activeFrame(t);
  const { z, sw, sh, x, y } = frameGeometry(s);
  const ratio = $("preview").clientWidth / 1080;
  v.style.width = sw * ratio + "px";
  v.style.height = sh * ratio + "px";
  v.style.left = -x * ratio + "px";
  v.style.top = y * ratio + "px";
  $("seek").step = "any";
  $("seek").min = sourcePreview ? 0 : c.start;
  $("seek").max = sourcePreview ? p.metadata.duration : c.end;
  $("seek").value = t;
  $("time").textContent =
    sourcePreview ? `원본 ${preciseTime(t)} / ${preciseTime(p.metadata.duration)}` : `${preciseTime(editedAt(c, t))} / ${preciseTime(editedDuration(c))} · 원본 ${preciseTime(t)}`;
  $("frame-info").textContent =
    `${Math.round(z * 100)}% · ${previewOverrides().some(f => containsFrame(f, t)) ? "이 프레임에 적용된 위치" : c.manual_frame ? "전체 기본 위치" : "중앙 고정"}`;
  const region = editingRegion(), shot = editingShot();
  $("frame-scope").textContent = shot ? `선택한 원본 컷 ${sourceShots().indexOf(shot)+1} · ${preciseTime(shot.start-c.start)} ~ ${preciseTime(shot.end-c.start)} 전체에 적용` : region ? `지금 조정하면 ${preciseTime(region.start-c.start)} ~ ${preciseTime(region.end-c.start)} 소재 구간에만 적용` : "지금 조정하면 쇼츠 전체의 기본 위치에 적용";
  $("whole-frame").hidden = !region && !shot;
  $("reset-frame").textContent = shot ? "이 원본 컷을 기본 위치로" : region ? "이 설명 구간을 기본 위치로" : "150% · 중앙 기본값으로";
  const controls = editingFrame();
  fieldValue("zoom", controls.zoom);
  fieldValue("center", controls.center);
  $("zoom-label").value = Math.round(controls.zoom*100)+"%";
  $("center-label").value = Math.round(controls.center*100)+"%";
}
let editQueue = Promise.resolve();
function edit(action, extra = {}, target = { pid, cid }) {
  if (!project()) return Promise.resolve();
  const targetProject = target.pid,
    targetClip = target.cid;
  const operation = editQueue
    .catch(() => {})
    .then(async () => {
      mutating = true;
      $("save-state").textContent = "저장 중…";
      try {
        const current = state.projects.find((p) => p.id === targetProject);
        const p = await api(`/api/projects/${targetProject}/edit`, {
          action,
          clip_id: targetClip,
          revision: current.revision,
          ...extra,
        });
        state.projects[
          state.projects.findIndex((p) => p.id === targetProject)
        ] = p;
        shownRevision = -1;
      } finally {
        mutating = false;
        $("save-state").textContent = "자동 저장";
      }
      render();
    });
  editQueue = operation;
  return operation;
}
async function change(changes) {
  return edit("update", { changes });
}
async function run(kind, args = {}) {
  const targetProject = pid;
  flushTitleDraft();
  await editQueue;
  if (kind === "encode" && !(await chooseExportFolder())) return;
  await api(`/api/projects/${targetProject}/jobs/${kind}`, args);
  await refresh();
  shownRevision = -1;
  render();
}
$("projects").onclick = safe(async (e) => {
  const remove = e.target.closest("[data-delete-project]");
  if (remove) {
    const target = remove.dataset.deleteProject;
    const p = state.projects.find((p) => p.id === target);
    if (!p || !confirm(`“${p.name}” 프로젝트를 목록에서 삭제할까요?\n원본 영상, 내보낸 영상과 기존 예약은 유지됩니다.`)) return;
    flushTitleDraft();
    await editQueue;
    await api(`/api/projects/${target}/delete`, {});
    state.projects = state.projects.filter((p) => p.id !== target);
    if (pid === target) {
      cancelDrag();
      pid = null;
      cid = null;
      shownRevision = -1;
      videoProject = null;
      selectedRegionId = null;
      selectedShotId = null;
      trimDraft = null;
      frameDraft = null;
      $("video").pause();
      $("video").removeAttribute("src");
      $("video").load();
      ++titleRequest;
      $("title-image").hidden = true;
      $("preview-placeholder").hidden = false;
      localStorage.removeItem("project");
      if (state.projects.length) selectProject(state.projects[0].id);
    }
    render();
    toast("프로젝트를 목록에서 삭제했습니다.");
    return;
  }
  const el = e.target.closest("[data-project]");
  if (el) selectProject(el.dataset.project);
});
$("clips").onclick = safe(async (e) => {
  const input = e.target.closest("[data-include]");
  if (input) {
    e.stopPropagation();
    await edit("update", {
      clip_id: input.dataset.include,
      changes: { included: input.checked },
    });
    render();
    return;
  }
  const card = e.target.closest("[data-clip]");
  if (card) selectClip(card.dataset.clip);
});
$("clips").onkeydown = (e) => {
  if (e.key === "Enter") e.target.click();
};
$("job-status").onclick = safe(async (e) => {
  const c = e.target.closest("[data-cancel]"),
    r = e.target.closest("[data-retry]");
  if (c) await api(`/api/jobs/${c.dataset.cancel}/cancel`, {});
  if (r) await api(`/api/jobs/${r.dataset.retry}/retry`, {});
  await refresh();
});
$("prepare-auto").onclick = safe(async () => {
  if (project().clips.length && !confirm("자동 준비를 새로 시작하면 현재 구간을 AI가 다시 나눕니다. 기존 편집은 되돌리기로 복구할 수 있습니다. 계속할까요?")) return;
  await run("prepare");
});
for (const button of document.querySelectorAll("[data-stage]"))
  button.onclick = safe(async () => {
    const kind = button.dataset.stage;
    if (
      kind === "analyze" &&
      project().clips.length &&
      !confirm(
        "AI 분할을 다시 제안하면 현재 구간 목록을 교체합니다. 기존 편집은 되돌리기로 복구할 수 있습니다. 계속할까요?",
      )
    )
      return;
    await run(kind);
  });
for (const id of ["new-project", "empty-import"])
  $(id).onclick = () => $("import-dialog").showModal();
$("choose-file").onclick = safe(async () => {
  const result = await api("/api/choose-file", {});
  if (result.path) $("source-path").value = result.path;
});
$("import-submit").onclick = safe(async () => {
  const path = $("source-path")
    .value.trim()
    .replace(/^['"]|['"]$/g, "");
  $("import-submit").disabled = true;
  try {
    const p = await api("/api/projects", { path });
    $("import-dialog").close();
    await refresh();
    selectProject(p.id);
  } finally {
    $("import-submit").disabled = false;
  }
});
$("settings-button").onclick = () => {
  $("model").innerHTML = state.models
    .map((m) => `<option>${esc(m)}</option>`)
    .join("");
  $("model").value = project().model;
  $("effort").value = project().effort;
  $("stt-model").value = project().stt_model;
  $("settings-dialog").showModal();
};
$("settings-save").onclick = safe(async () => {
  await edit("settings", {
    model: $("model").value,
    effort: $("effort").value,
    stt_model: $("stt-model").value,
  });
  $("settings-dialog").close();
});
$("add-clip").onclick = safe(async () => {
  const start = clip() ? clip().end : 0,
    end = Math.min(project().metadata.duration, start + 90);
  if (end <= start)
    throw Error("영상 끝입니다. 기존 구간을 나누거나 시간을 조정하세요.");
  await edit("add", { start, end });
  cid = project().clips.at(-1).id;
  shownRevision = -1;
  render();
});
$("undo").onclick = safe(() => edit("undo"));
$("hooks").onclick = safe(async (e) => {
  const b = e.target.closest("[data-hook]");
  if (b) {
    clearTimeout(titleTimer);
    titleDrafts.delete(`${pid}/${cid}`);
    const h = clip().hooks[Number(b.dataset.hook)];
    await change({ hook: h.text, yellow: h.yellow_phrase, confirmed: false });
  }
});
const titleDrafts = new Map();
let titleTimer;
function captureTitleDraft() {
  if (!clip()) return;
  const hook = $("hook").value;
  const yellow = hook.includes($("yellow").value) ? $("yellow").value : "";
  const draft = { pid, cid, changes: { hook, yellow,
    font_size: Number($("font-size").value), confirmed: false } };
  titleDrafts.set(`${pid}/${cid}`, draft);
  $("save-state").textContent = "저장 대기…";
  $("confirm-hook").textContent = "이 문구로 확정";
  clearTimeout(titleTimer);
  titleTimer = setTimeout(() => flushTitleDraft(), 300);
}
function flushTitleDraft() {
  clearTimeout(titleTimer);
  const key = `${pid}/${cid}`, draft = titleDrafts.get(key);
  if (!draft || draft.saving) return;
  draft.saving = true;
  edit("update", { changes: draft.changes }, draft).then(() => {
    if (titleDrafts.get(key) === draft) titleDrafts.delete(key);
  }).catch(e => {
    draft.saving = false;
    $("save-state").textContent = "저장 실패 · 입력 내용 유지";
    toast(e.message);
  });
}
for (const id of ["hook", "yellow", "font-size"]) {
  $(id).oninput = e => { if (!e.isComposing) captureTitleDraft(); };
  $(id).oncompositionend = captureTitleDraft;
  $(id).onchange = () => { captureTitleDraft(); flushTitleDraft(); };
}
$("confirm-hook").onclick = safe(async () => {
  captureTitleDraft();
  const key = `${pid}/${cid}`, draft = titleDrafts.get(key);
  clearTimeout(titleTimer);
  draft.saving = true;
  try {
    await edit("update", { changes: { ...draft.changes, confirmed: true } }, draft);
    if (titleDrafts.get(key) === draft) titleDrafts.delete(key);
  } catch (e) {
    draft.saving = false;
    $("save-state").textContent = "저장 실패 · 입력 내용 유지";
    throw e;
  }
});
$("regenerate").onclick = safe(() =>
  run("hooks", { clip_ids: [cid], fresh: true }),
);
$("auto-frame").onclick = safe(() => run("framing", { clip_ids: [cid] }));
async function saveFrame(frame) {
  if (!clip() || busy()) return;
  const region = editingRegion(), shot = editingShot(), scope = shot || region;
  if (scope) { $("video").pause(); if (!containsFrame(scope, previewTime())) seekFrame(scope.start); }
  const fallback = scope ? (clip().manual_frame || { zoom:1.5, center:.5, vertical:.5 }) : { zoom:1.5, center:.5, vertical:.5 };
  const draft = { pid, cid, scopeKey:frameScopeKey(), frame: frame || fallback };
  frameDraft = draft;
  updatePreview();
  try {
    if (shot?.id) await edit("frame_shot", { shot_id:shot.id, frame });
    else if (region?.id) await edit("frame_region", { suggestion_id: region.id, frame });
    else await change({ manual_frame: frame });
  } finally {
    if (frameDraft === draft) frameDraft = null;
    shownRevision = -1;
    render();
  }
}
for (const button of document.querySelectorAll("[data-zoom]")) {
  button.onclick = safe(() => saveFrame({ zoom:Number(button.dataset.zoom), center:editingFrame().center, vertical:editingFrame().vertical ?? .5 }));
}
for (const edge of ["start", "end"]) {
  $(`selected-region-now-${edge}`).onclick = () => {
    if (clip()) $(`selected-region-${edge}`).value = Math.max(0, Math.min(clip().end-clip().start, snapRegionTime(previewTime())-clip().start)).toFixed(6);
  };
}
$("selected-region-save").onclick = safe(async () => {
  const region = editingRegion(), c = clip();
  if (!region || !c || busy()) return;
  const start = $("selected-region-start"), end = $("selected-region-end");
  if (!start.value || !end.value || !start.reportValidity() || !end.reportValidity()) throw Error("시작·끝 시간을 입력해 주세요.");
  await edit("frame_region_time", { suggestion_id:region.id, start:snapRegionTime(c.start+Number(start.value)), end:snapRegionTime(c.start+Number(end.value)) });
  $("region-trim-status").textContent = "저장됨";
});
$("add-frame-region").onclick = safe(async () => {
  const c = clip();
  if (!c || busy()) return;
  const start = $("custom-region-start"), end = $("custom-region-end");
  if (!start.value || !end.value || !start.reportValidity() || !end.reportValidity()) throw Error("시작·끝 시간을 입력해 주세요.");
  const previous = new Set(informationRegions().map(s => s.id));
  await edit("frame_region_add", { start:snapRegionTime(c.start+Number(start.value)), end:snapRegionTime(c.start+Number(end.value)) });
  const index = informationRegions().findIndex(s => !previous.has(s.id));
  if (index >= 0) inspectInformation(index);
  $("custom-region").open = false;
});
$("reset-frame").onclick = safe(() => saveFrame(null));
for (const b of document.querySelectorAll("[data-align]")) {
  b.onclick = safe(() => {
    const center = Number(b.dataset.align);
    return saveFrame({ ...editingFrame(), center,
      ...(center === 0.5 ? { vertical: 0.5 } : {}) });
  });
}
for (const id of ["zoom", "center"]) {
  $(id).oninput = () => {
    if (!clip() || busy()) return;
    frameDraft = { pid, cid, scopeKey:frameScopeKey(), frame: { ...editingFrame(), [id]: Number($(id).value) } };
    $(id + "-label").value = Math.round(Number($(id).value) * 100) + "%";
    updatePreview();
  };
  $(id).onchange = safe(() => saveFrame({ ...editingFrame(), [id]: Number($(id).value) }));
}
function inspectInformation(index) {
  $("frame-details").open = true;
  const s = informationRegions()[index];
  selectedRegionId = s.id || null;
  selectedShotId = null;
  trimDraft = null;
  $("region-trim-status").textContent = "";
  $("video").pause();
  seekFrame(s.start);
  shownRevision = -1;
  render();
  $("preview-region").scrollIntoView({ block: "nearest", behavior: "smooth" });
  return s;
}
const informationClick = safe(async (e) => {
  if (busy()) return;
  const card = e.target.closest("[data-region-id]");
  const now = e.target.closest("[data-region-now]");
  if (now && card) {
    card.querySelector(`[data-region-${now.dataset.regionNow}]`).value = Math.max(0, Math.min(clip().end-clip().start, snapRegionTime(previewTime())-clip().start)).toFixed(6);
  }
  if (e.target.closest("[data-save-region-time]") && card) {
    const start = card.querySelector("[data-region-start]"), end = card.querySelector("[data-region-end]");
    if (!start.value || !end.value || !start.reportValidity() || !end.reportValidity()) throw Error("적용 시작·끝 시간을 입력해 주세요.");
    const regionId = card.dataset.regionId;
    await edit("frame_region_time", { suggestion_id: regionId,
      start: snapRegionTime(clip().start + Number(start.value)),
      end: snapRegionTime(Math.min(clip().end, clip().start + Number(end.value))) });
    const index = informationRegions().findIndex(s => s.id === regionId);
    if (index >= 0) inspectInformation(index);
    toast("위치 적용 시간을 저장했습니다.");
  }
  const inspect = e.target.closest("[data-inspect]"), apply = e.target.closest("[data-apply-region]");
  if (inspect) inspectInformation(Number(inspect.dataset.inspect));
  if (apply) {
    const s = inspectInformation(Number(apply.dataset.applyRegion));
    await saveFrame({ zoom:s.zoom, center:s.center, vertical:.5 });
  }
});
$("frame-suggestions").onclick = informationClick;
$("information-markers").onclick = informationClick;
$("whole-frame").onclick = () => {
  selectedRegionId = null;
  selectedShotId = null;
  trimDraft = null;
  const c = clip();
  // Jump to an unadjusted part so the editing scope is unambiguous.
  let t = c.start;
  for (const f of c.frame_overrides || []) if (t >= f.start && t < f.end) t = f.end;
  if (t >= c.end) t = c.start;
  $("video").pause(); $("video").currentTime = t;
  shownRevision = -1; render();
};
$("video").onpointerdown = (e) => {
  if (e.button !== 0 || !clip() || busy() || mutating || frameDraft) return;
  e.preventDefault();
  $("video").pause();
  $("video").focus({ preventScroll: true });
  dragging = { pointer: e.pointerId, pid, cid, startX: e.clientX, startY: e.clientY,
    frame: { ...editingFrame() }, geometry: frameGeometry(editingFrame()),
    ratio: $("preview").clientWidth / 1080 };
  $("video").setPointerCapture(e.pointerId);
  $("video").classList.add("dragging");
};
$("video").onpointermove = (e) => {
  if (!dragging || dragging.pointer !== e.pointerId) return;
  if (dragging.pid !== pid || dragging.cid !== cid || busy()) return cancelDrag();
  const d = dragging, g = d.geometry;
  if (!frameDraft && Math.hypot(e.clientX-d.startX, e.clientY-d.startY) < 3) return;
  const x = Math.max(0, Math.min(g.sw - 1080, g.x - (e.clientX - d.startX) / d.ratio));
  const y = Math.max(0, Math.min(1920 - g.sh, g.y + (e.clientY - d.startY) / d.ratio));
  frameDraft = { pid, cid, scopeKey:frameScopeKey(), frame: { ...d.frame,
    center: (x + 540) / g.sw, vertical: y / (1920 - g.sh) } };
  updatePreview();
};
function cancelDrag() {
  if (!dragging) return;
  const pointer = dragging.pointer;
  dragging = null;
  frameDraft = null;
  if ($("video").hasPointerCapture(pointer)) $("video").releasePointerCapture(pointer);
  $("video").classList.remove("dragging");
  updatePreview();
}
$("video").onpointerup = safe(async (e) => {
  if (!dragging || dragging.pointer !== e.pointerId) return;
  const frame = frameDraft?.frame;
  const sameClip = dragging.pid === pid && dragging.cid === cid;
  cancelDrag();
  if (frame && sameClip) await saveFrame(frame);
});
$("video").onpointercancel = cancelDrag;
$("video").onlostpointercapture = cancelDrag;
$("video").onkeydown = safe(async (e) => {
  if (e.key === "Escape") return cancelDrag();
  if (!clip() || busy() || !["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(e.key)) return;
  e.preventDefault();
  const f = editingFrame(), step = e.shiftKey ? 0.05 : 0.01;
  const center = Math.max(0, Math.min(1, f.center + (e.key === "ArrowLeft" ? step : e.key === "ArrowRight" ? -step : 0)));
  const vertical = Math.max(0, Math.min(1, (f.vertical ?? 0.5) + (e.key === "ArrowUp" ? -step : e.key === "ArrowDown" ? step : 0)));
  await saveFrame({ ...f, center, vertical });
});
$("play").onclick = safe(async () => {
  const c = clip(),
    v = $("video");
  if (!c) return;
  if (v.paused) {
    if (sourcePreview) { if (v.currentTime >= project().metadata.duration) v.currentTime = 0; }
    else { v.currentTime = playbackStart(c, v.currentTime); }
    await v.play();
  } else v.pause();
});
let playbackFrame = null, videoFrameRequest = null, videoFrameGeneration = 0;
function checkPlayback() {
  const c = clip(), v = $("video");
  if (!c || v.paused || v.seeking || sourcePreview) return;
  const ranges = keptRanges(c), next = ranges.find(r => v.currentTime < r.end);
  if (!next) { v.pause(); return; }
  if (v.currentTime < next.start) v.currentTime = next.start;
}
function watchPlayback() {
  checkPlayback();
  if (!$("video").requestVideoFrameCallback) updatePreview($("video").currentTime);
  if (!$("video").paused) playbackFrame = requestAnimationFrame(watchPlayback);
}
function watchVideoFrames() {
  const v = $("video");
  if (!v.requestVideoFrameCallback || videoFrameRequest !== null) return;
  const targetProject = pid, targetClip = cid, generation = videoFrameGeneration;
  videoFrameRequest = v.requestVideoFrameCallback((_, metadata) => {
    if (generation !== videoFrameGeneration) return;
    videoFrameRequest = null;
    if (targetProject !== pid || targetClip !== cid || v.seeking) { watchVideoFrames(); return; }
    presentedTime = { pid, cid, mediaTime:metadata.mediaTime };
    updatePreview(metadata.mediaTime);
    if (!v.paused) watchVideoFrames();
  });
}
$("video").onplay = () => {
  $("play").textContent = "Ⅱ";
  cancelAnimationFrame(playbackFrame);
  watchPlayback();
  watchVideoFrames();
};
$("video").onpause = () => {
  $("play").textContent = "▶";
  cancelAnimationFrame(playbackFrame);
  updatePreview();
};
$("video").ontimeupdate = () => {
  checkPlayback();
  // Decoded mediaTime drives crop changes during playback, without timeupdate's delay.
  if ($("video").paused || !$("video").requestVideoFrameCallback) updatePreview();
};
$("video").onseeking = () => { presentedTime = null; updatePreview($("video").currentTime); watchVideoFrames(); };
$("video").onseeked = () => { updatePreview($("video").currentTime); watchVideoFrames(); };
$("video").addEventListener("loadeddata", watchVideoFrames);
$("video").addEventListener("emptied", () => {
  videoFrameGeneration++;
  presentedTime = null;
  if (videoFrameRequest !== null) $("video").cancelVideoFrameCallback?.(videoFrameRequest);
  videoFrameRequest = null;
});
$("seek").oninput = () => { sourcePreview ? seekSource(Number($("seek").value)) : seekFrame(Number($("seek").value), false); };
new ResizeObserver(updatePreview).observe($("preview"));
new ResizeObserver(() => { clipTimelineKey=""; renderClipTimeline(); }).observe($("clip-timeline-scroll"));
new ResizeObserver(() => { shotTimelineKey = ""; renderShotTimeline(); }).observe($("shot-timeline-scroll"));
$("content-cuts").onclick = safe(async (event) => {
  const button = event.target.closest("[data-restore-cut]");
  if (button) await edit("restore_cut", { index: Number(button.dataset.restoreCut) });
});
$("download").onclick = safe(async () => {
  if (!clip()?.render) throw Error("먼저 인코딩을 완료해 주세요.");
  window.open(`/api/projects/${pid}/clips/${cid}/output`, "_blank");
});
$("reveal").onclick = safe(() => api(`/api/projects/${pid}/reveal`, {}));
async function loadReservations() {
  const result = await api("/api/reservations");
  records = result.records;
  $("reservations").innerHTML =
    records
      .filter((r) => r.project_id === pid)
      .map(
        (r) =>
          `<div class="reservation"><h3>${esc(r.title)}</h3>${r.deliveries.map((d) => `<div class="row"><span class="badge">${esc(d.service)}</span><span>${esc(statusNames[d.status] || d.status)}</span><small>${new Date(d.due_at).toLocaleString("ko-KR")}</small>${d.post_id ? `<small>ID ${esc(d.post_id)}</small>` : ""}${d.error ? `<small class="warning">${esc(d.error)}</small>` : ""}${d.status === "unknown" ? `<button data-reconcile="${r.id}" data-channel-id="${d.channel_id}">Buffer 게시물 ID 연결</button>` : ""}</div>`).join("")}<div class="row">${r.deleted_at ? `<small>${esc(r.deletion_reason)}</small>` : `<button data-delete-reservation="${r.id}">예약 취소·원격 영상 정리</button>`}</div></div>`,
      )
      .join("") || '<p class="muted">아직 예약한 영상이 없습니다.</p>';
}
function savedSchedulePreferences() {
  try { return JSON.parse(localStorage.getItem("schedulePreferences") || "{}"); }
  catch { return {}; }
}
function monthShift(amount) {
  const [y, m] = calendarMonth.split("-").map(Number);
  const d = new Date(Date.UTC(y, m - 1 + amount, 1));
  calendarMonth = d.toISOString().slice(0, 7);
  renderCalendar();
}
function renderCalendar() {
  if (!calendarPlan) return;
  const [year, month] = calendarMonth.split("-").map(Number);
  $("calendar-month").textContent = `${year}년 ${month}월`;
  const padding = new Date(Date.UTC(year, month - 1, 1)).getUTCDay();
  const count = new Date(Date.UTC(year, month, 0)).getUTCDate();
  let html = '<div class="calendar-blank"></div>'.repeat(padding);
  for (let d = 1; d <= count; d++) {
    const date = `${calendarMonth}-${String(d).padStart(2, "0")}`;
    const item = calendarPlan.items.find(i => i.date === date);
    const existing = calendarPlan.existing.filter(i => i.date === date);
    const names = [...new Set(existing.map(i => i.title))];
    html += `<div class="calendar-day ${item ? "planned" : existing.length ? "occupied" : ""}"><strong>${d}</strong>${item ? `<time>낮 12:00</time><span>SHORT ${String(item.number).padStart(2,"0")}</span><p>${esc(item.title)}</p>` : names.length ? `<small>기존 예약</small>${names.map(n => `<p>${esc(n)}</p>`).join("")}` : ""}</div>`;
  }
  $("calendar-grid").innerHTML = html;
  $("calendar-agenda").innerHTML = calendarPlan.items.map(i =>
    `<div class="calendar-agenda-row"><time>${i.date} · 낮 12시</time><span>${esc(i.title)}</span></div>`).join("");
  $("schedule-summary").textContent = `${calendarPlan.items.length}개 쇼츠 · ${calendarPlan.items[0].date} ~ ${calendarPlan.items.at(-1).date} · ${calendarPlan.channels.map(c => c.displayName || c.name).join(" / ")}`;
}
async function buildCalendar(initial = false) {
  const requestId = ++calendarRequest;
  calendarPlan = null;
  $("schedule-submit").disabled = true;
  $("calendar-message").textContent = "이 앱에 저장된 예약을 기준으로 달력을 배치하고 있습니다…";
  $("calendar-grid").innerHTML = $("calendar-agenda").innerHTML = "";
  const selected = initial ? undefined : [...document.querySelectorAll("[data-channel]:checked")].map(c => c.dataset.channel);
  try {
    await editQueue;
    const plan = await api(`/api/projects/${calendarProject}/calendar`, {
      start_date: $("calendar-start").value || undefined,
      channel_ids: selected,
      youtube_privacy: $("privacy").value,
    });
    if (requestId !== calendarRequest || !$("schedule-dialog").open) return;
    if (initial) channels = plan.channels;
    calendarPlan = plan;
    if (initial) $("channels").innerHTML = channels.map(c => `<label class="check"><input type="checkbox" data-channel="${esc(c.id)}" checked>${esc(c.displayName || c.name)} · ${esc(c.service)}</label>`).join("");
    $("calendar-start").value = plan.start_date;
    calendarMonth = plan.items[0].date.slice(0, 7);
    $("calendar-message").textContent = "날짜와 문구를 확인해 주세요. 아직 인코딩·예약을 시작하지 않았습니다.";
    renderCalendar();
    $("schedule-submit").disabled = false;
  } catch (e) {
    if (requestId === calendarRequest) $("calendar-message").textContent = e.message;
  }
}
async function chooseExportFolder(target = pid) {
  const p = await api(`/api/projects/${target}/export-folder`, {});
  if (p.cancelled) return false;
  await refresh();
  return true;
}
$("schedule-open").onclick = safe(async () => {
  await editQueue;
  if (!project().clips.some(c => c.included)) throw Error("예약할 쇼츠를 체크해 주세요.");
  calendarProject = pid;
  $("calendar-start").value = "";
  $("privacy").value = savedSchedulePreferences().youtube_privacy || "public";
  $("channels").innerHTML = "";
  $("schedule-summary").textContent = "";
  $("schedule-dialog").showModal();
  await buildCalendar(true);
});
$("calendar-refresh").onclick = safe(() => buildCalendar(!$("channels").querySelector("input")));
$("calendar-start").oninput = safe(() => {
  if (!$("calendar-start").value || !$("calendar-start").validity.valid) {
    calendarRequest++;
    calendarPlan = null;
    $("schedule-submit").disabled = true;
    $("calendar-message").textContent = "시작일을 입력해 주세요.";
    $("calendar-grid").innerHTML = $("calendar-agenda").innerHTML = "";
    return;
  }
  return buildCalendar(!$("channels").querySelector("input"));
});
$("channels-refresh").onclick = safe(async () => {
  $("channels-refresh").disabled = true;
  try {
    await api("/api/channels/refresh", {});
    await buildCalendar(true);
  } finally {
    $("channels-refresh").disabled = false;
  }
});
$("channels").onchange = safe(() => buildCalendar());
$("privacy").onchange = safe(() => buildCalendar(!$("channels").querySelector("input")));
$("calendar-prev").onclick = () => calendarPlan && monthShift(-1);
$("calendar-next").onclick = () => calendarPlan && monthShift(1);
$("schedule-dialog").addEventListener("close", () => { calendarRequest++; calendarPlan = null; });
$("schedule-submit").onclick = safe(async () => {
  if (!calendarPlan) return;
  const plan = calendarPlan;
  $("schedule-submit").disabled = true;
  try {
    await editQueue;
    if (!(await chooseExportFolder(calendarProject))) {
      $("schedule-submit").disabled = false;
      return;
    }
    await api(`/api/projects/${calendarProject}/calendar/${plan.id}/confirm`, {});
    localStorage.setItem("schedulePreferences", JSON.stringify({ youtube_privacy: plan.youtube_privacy }));
    $("schedule-dialog").close();
    await refresh();
    toast("달력을 확정했습니다. 인코딩 후 해당 날짜로 자동 예약합니다.");
  } catch (e) {
    $("calendar-message").textContent = e.message;
    $("schedule-submit").disabled = false;
  }
});
$("refresh-reservations").onclick = safe(() => run("refresh"));
$("reservations").onclick = safe(async (e) => {
  const link = e.target.closest("[data-reconcile]");
  if (link) {
    const post_id = prompt(
      "Buffer에서 확인한 게시물 ID를 입력하세요. 채널·문구·예약 시간이 일치하는지 검사합니다.",
    );
    if (post_id) {
      await api(`/api/projects/${pid}/reconcile`, {
        record_id: link.dataset.reconcile,
        channel_id: link.dataset.channelId,
        post_id: post_id.trim(),
      });
      await loadReservations();
    }
    return;
  }
  const b = e.target.closest("[data-delete-reservation]");
  if (
    b &&
    confirm(
      "해당 쇼츠의 예약을 취소하고 R2 복사본을 정리할까요? Mac 영상은 보존합니다.",
    )
  )
    await run("delete_reservation", { record_id: b.dataset.deleteReservation });
});
safe(async () => {
  await refresh();
  await loadReservations();
})();
setInterval(() => safe(refresh)(), 2000);
setInterval(() => safe(loadReservations)(), 10000);
