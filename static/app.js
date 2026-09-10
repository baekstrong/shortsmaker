"use strict";
const $ = (id) => document.getElementById(id);
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
        `<article tabindex="0" role="button" data-clip="${c.id}" class="clip-card ${c.id === cid ? "active" : ""} ${!c.included ? "excluded" : ""}"><div class="row"><span class="badge">SHORT ${String(i + 1).padStart(2, "0")}</span><input type="checkbox" data-include="${c.id}" ${c.included ? "checked" : ""} aria-label="${i + 1}번 쇼츠 작업에 포함" ${busy() ? "disabled" : ""}></div><h3>${esc(c.title)}</h3><small>${time(c.start)} – ${time(c.end)} · ${(c.end - c.start).toFixed(1)}초</small><div><small class="${c.end - c.start > 180 ? "warning" : ""}">${c.end - c.start > 180 ? "3분 초과 · 추가 분할 필요" : c.confirmed ? "✓ 문구 확정" : "문구 확인 대기"}${c.framing.some((f) => f.review) ? " · 구도 확인 필요" : ""}</small></div></article>`,
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
    "[data-stage],#schedule-open,#add-clip,#undo",
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
  if (!c) return;
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
  $("manual-frame").checked = !!c.manual_frame;
  const f = c.manual_frame || c.framing[0] || { zoom: 1.5, center: 0.5 };
  fieldValue("zoom", f.zoom);
  fieldValue("center", f.center);
  $("zoom").disabled = $("center").disabled = !c.manual_frame;
  $("zoom-label").value = Math.round(f.zoom * 100) + "%";
  $("center-label").value = Math.round(f.center * 100) + "%";
  $("title-image").src =
    `/api/projects/${pid}/clips/${c.id}/title.png?v=${project().revision}`;
  for (const el of $("editor").querySelectorAll("button,input,textarea"))
    if (busy()) el.disabled = true;
    else if (!["zoom", "center"].includes(el.id)) el.disabled = false;
}
function updatePreview() {
  const p = project(),
    c = clip(),
    v = $("video");
  if (!p || !c) return;
  const t = v.currentTime;
  const s = c.manual_frame ||
    c.framing.find((s) => t >= s.start && t < s.end) || {
      zoom: 1.5,
      center: 0.5,
    };
  const z = Math.max(
    1,
    Math.min(s.zoom, 2, p.metadata.width / p.metadata.height),
  );
  const sw = Math.round((1080 * z) / 2) * 2,
    sh = Math.round((sw * p.metadata.height) / p.metadata.width / 2) * 2;
  const center = s.center ?? 0.5;
  const x =
    Math.floor(Math.max(0, Math.min(sw - 1080, center * sw - 540)) / 2) * 2;
  const ratio = $("preview").clientWidth / 1080;
  v.style.width = sw * ratio + "px";
  v.style.height = sh * ratio + "px";
  v.style.left = -x * ratio + "px";
  v.style.top = Math.floor((1920 - sh) / 4) * 2 * ratio + "px";
  $("seek").min = c.start;
  $("seek").max = c.end;
  $("seek").value = Math.max(c.start, Math.min(c.end, t));
  $("time").textContent =
    `${time(Math.max(0, t - c.start))} / ${time(c.end - c.start)}`;
  $("frame-info").textContent =
    `${Math.round(z * 100)}% · ${c.manual_frame ? "직접 조정" : c.framing.length ? "AI 장면별 구도" : "중앙 기본 구도"}${s.review ? " · 확인 필요" : ""}`;
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
    const prev = cid;
    cid = input.dataset.include;
    await change({ included: input.checked });
    cid = prev;
    render();
    return;
  }
  const card = e.target.closest("[data-clip]");
  if (card) {
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
$("manual-frame").onchange = safe(() =>
  change({
    manual_frame: $("manual-frame").checked
      ? { zoom: Number($("zoom").value), center: Number($("center").value) }
      : null,
  }),
);
for (const id of ["zoom", "center"]) {
  $(id).oninput = () => {
    $(id + "-label").value = Math.round(Number($(id).value) * 100) + "%";
  };
  $(id).onchange = safe(() =>
    change({
      manual_frame: {
        zoom: Number($("zoom").value),
        center: Number($("center").value),
      },
    }),
  );
}
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
          `<div class="reservation"><h3>${esc(r.title)}</h3>${r.deliveries.map((d) => `<div class="row"><span class="badge">${esc(d.service)}</span><span>${esc(d.status)}</span><small>${new Date(d.due_at).toLocaleString("ko-KR")}</small>${d.post_id ? `<small>ID ${esc(d.post_id)}</small>` : ""}${d.error ? `<small class="warning">${esc(d.error)}</small>` : ""}${d.status === "unknown" ? `<button data-reconcile="${r.id}" data-channel-id="${d.channel_id}">Buffer 게시물 ID 연결</button>` : ""}</div>`).join("")}<div class="row">${r.deleted_at ? `<small>${esc(r.deletion_reason)}</small>` : `<button data-delete-reservation="${r.id}">예약 취소·원격 영상 정리</button>`}</div></div>`,
      )
      .join("") || '<p class="muted">아직 예약한 영상이 없습니다.</p>';
}
$("schedule-open").onclick = safe(async () => {
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
