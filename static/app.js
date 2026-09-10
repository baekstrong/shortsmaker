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
let channels = [],
  records = [],
  audio = null,
  mutating = false,
  polling = false;
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
  );
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
      if (
        ["succeeded", "failed", "cancelled", "interrupted"].includes(
          j.status,
        ) &&
        !seen.has(j.id)
      ) {
        if (refresh.ready) {
          beep();
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
  cancelDrag();
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
        `<article tabindex="0" role="button" data-clip="${c.id}" class="clip-card ${c.id === cid ? "active" : ""} ${!c.included ? "excluded" : ""}"><div class="row"><span class="badge">SHORT ${String(i + 1).padStart(2, "0")}</span><input type="checkbox" data-include="${c.id}" ${c.included ? "checked" : ""} aria-label="${i + 1}번 쇼츠 작업에 포함" ${busy() ? "disabled" : ""}></div><h3>${esc(c.title)}</h3><small>${time(c.start)} – ${time(c.end)} · ${(c.end - c.start).toFixed(1)}초</small><div><small class="${c.end - c.start > 180 ? "warning" : ""}">${c.end - c.start > 180 ? "3분 초과 · 추가 분할 필요" : c.confirmed ? "✓ 문구 확정" : "문구 확인 대기"}${c.frame_suggestions?.length ? " · 화면 조정 제안 있음" : ""}</small></div></article>`,
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
    "[data-stage],#schedule-open,#add-clip,#undo,[data-align]",
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
  $("editor-empty").hidden = !!c;
  $("preview-placeholder").hidden = !!c;
  $("download").disabled = !c || !c.render_current;
  $("title-image").hidden = !c;
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
  fieldValue("hook", c.hook);
  fieldValue("yellow", c.yellow);
  fieldValue("font-size", c.font_size);
  $("confirm-hook").textContent = c.confirmed
    ? "✓ 문구 확정됨"
    : "이 문구로 확정";
  $("recommendation").textContent =
    c.recommendation_reason || "AI가 10개 후보 중 가장 좋은 문구를 추천합니다.";
  $("hooks").innerHTML = c.hooks
    .map(
      (h, i) =>
        `<button class="hook-option ${i === c.recommended_index ? "recommended" : ""} ${h.text === c.hook ? "selected" : ""}" data-hook="${i}">${i === c.recommended_index ? "<strong>✦ AI 추천</strong>" : ""}${esc(h.text)}</button>`,
    )
    .join("");
  const f = activeFrame();
  fieldValue("zoom", f.zoom);
  fieldValue("center", f.center);
  $("zoom-label").value = Math.round(f.zoom * 100) + "%";
  $("center-label").value = Math.round(f.center * 100) + "%";
  $("frame-suggestions").innerHTML = !Array.isArray(c.frame_suggestions)
    ? '<p class="muted">그림·글 확인을 실행하면 조정이 필요한 구간을 제안합니다.</p>'
    : c.frame_suggestions.length ? c.frame_suggestions.map((s, i) =>
      `<div class="frame-suggestion"><button data-inspect="${i}">${time(s.time)} · ${esc(s.direction)}</button><p>${esc(s.reason)}</p><small>제안 배율 ${Math.round(s.zoom * 100)}%${s.confidence < 0.8 ? " · 판단 불확실" : ""} · 자동 적용 안 함</small></div>`).join("")
    : '<p class="muted">그림·글 조정 제안이 없습니다. 필요하면 직접 위치를 조정하세요.</p>';
  $("title-image").src =
    `/api/projects/${pid}/clips/${c.id}/title.png?v=${project().revision}`;
  for (const el of $("editor").querySelectorAll("button,input,textarea"))
    if (busy()) el.disabled = true;
    else el.disabled = false;
}
function activeFrame() {
  if (frameDraft?.pid === pid && frameDraft?.cid === cid) return frameDraft.frame;
  return clip()?.manual_frame || { zoom: 1.5, center: 0.5, vertical: 0.5 };
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
    `${Math.round(z * 100)}% · ${c.manual_frame || frameDraft ? "직접 조정 · 구간 전체 고정" : "중앙 고정"}`;
}
let editQueue = Promise.resolve();
function edit(action, extra = {}) {
  if (!project()) return Promise.resolve();
  const targetProject = pid,
    targetClip = cid;
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
  await editQueue;
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
    cancelDrag();
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
    const h = clip().hooks[Number(b.dataset.hook)];
    await change({ hook: h.text, yellow: h.yellow_phrase, confirmed: false });
  }
});
$("hook").onchange = safe(() => {
  const hook = $("hook").value;
  return change({
    hook,
    yellow: hook.includes($("yellow").value) ? $("yellow").value : "",
    confirmed: false,
  });
});
$("yellow").onchange = safe(() =>
  change({ yellow: $("yellow").value, confirmed: false }),
);
$("font-size").onchange = safe(() =>
  change({ font_size: Number($("font-size").value) }),
);
$("confirm-hook").onclick = safe(() =>
  change({ hook: $("hook").value, yellow: $("yellow").value, confirmed: true }),
);
$("regenerate").onclick = safe(() =>
  run("hooks", { clip_ids: [cid], fresh: true }),
);
$("auto-frame").onclick = safe(() => run("framing", { clip_ids: [cid] }));
async function saveFrame(frame) {
  if (!clip() || busy()) return;
  const draft = { pid, cid, frame: frame || { zoom: 1.5, center: 0.5, vertical: 0.5 } };
  frameDraft = draft;
  updatePreview();
  try {
    await change({ manual_frame: frame });
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
$("frame-suggestions").onclick = (e) => {
  const b = e.target.closest("[data-inspect]");
  if (!b || busy()) return;
  const s = clip().frame_suggestions[Number(b.dataset.inspect)];
  $("video").pause();
  $("video").currentTime = s.time;
  $("preview").scrollIntoView({ block: "center", behavior: "smooth" });
  updatePreview();
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
$("schedule-open").onclick = safe(async () => {
  const selected = project().clips.filter((c) => c.included);
  if (
    !selected.length ||
    selected.some((c) => !c.confirmed || !c.render_current)
  )
    throw Error(
      "포함된 쇼츠의 문구 확정과 현재 편집본 인코딩을 먼저 완료해 주세요.",
    );
  $("schedule-dialog").showModal();
  const data = await api("/api/channels");
  channels = data.channels;
  $("channels").innerHTML = channels
    .map(
      (c) =>
        `<label class="check"><input type="checkbox" data-channel="${c.id}" checked>${esc(c.displayName || c.name)} · ${esc(c.service)}</label>`,
    )
    .join("");
  const t = new Date(Date.now() + 24 * 3600000);
  t.setMinutes(t.getMinutes() - t.getTimezoneOffset());
  $("due-at").value = t.toISOString().slice(0, 16);
  $("schedule-summary").textContent =
    `쇼츠 ${project().clips.filter((c) => c.included).length}개를 예약합니다.`;
});
$("schedule-submit").onclick = safe(async () => {
  const due = new Date($("due-at").value);
  if (!Number.isFinite(due.getTime()))
    throw Error("예약 시간을 입력해 주세요.");
  await run("schedule", {
    clip_ids: project()
      .clips.filter((c) => c.included)
      .map((c) => c.id),
    channel_ids: [...document.querySelectorAll("[data-channel]:checked")].map(
      (x) => x.dataset.channel,
    ),
    due_at: due.toISOString(),
    spacing_minutes: Number($("spacing").value),
    youtube_privacy: $("privacy").value,
  });
  $("schedule-dialog").close();
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
