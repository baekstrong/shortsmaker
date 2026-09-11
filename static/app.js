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
function beep() {
  if (!sound || !audio) return;
  const osc = audio.createOscillator(),
    gain = audio.createGain();
  osc.connect(gain);
  gain.connect(audio.destination);
  osc.frequency.value = 660;
  gain.gain.setValueAtTime(0.05, audio.currentTime);
  gain.gain.exponentialRampToValueAtTime(0.001, audio.currentTime + 0.35);
  osc.start();
  osc.stop(audio.currentTime + 0.35);
}
document.addEventListener(
  "pointerdown",
  () => {
    if (!audio) audio = new AudioContext();
    if (audio.state === "suspended") audio.resume();
  },
  { once: true },
);
$("sound").onclick = () => {
  sound = !sound;
  localStorage.setItem("sound", sound ? "on" : "off");
  $("sound").textContent = sound ? "♪ 알림 켜짐" : "♪ 알림 꺼짐";
};
$("sound").textContent = sound ? "♪ 알림 켜짐" : "♪ 알림 꺼짐";
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
  flushTitleDraft();
  cancelDrag();
  selectedRegionId = null;
  trimDraft = null;
  pid = id;
  cid = null;
  shownRevision = -1;
  localStorage.setItem("project", id);
  $("video").pause();
  render();
  safe(loadReservations)();
}
function render() {
  const p = project();
  $("projects").innerHTML = state.projects
    .map(
      (p) =>
        `<button data-project="${p.id}" class="${p.id === pid ? "active" : ""}">${esc(p.name)}<small>${time(p.metadata.duration)} · 쇼츠 ${p.clips.length}개</small></button>`,
    )
    .join("");
  $("empty").hidden = !!p;
  $("workspace").hidden = !p;
  $("settings-button").disabled = !p;
  if (!p) return;
  $("project-name").textContent = p.name;
  $("project-meta").textContent =
    `${p.metadata.width} × ${p.metadata.height} · ${time(p.metadata.duration)} · 원본 보존 · ${p.model}`;
  $("clip-count").textContent = p.clips.length;
  $("summary").textContent =
    p.summary ||
    "내용 분석을 시작하면 AI가 구간을 제안합니다. 직접 추가할 수도 있습니다.";
  if (!clip()) cid = p.clips[0]?.id;
  $("clips").innerHTML = p.clips
    .map(
      (c, i) =>
        `<article tabindex="0" role="button" data-clip="${c.id}" class="clip-card ${c.id === cid ? "active" : ""} ${!c.included ? "excluded" : ""}"><div class="row"><span class="badge">SHORT ${String(i + 1).padStart(2, "0")}</span><input type="checkbox" data-include="${c.id}" ${c.included ? "checked" : ""} aria-label="${i + 1}번 쇼츠 작업에 포함" ${busy() ? "disabled" : ""}></div><h3>${esc(c.title)}</h3><small>${time(c.start)} – ${time(c.end)} · ${(c.end - c.start).toFixed(1)}초</small><div><small class="${c.end - c.start > 180 ? "warning" : ""}">${c.end - c.start > 180 ? "3분 초과 · 추가 분할 필요" : c.confirmed ? "✓ 문구 확정" : "문구 확인 대기"}${c.frame_suggestions?.length ? ` · 설명 화면 ${c.frame_suggestions.length}곳` : ""}</small></div></article>`,
    )
    .join("");
  const relevant = state.jobs.filter((j) => j.project_id === pid).slice(0, 2);
  $("job-status").innerHTML = relevant
    .map(
      (j) =>
        `<div class="job ${j.status}"><span>${esc(j.message)}</span>${j.progress !== null && j.status === "running" ? `<progress max="100" value="${j.progress}"></progress>` : ""}${["running", "queued"].includes(j.status) ? `<button data-cancel="${j.id}">중단</button>` : ["failed", "interrupted", "cancelled"].includes(j.status) ? `<button data-retry="${j.id}">재시도</button>` : "<small>✓ 완료</small>"}</div>`,
    )
    .join("");
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
      if (clip()) $("video").currentTime = clip().start;
      updatePreview();
    };
  }
  updatePreview();
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
  if (!c) return;
  const v = $("video");
  if (
    videoProject === pid &&
    v.readyState > 0 &&
    (v.currentTime < c.start || v.currentTime > c.end)
  )
    v.currentTime = c.start;
  fieldValue("clip-title", c.title);
  fieldValue("start", c.start);
  fieldValue("end", c.end);
  const draft = titleDrafts.get(`${pid}/${cid}`);
  fieldValue("hook", draft?.changes.hook ?? c.hook);
  fieldValue("yellow", draft?.changes.yellow ?? c.yellow);
  fieldValue("font-size", draft?.changes.font_size ?? c.font_size);
  $("confirm-hook").textContent = c.confirmed
    ? "✓ 문구 확정됨"
    : "이 문구로 확정";
  $("recommendation").textContent =
    c.recommendation_reason || "AI가 10개 후보 중 가장 좋은 문구를 추천합니다.";
  $("hooks").innerHTML = c.hooks
    .map(
      (h, i) =>
        `<button class="hook-option ${i === c.recommended_index ? "recommended" : ""} ${h.text === c.hook ? "selected" : ""}" data-hook="${i}">${i === c.recommended_index ? "<strong>✦ AI 추천</strong>" : ""}${esc(h.text)}${h.approach ? `<small class="hook-evaluation">${esc(h.approach)} · ${esc(h.evaluation || "")}</small>` : ""}</button>`,
    )
    .join("");
  const f = activeFrame();
  fieldValue("zoom", f.zoom);
  fieldValue("center", f.center);
  $("zoom-label").value = Math.round(f.zoom * 100) + "%";
  $("center-label").value = Math.round(f.center * 100) + "%";
  const regions = informationRegions();
  $("frame-suggestions").innerHTML = !Array.isArray(c.frame_suggestions)
    ? '<p class="muted">설명 화면 찾기를 실행하면 그림·글·각도 표시의 등장 구간을 잡아줍니다.</p>'
    : regions.length ? regions.map((s, i) => {
      const applied = c.frame_overrides?.find(f => f.id === s.id);
      return `<div data-region-id="${esc(s.id)}" class="frame-suggestion ${editingRegion()?.id === s.id ? "selected" : ""}">
        <button class="information-inspect" data-inspect="${i}">${s.thumbnail ? `<img src="/api/projects/${pid}/clips/${c.id}/information/${s.id}.jpg?v=${project().revision}" alt="설명 자료가 등장하는 원본 화면" loading="lazy">` : ""}
        <strong>${esc(s.subject || "설명 자료")}</strong><span>이 쇼츠 ${time(s.start-c.start)} ~ ${time(s.end-c.start)}</span><small>원본 ${time(s.start)} ~ ${time(s.end)}</small></button>
        <p>${esc(s.reason)}</p><small>추천: ${esc(s.direction)} · ${Math.round(s.zoom*100)}%${s.confidence < .8 ? " · 확인 필요" : ""}</small>
        <div class="region-time-editor">
          <small>위치 적용 시간 · 이 쇼츠 시작부터 초 단위</small>
          <div class="row">
            <label>적용 시작 (초)<input data-region-start type="number" min="0" max="${Number((c.end-c.start).toFixed(3))}" step="0.001" value="${Number((s.start-c.start).toFixed(3))}"></label>
            <label>적용 끝 (초)<input data-region-end type="number" min="0" max="${Number((c.end-c.start).toFixed(3))}" step="0.001" value="${Number((s.end-c.start).toFixed(3))}"></label>
          </div>
          <div class="row"><button data-region-now="start">현재 재생 위치를 시작으로</button><button data-region-now="end">현재 재생 위치를 끝으로</button><button data-save-region-time>시간 적용</button></div>
        </div>
        <div class="row"><button data-inspect="${i}">이 구간 위치 조정</button>${s.id ? `<button data-apply-region="${i}">${applied ? "추천 위치 다시 적용" : "추천 위치 적용"}</button>` : ""}</div>
        ${applied ? `<small>✓ ${applied.origin === 'ai' ? 'AI 추천 위치 자동 적용됨 · 필요하면 직접 조정하세요' : '직접 조정한 위치 적용됨'}</small>` : '<small>기존 직접 조정 구간과 겹치면 자동 적용을 건너뜁니다.</small>'}
      </div>`;
    }).join("")
    : '<p class="muted">설명 그림·글·각도 표시를 찾지 못했습니다. 필요하면 다시 분석해 주세요.</p>';
  $("information-markers").innerHTML = regions.map((s,i) => `<button data-inspect="${i}" title="${esc(s.subject || s.reason)}">${time(s.start-c.start)}–${time(s.end-c.start)} 설명</button>`).join("");
  loadTitlePreview();
  for (const el of document.querySelectorAll("#editor button,#editor input,#editor textarea,#hooks-pane button,#hooks-pane input,#hooks-pane textarea"))
    if (busy()) el.disabled = true;
    else el.disabled = false;
  $("download").disabled = busy() || !c.render_current;
}
let titleRequest = 0, titleURL = "", titleObjectURL = null, titleRetryAt = 0;
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
    $("title-image").src = objectURL;
    $("title-image").hidden = false;
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
  if (titleDrafts.size || trimDraft) { e.preventDefault(); e.returnValue = ""; }
});
function previewOverrides() {
  const frames = clip()?.frame_overrides || [];
  if (trimDraft?.pid !== pid || trimDraft?.cid !== cid) return frames;
  const existing = frames.find(f => f.id === trimDraft.id) || clip().frame_suggestions?.find(f => f.id === trimDraft.id);
  return existing ? [...frames.filter(f => f.id !== trimDraft.id), { ...existing, start:trimDraft.start, end:trimDraft.end }] : frames;
}
function updateRegionTimeline() {
  const c = clip(), region = informationRegions().find(s => s.id === selectedRegionId);
  $("preview-region").hidden = !region;
  if (!region || !c) return;
  const duration = c.end-c.start;
  const start = region.start-c.start, end = region.end-c.start;
  $("preview-region-name").textContent = region.subject || "직접 조정한 설명 구간";
  $("preview-region-time").textContent = `${start.toFixed(2)}초 ~ ${end.toFixed(2)}초`;
  for (const edge of ["start", "end"]) {
    const input = $(`region-trim-${edge}`);
    input.min = 0; input.max = duration;
    input.value = edge === "start" ? start : end;
    input.disabled = busy();
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
    const value = c.start + Number($(`region-trim-${edge}`).value);
    const start = edge === "start" ? Math.max(lower, Math.min(region.end-.01, value)) : region.start;
    const end = edge === "end" ? Math.min(upper, Math.max(region.start+.01, value)) : region.end;
    trimDraft = { pid, cid, id:region.id, start, end };
    $("video").pause();
    $("video").currentTime = edge === "start" ? start : Math.max(start, end-.001);
    $("region-trim-status").textContent = "조정 중 · 놓으면 자동 저장";
    const card = document.querySelector(`[data-region-id="${CSS.escape(region.id)}"]`);
    if (card) {
      card.querySelector("[data-region-start]").value = Number((start-c.start).toFixed(3));
      card.querySelector("[data-region-end]").value = Number((end-c.start).toFixed(3));
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
    return applied ? { ...s, start: applied.start, end: applied.end } : s;
  });
  for (const f of previewOverrides()) if (!regions.some(s => s.id === f.id))
    regions.push({ ...f, time:(f.start+f.end)/2, subject:"직접 조정한 설명 구간", reason:"이전에 저장한 구간 위치", direction:"저장된 위치", confidence:1 });
  return regions.sort((a,b) => a.start-b.start);
}
function editingRegion() {
  const t = $("video").currentTime;
  const regions = informationRegions();
  const selected = regions.find(s => s.id === selectedRegionId && t >= s.start && t < s.end);
  if (selected) return selected;
  const applied = clip()?.frame_overrides?.find(f => t >= f.start && t < f.end);
  return applied ? regions.find(s => s.id === applied.id) : null;
}
function activeFrame() {
  if (frameDraft?.pid === pid && frameDraft?.cid === cid) return frameDraft.frame;
  const c = clip(), t = $("video").currentTime;
  const f = previewOverrides().find(f => t >= f.start && t < f.end) || c?.manual_frame;
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
function updatePreview() {
  const p = project(),
    c = clip(),
    v = $("video");
  updateRegionTimeline();
  if (!p || !c) return;
  const t = v.currentTime;
  const s = activeFrame();
  const { z, sw, sh, x, y } = frameGeometry(s);
  const ratio = $("preview").clientWidth / 1080;
  v.style.width = sw * ratio + "px";
  v.style.height = sh * ratio + "px";
  v.style.left = -x * ratio + "px";
  v.style.top = y * ratio + "px";
  $("seek").min = c.start;
  $("seek").max = c.end;
  $("seek").value = Math.max(c.start, Math.min(c.end, t));
  $("time").textContent =
    `${time(Math.max(0, t - c.start))} / ${time(c.end - c.start)}`;
  $("frame-info").textContent =
    `${Math.round(z * 100)}% · ${previewOverrides().some(f => t >= f.start && t < f.end) ? "이 설명 구간에 적용된 위치" : c.manual_frame ? "전체 기본 위치" : "중앙 고정"}`;
  const region = editingRegion();
  $("frame-scope").textContent = region ? `지금 조정하면 ${time(region.start-c.start)} ~ ${time(region.end-c.start)} 구간에만 적용` : "지금 조정하면 쇼츠 전체의 기본 위치에 적용";
  $("whole-frame").hidden = !region;
  $("reset-frame").textContent = region ? "이 설명 구간을 기본 위치로" : "150% · 중앙 기본값으로";
  fieldValue("zoom", s.zoom);
  fieldValue("center", s.center);
  $("zoom-label").value = Math.round(s.zoom*100)+"%";
  $("center-label").value = Math.round(s.center*100)+"%";
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
  flushTitleDraft();
  await editQueue;
  if (kind === "encode" && !(await chooseExportFolder())) return;
  await api(`/api/projects/${pid}/jobs/${kind}`, args);
  await refresh();
  shownRevision = -1;
  render();
}
$("projects").onclick = (e) => {
  const el = e.target.closest("[data-project]");
  if (el) selectProject(el.dataset.project);
};
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
  if (card) {
    flushTitleDraft();
    cancelDrag();
    selectedRegionId = null;
    trimDraft = null;
    cid = card.dataset.clip;
    shownRevision = -1;
    $("video").currentTime = clip().start;
    render();
  }
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
$("clip-title").onchange = safe(() => change({ title: $("clip-title").value }));
for (const id of ["start", "end"])
  $(id).onchange = safe(() => change({ [id]: Number($(id).value) }));
$("set-start").onclick = safe(() =>
  change({ start: Number($("video").currentTime.toFixed(3)) }),
);
$("set-end").onclick = safe(() =>
  change({ end: Number($("video").currentTime.toFixed(3)) }),
);
$("split").onclick = safe(() => edit("split", { at: $("video").currentTime }));
$("merge").onclick = safe(() => edit("merge"));
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
  const region = editingRegion();
  const fallback = region ? (clip().manual_frame || { zoom:1.5, center:.5, vertical:.5 }) : { zoom:1.5, center:.5, vertical:.5 };
  const draft = { pid, cid, frame: frame || fallback };
  frameDraft = draft;
  updatePreview();
  try {
    if (region?.id) await edit("frame_region", { suggestion_id: region.id, frame });
    else await change({ manual_frame: frame });
  } finally {
    if (frameDraft === draft) frameDraft = null;
    shownRevision = -1;
    render();
  }
}
$("reset-frame").onclick = safe(() => saveFrame(null));
for (const b of document.querySelectorAll("[data-align]")) {
  b.onclick = safe(() => {
    const center = Number(b.dataset.align);
    return saveFrame({ ...activeFrame(), center,
      ...(center === 0.5 ? { vertical: 0.5 } : {}) });
  });
}
for (const id of ["zoom", "center"]) {
  $(id).oninput = () => {
    if (!clip() || busy()) return;
    frameDraft = { pid, cid, frame: { ...activeFrame(), [id]: Number($(id).value) } };
    $(id + "-label").value = Math.round(Number($(id).value) * 100) + "%";
    updatePreview();
  };
  $(id).onchange = safe(() => saveFrame({ ...activeFrame(), [id]: Number($(id).value) }));
}
function inspectInformation(index) {
  const s = informationRegions()[index];
  selectedRegionId = s.id || null;
  trimDraft = null;
  $("region-trim-status").textContent = "";
  $("video").pause();
  $("video").currentTime = s.time >= s.start && s.time < s.end ? s.time : (s.start+s.end)/2;
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
    card.querySelector(`[data-region-${now.dataset.regionNow}]`).value = Math.max(0, Math.min(clip().end-clip().start, $("video").currentTime-clip().start)).toFixed(3);
  }
  if (e.target.closest("[data-save-region-time]") && card) {
    const start = card.querySelector("[data-region-start]"), end = card.querySelector("[data-region-end]");
    if (!start.value || !end.value || !start.reportValidity() || !end.reportValidity()) throw Error("적용 시작·끝 시간을 입력해 주세요.");
    const regionId = card.dataset.regionId;
    await edit("frame_region_time", { suggestion_id: regionId,
      start: clip().start + Number(start.value),
      end: Math.min(clip().end, clip().start + Number(end.value)) });
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
  trimDraft = null;
  const c = clip();
  // Jump to an unadjusted part so the editing scope is unambiguous.
  let t = c.start;
  for (const f of c.frame_overrides || []) if (t >= f.start && t < f.end) t = f.end;
  if (t >= c.end) { toast("전체가 설명 구간으로 조정되어 있습니다. 구간 조정을 먼저 해제해 주세요."); return; }
  $("video").pause(); $("video").currentTime = t;
  shownRevision = -1; render();
};
$("video").onpointerdown = (e) => {
  if (e.button !== 0 || !clip() || busy() || mutating || frameDraft) return;
  e.preventDefault();
  $("video").pause();
  $("video").focus({ preventScroll: true });
  dragging = { pointer: e.pointerId, pid, cid, startX: e.clientX, startY: e.clientY,
    frame: { ...activeFrame() }, geometry: frameGeometry(activeFrame()),
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
  frameDraft = { pid, cid, frame: { ...d.frame,
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
  const f = activeFrame(), step = e.shiftKey ? 0.05 : 0.01;
  const center = Math.max(0, Math.min(1, f.center + (e.key === "ArrowLeft" ? step : e.key === "ArrowRight" ? -step : 0)));
  const vertical = Math.max(0, Math.min(1, (f.vertical ?? 0.5) + (e.key === "ArrowUp" ? -step : e.key === "ArrowDown" ? step : 0)));
  await saveFrame({ ...f, center, vertical });
});
$("play").onclick = safe(async () => {
  const c = clip(),
    v = $("video");
  if (!c) return;
  if (v.paused) {
    if (v.currentTime < c.start || v.currentTime >= c.end)
      v.currentTime = c.start;
    await v.play();
  } else v.pause();
});
$("video").onplay = () => ($("play").textContent = "Ⅱ");
$("video").onpause = () => ($("play").textContent = "▶");
$("video").ontimeupdate = () => {
  if (clip() && $("video").currentTime >= clip().end && !$("video").paused)
    $("video").pause();
  updatePreview();
};
$("seek").oninput = () => {
  $("video").currentTime = Number($("seek").value);
  updatePreview();
};
new ResizeObserver(updatePreview).observe($("preview"));
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
  $("calendar-message").textContent = "Buffer의 기존 예약을 확인하고 달력을 배치하고 있습니다…";
  $("calendar-grid").innerHTML = $("calendar-agenda").innerHTML = "";
  const preferences = savedSchedulePreferences();
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
    // Reuse saved channel choices only when all of them are still connected.
    if (initial && preferences.channel_ids?.length && preferences.channel_ids.every(id => plan.channels.some(c => c.id === id)) && preferences.channel_ids.length !== plan.channels.length) {
      $("channels").innerHTML = plan.channels.map(c => `<label class="check"><input type="checkbox" data-channel="${esc(c.id)}" ${preferences.channel_ids.includes(c.id) ? "checked" : ""}>${esc(c.displayName || c.name)} · ${esc(c.service)}</label>`).join("");
      return buildCalendar(false);
    }
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
    localStorage.setItem("schedulePreferences", JSON.stringify({ channel_ids: plan.channel_ids, youtube_privacy: plan.youtube_privacy }));
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
