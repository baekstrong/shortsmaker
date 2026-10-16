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
    pause() { this.paused = true; }, focus(){}, scrollIntoView(){},
    getBoundingClientRect(){return this.rect || {left:100,width:600};},
    setPointerCapture(id){this.captured=id;}, hasPointerCapture(id){return this.captured===id;},
    releasePointerCapture(){this.captured=null;},
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
run(`selectedShotId='obsolete-after-undo'; selectFirstShot=false; videoProject=pid;`);
video.currentTime=1.133333;
run('renderShotTimeline()');
assert.equal(run('selectedShotId'),'b','undo restoring different shot IDs keeps editing scoped to the current cut');
run('selectedShotId=null; renderShotTimeline()');
assert.equal(run('selectedShotId'),null,'explicit whole-short editing remains selected');
const timeline = element('shot-timeline');
const pointer = (clientX, target=element('shot-ruler'), pointerId=7) => ({clientX,currentTarget:target,pointerId,button:0,isPrimary:true,preventDefault(){}});
run(`selectedShotId='a';`);
const storedClip = run('JSON.stringify(clip())');
element('shot-ruler').onpointerdown(pointer(166.666));
assert.equal(timeline.captured, 7, 'ruler drag captures its pointer outside the ruler');
assert.ok(Math.abs(video.currentTime-1.033333)<2e-6);
timeline.rect={left:-200,width:1200};
timeline.onpointermove(pointer(333.332));
assert.ok(Math.abs(video.currentTime-1.133333)<2e-6, 'drag uses zoomed, scrolled coordinates and actual VFR frames');
assert.equal(run('selectedShotId'), 'a', 'scrubbing preserves the selected crop edit scope');
timeline.onpointerup(pointer(2000));
assert.ok(Math.abs(video.currentTime-1.233333)<2e-6, 'drag clamps to the final visible frame');
assert.equal(timeline.captured, null, 'release ends capture');
assert.equal(run('shotScrub'), null);
assert.equal(run('JSON.stringify(clip())'), storedClip, 'scrubbing never edits saved cuts or framing');
video.currentTime=1.066667;
const grabX=-200+(video.currentTime-1)/.3*1200+6;
element('shot-playhead').onpointerdown(pointer(grabX, element('shot-playhead')));
assert.ok(Math.abs(video.currentTime-1.066667)<2e-6, 'grabbing the wide handle edge does not jump the frame');
timeline.onpointermove(pointer(-1000));
assert.ok(Math.abs(video.currentTime-1)<2e-6, 'drag clamps to the beginning');
timeline.onpointercancel(pointer(-1000));
const stoppedTime=video.currentTime;
timeline.onpointermove(pointer(600));
assert.equal(video.currentTime, stoppedTime, 'cancelled drags no longer seek');
element('shot-ruler').onpointerdown(pointer(100));
run(`cid='other';`);
timeline.onpointermove(pointer(500));
assert.equal(run('shotScrub'), null, 'switching clips cancels an old drag');
assert.equal(timeline.captured, null);
run(`cid='c';`);
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
  // Source overview: real pointer handlers commit once, preserve siblings, and cancel safely.
  run(`
    pid='p'; cid='c'; frameDraft=null;
    project().metadata.duration=100; project().revision++;
    clip().start=10; clip().end=30; clip().source_shots=[];
    project().clips.push({id:'sibling',start:40,end:60});
    render=()=>{}; renderClipTimeline=()=>{}; ensureClipShots=()=>{};
    editQueue=Promise.resolve();
    const savedBounds=[];
    edit=async(action,extra,target)=>{savedBounds.push({action,extra,target}); Object.assign(state.projects.find(p=>p.id===target.pid).clips.find(c=>c.id===target.cid),extra.changes);};
  `);
  const overview=element('clip-timeline'); overview.rect={left:100,width:1000};
  const range={dataset:{clipId:'c'},style:{},querySelector:()=>({textContent:''})};
  element('clip-strip').children=[range];
  const handle={dataset:{edge:'start'},closest:()=>range};
  const edgePointer=x=>({...pointer(x),target:{closest:()=>handle}});
  element('clip-strip').onpointerdown(edgePointer(200));
  await overview.onpointerup(edgePointer(200));
  assert.equal(run('savedBounds.length'),0,'clicking a fractional trim edge does not alter its time');
  element('clip-strip').onpointerdown(edgePointer(200));
  overview.onpointermove(edgePointer(250));
  assert.equal(run('clip().start'),10,'drag preview does not mutate the stored clip');
  assert.equal(run('savedBounds.length'),0,'dragging does not issue save requests');
  assert.ok(Math.abs(video.currentTime-15)<.04,'trim previews its source position');
  await overview.onpointerup(edgePointer(250));
  assert.equal(run('savedBounds.length'),1,'release saves one undoable edit');
  assert.ok(Math.abs(run('clip().start')-15)<.04);
  assert.equal(run("project().clips[1].start"),40,'adjacent clips are not silently moved');
  assert.equal(overview.captured,null);
  const storedStart=run('clip().start');
  element('clip-strip').onpointerdown(edgePointer(250));
  overview.onpointermove(edgePointer(300));
  overview.onpointercancel(edgePointer(300));
  assert.equal(run('clip().start'),storedStart,'cancel discards the trim draft');
  assert.equal(run('savedBounds.length'),1);
  element('clip-strip').onpointerdown(edgePointer(250));
  run(`cid='sibling';`);
  overview.onpointermove(edgePointer(350));
  assert.equal(run('clipInteraction'),null,'switching clips releases an old trim');
  run(`cid='c';`);
  assert.equal(run("trimBounds(clip(),'start',100).start"),29.9,'trim cannot cross the other edge');
  assert.equal(run("trimBounds(clip(),'end',1000).end"),100,'trim cannot exceed the source');
  assert.equal(run("trimBounds(clip(),'start',-1).start"),0,'trim cannot precede the source');
  element('clip-ruler').onpointerdown(pointer(900,element('clip-ruler')));
  await overview.onpointerup(pointer(900));
  assert.ok(Math.abs(video.currentTime-80)<.04,'source ruler reaches outside the selected short');
  assert.equal(run('savedBounds.length'),1,'source scrubbing never saves a trim');
  element('source-filmstrip').onpointerdown(pointer(950,element('source-filmstrip')));
  await overview.onpointerup(pointer(950));
  assert.ok(Math.abs(video.currentTime-85)<.04,'original preview strip scrubs the full source');
  assert.equal(run('savedBounds.length'),1,'preview strip does not edit a short');
  const beforeConflict=run('savedBounds.length');
  await assert.rejects(run("saveClipBounds({pid,cid,original:{start:10,end:30},start:12,end:30})"),/다른 편집/);
  assert.equal(run('savedBounds.length'),beforeConflict,'concurrent range changes are rejected');
  const rows=JSON.parse(run("JSON.stringify(overviewRows([{start:0,end:20},{start:10,end:30},{start:40,end:60}],100,1000))"));
  assert.deepEqual(rows.map(r=>r.row),[0,1,0],'overlapping shorts remain independently selectable');
  assert.equal(run('compactClipOffset({start:99,end:100},100,1000)'),-75,'short clips near source end keep their preview inside the timeline');
  console.log('Cut editor: frame timing, cut/source timeline drags, trim save/cancel/conflict, selection/save isolation, and callback lifecycle passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
