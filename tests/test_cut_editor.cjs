// Run with: node tests/test_cut_editor.cjs
// No browser/package dependencies: exercise the real editor script with a small DOM fixture.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const ids = new Set([...fs.readFileSync(path.join(root, 'static/index.html'), 'utf8').matchAll(/id="([^"]+)"/g)].map(m => m[1]));
const elements = new Map();
function element(id) {
  if (!ids.has(id) && id !== '.preview-pane') throw Error(`Missing HTML element: ${id}`);
  if (!elements.has(id)) elements.set(id, {
    value:'', checked:true, style:{}, dataset:{}, children:[], clientWidth:600,
    classList:{ toggle(){}, add(){}, remove(){} }, listeners:{}, currentTime:0, paused:true, seeking:false,
    addEventListener(name, fn) { this.listeners[name] = fn; }, setAttribute(){},
    pause() { this.paused = true; }, focus(){}, scrollIntoView(){}, hasPointerCapture(){return false;},
  });
  return elements.get(id);
}
const frameCallbacks = new Map(), cancelledFrames = [];
let callbackId = 0;
const video = element('video');
video.requestVideoFrameCallback = callback => { frameCallbacks.set(++callbackId, callback); return callbackId; };
video.cancelVideoFrameCallback = id => { cancelledFrames.push(id); frameCallbacks.delete(id); };
const context = vm.createContext({
  console, setTimeout(){}, clearTimeout(){}, setInterval(){}, requestAnimationFrame(){return 1;}, cancelAnimationFrame(){},
  localStorage:{getItem(){return null;}, setItem(){}},
  document:{getElementById:element, querySelectorAll(){return [];}, querySelector:element, addEventListener(){}},
  window:{addEventListener(){}}, ResizeObserver:class {observe(){}},
  fetch(){return new Promise(() => {});},
});
vm.runInContext(fs.readFileSync(path.join(root, 'static/app.js'), 'utf8'), context);
const run = code => vm.runInContext(code, context);
run(`
  pid='p'; cid='c';
  state.projects=[{id:'p',revision:1,metadata:{fps:'30000/1001',width:1920,height:1080},clips:[{
    id:'c',start:1,end:1.3,manual_frame:{zoom:1.5,center:.5},
    source_shots:[{id:'a',start:1,end:1.066667,frame_times:[1,1.033333]},
      {id:'b',start:1.066667,end:1.3,frame_times:[1.066667,1.133333,1.166667,1.233333]}],
    frame_overrides:[{id:'a',start:1,end:1.066667,zoom:1.5,center:0},
      {id:'b',start:1.066667,end:1.3,zoom:1.5,center:1}]
  }]}];
`);
assert.equal(run('timelineTick(60, 600)'), 10, 'timeline labels stay at least 100 pixels apart');
assert.equal(run('timelineTick(60, 340)'), 20, 'narrow timelines use readable ruler spacing');
assert.equal(run('sourceFps()'), 30000/1001, 'rational FPS is parsed');
assert.equal(run('adjacentFrame(1.066667, 1)'), 1.133333, 'VFR navigation uses the next decoded PTS');
assert.equal(run('adjacentFrame(1.133333, -1)'), 1.066667, 'VFR navigation uses the previous decoded PTS');
assert.equal(run('nearestFrame(1.1)'), 1.133333, 'VFR snapping selects an actual decoded frame');
assert.equal(run('activeFrame(1.066666666667).center'), 1, 'new crop applies on the first frame despite rounded PTS');
assert.equal(run('activeFrame(1.033333).center'), 0, 'preceding frame keeps previous crop');
assert.equal(run('activeFrame(.999).center'), 0, 'first decoded frame before a trimmed clip gets the clip crop');
assert.equal(run('activeFrame(1.31).center'), 1, 'paused end of clip retains final crop');
run('updateShotPlayhead(0)');
assert.match(element('shot-position').textContent, /^00:00\.000 · 1 프레임$/, 'loading never displays negative frames');
assert.equal(run('preciseTime(59.9996)'), '01:00.000', 'millisecond labels carry into the next minute');
assert.equal(run('snapRegionTime(1.08)'), 1.066667, 'enabled snapping finds the nearby cut boundary');
element('snap-shots').checked = false;
assert.equal(run('snapRegionTime(1.12)'), 1.133333, 'disabled cut snapping still snaps to a decoded frame');
run(`selectedShotId='a'; frameDraft={pid,cid,scopeKey:'shot:a',frame:{zoom:2,center:.2}};`);
assert.equal(run('editingFrame().center'), .2);
run(`selectedShotId='b';`);
assert.equal(run('editingFrame().center'), 1, 'pending draft from another shot does not leak across selection');
run(`cid='other';`);
assert.equal(run('currentFrameDraft()'), null, 'pending draft does not leak across clips');
run(`cid='c'; frameDraft=null; selectedShotId=null; clip().start=1.025; project().revision++;`);
assert.equal(run('sourceFrameTimes()[0]'), 1, 'trim retains the frame already displayed at the clip start');
run('seekFrame(1.025)');
assert.ok(Math.abs(video.currentTime-1.025)<2e-6, 'selecting the first shot preserves the original trim timestamp');
run('updateShotPlayhead(1.025)');
assert.match(element('shot-position').textContent, /· 1 프레임$/, 'trim inside a frame still displays frame one');
run(`clip().start=1; clip().source_shots=[]; project().revision++;`);
assert.ok(Math.abs(run('adjacentFrame(1.001, 1)') - 31*1001/30000)<1e-12, 'FPS fallback advances one exact rational frame');
run(`const previewUpdates=[]; updatePreview=at=>previewUpdates.push(at);`);
video.currentTime = 1.2;
video.paused = false;
run('watchVideoFrames()');
const firstId = callbackId;
frameCallbacks.get(firstId)(0, {mediaTime:1.066666666667});
assert.equal(run('previewUpdates.at(-1)'), 1.066666666667, 'crop update uses presented mediaTime, not currentTime/timeupdate');
const pendingId = callbackId;
const cancelledCallback = frameCallbacks.get(pendingId);
video.listeners.emptied();
assert.ok(cancelledFrames.includes(pendingId), 'source replacement cancels the pending video-frame callback');
assert.equal(run('videoFrameRequest'), null);
run('watchVideoFrames()');
const replacementId = callbackId;
cancelledCallback(0, {mediaTime:1.166667});
assert.equal(run('videoFrameRequest'), replacementId, 'a late cancelled callback cannot clear the replacement callback');
const oldProjectCallback = frameCallbacks.get(callbackId);
run(`pid='new-project';`);
const updateCount = run('previewUpdates.length');
oldProjectCallback(0, {mediaTime:1.233333});
assert.equal(run('previewUpdates.length'), updateCount, 'callbacks from a previous project cannot repaint the new project');
(async () => {
  run(`
    pid='p'; cid='c'; selectedShotId='a';
    clip().source_shots=[{id:'a',start:1,end:1.2},{id:'b',start:1.2,end:1.3}];
    const saves=[]; let finishSave;
    edit=(action,args,target={pid,cid})=>{ saves.push({action,args,target}); return new Promise(resolve=>finishSave=resolve); };
    render=()=>{};
  `);
  const saving = run('saveFrame({zoom:1.5,center:0})');
  assert.equal(run('saves[0].action'), 'frame_shot');
  assert.equal(run('saves[0].args.shot_id'), 'a', 'saved crop targets the selected shot');
  run(`pid='other-project'; cid='other-clip'; selectedShotId='b';`);
  assert.equal(run('saves[0].target.pid'), 'p', 'in-flight crop save retains its original project');
  assert.equal(run('saves[0].target.cid'), 'c', 'in-flight crop save retains its original clip');
  assert.equal(run('currentFrameDraft()'), null, 'in-flight crop is hidden after a clip switch');
  run('finishSave()');
  await saving;
  console.log('Cut editor: decoded-frame timing, selection/save isolation, snapping, and callback lifecycle passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
