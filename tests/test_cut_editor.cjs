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
const documentListeners = {};
let dialogOpen = false;
let callbackId = 0;
const video = element('video');
video.requestVideoFrameCallback = callback => { frameCallbacks.set(++callbackId, callback); return callbackId; };
video.cancelVideoFrameCallback = id => { cancelledFrames.push(id); frameCallbacks.delete(id); };
const context = vm.createContext({
  console, setTimeout(){}, clearTimeout(){}, setInterval(){}, requestAnimationFrame(){return 1;}, cancelAnimationFrame(){},
  localStorage:{getItem(){return null;}, setItem(){}},
  document:{getElementById:element, querySelectorAll(){return [];},
    querySelector(selector){return selector === 'dialog[open]' ? (dialogOpen ? {} : null) : element(selector);},
    addEventListener(name, listener){(documentListeners[name] ||= []).push(listener);}},
  window:{addEventListener(){}}, ResizeObserver:class {observe(){}},
  fetch(){return new Promise(() => {});},
});
vm.runInContext(fs.readFileSync(path.join(root, 'static/app.js'), 'utf8'), context);
const run = code => vm.runInContext(code, context);
run(`
  pid='p'; cid='c';
  state.projects=[{id:'p',revision:1,metadata:{fps:'30000/1001',width:1920,height:1080,duration:10},clips:[{
    id:'c',start:1,end:1.3,manual_frame:{zoom:1.5,center:.5},
    source_shots:[{id:'a',start:1,end:1.066667,frame_times:[1,1.033333]},
      {id:'b',start:1.066667,end:1.3,frame_times:[1.066667,1.133333,1.166667,1.233333]}],
    frame_overrides:[{id:'a',start:1,end:1.066667,zoom:1.5,center:0},
      {id:'b',start:1.066667,end:1.3,zoom:1.5,center:1}]
  }]}];
`);
const shortcut = documentListeners.keydown.find(fn => fn.name === 'handlePlaybackShortcut');
assert.ok(shortcut, 'Space playback is registered on the document');
let playbackClicks = 0;
element('play').click = () => playbackClicks++;
function spaceEvent(options={}) {
  return {code:'Space', key:' ', target:{closest(){return null;}}, defaultPrevented:false,
    preventDefault(){this.defaultPrevented=true;}, ...options};
}
for (const fromShotTimeline of [false, true]) {
  const event = spaceEvent(), before = playbackClicks;
  if (fromShotTimeline) element('shot-timeline-scroll').onkeydown(event);
  shortcut(event);
  assert.equal(playbackClicks, before+1, 'Space toggles once, including after bubbling through the shot timeline');
  assert.equal(event.defaultPrevented, true, 'native scrolling/button activation is prevented');
}
for (const options of [{repeat:true}, {shiftKey:true,repeat:true}]) {
  const event = spaceEvent(options), before = playbackClicks;
  shortcut(event);
  assert.equal(playbackClicks, before, 'holding Space does not repeatedly toggle playback');
  assert.equal(event.defaultPrevented, true, 'repeated Space still cannot scroll the page');
}
for (const options of [
  {isComposing:true}, {keyCode:229}, {ctrlKey:true}, {metaKey:true}, {altKey:true},
  {target:{isContentEditable:true}}, {target:{closest(){return {};}}},
  {code:'Enter',key:'Enter'}, {defaultPrevented:true},
]) {
  const event = spaceEvent(options), before = playbackClicks, prevented = event.defaultPrevented;
  shortcut(event);
  assert.equal(playbackClicks, before, 'typing, form controls, composition and other shortcuts keep their behavior');
  assert.equal(event.defaultPrevented, prevented, 'ignored shortcuts are not intercepted');
}
for (const noClip of [false,true]) {
  dialogOpen = !noClip;
  if (noClip) run("cid='missing'");
  const event = spaceEvent(), before = playbackClicks;
  shortcut(event);
  assert.equal(playbackClicks, before, 'dialogs and empty projects never trigger background playback');
  assert.equal(event.defaultPrevented, false);
}
run("cid='c'");
dialogOpen = false;
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
run(`titleURL=titleLoadedURL='current-title'; titleObjectURL='blob:current-title'; seekFrame(1);`);
assert.equal(element('title-image').hidden,false,'the selected short starts with its hook visible');
run('seekSource(1.133333)');
assert.equal(element('title-image').hidden,false,'scrubbing the source inside the selected short retains its hook');
run('updatePreview(1.233333)');
assert.equal(element('title-image').hidden,false,'subsequent playback frames inside the short retain its hook');
run('seekSource(.9)');
assert.equal(element('title-image').hidden,true,'the hook does not leak before the selected short');
run('seekSource(1.3)');
assert.equal(element('title-image').hidden,true,'the hook does not leak beyond the selected short');
run('seekSource(1.166667)');
assert.equal(element('title-image').hidden,false,'returning from the source to the short restores its hook');
run(`titleLoadedURL='previous-title'; updatePreview(1.166667);`);
assert.equal(element('title-image').hidden,true,'an old title stays hidden while the selected title loads');
run(`titleLoadedURL=titleURL; seekFrame(1);`);
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
  // A title can finish decoding after the user scrubs into/out of the short.
  let finishTitleDecode, titleDecodeStarted;
  let decoding = new Promise(resolve => titleDecodeStarted=resolve);
  context.fetch=async()=>({ok:true,blob:async()=>({})});
  context.URL={createObjectURL:()=> 'blob:loaded-title',revokeObjectURL(){}};
  context.Image=class { decode() { titleDecodeStarted(); return new Promise(resolve=>finishTitleDecode=resolve); } };
  run(`pid='p'; cid='c'; sourcePreview=true; presentedTime=null;`);
  video.currentTime=.9;
  const loadingTitle=run('loadTitlePreview(true)');
  await decoding;
  video.currentTime=1.166667;
  finishTitleDecode(); await loadingTitle;
  assert.equal(element('title-image').hidden,false,'a delayed title load uses the current in-short position');
  decoding = new Promise(resolve => titleDecodeStarted=resolve);
  const loadingOutside=run('loadTitlePreview(true)');
  await decoding;
  video.currentTime=1.4;
  finishTitleDecode(); await loadingOutside;
  assert.equal(element('title-image').hidden,true,'a delayed title load never overlays source footage outside the short');
  context.fetch=()=>new Promise(()=>{});
  run('sourcePreview=false;');
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
  const rows=JSON.parse(run("JSON.stringify(overviewRows([{start:0,end:20},{start:10,end:30},{start:40,end:60}]))"));
  assert.deepEqual(rows.map(r=>r.row),[0,1,0],'overlapping shorts remain independently selectable');
  const adjacent=JSON.parse(run("JSON.stringify(overviewRows([{start:92.52,end:162.12},{start:33.94,end:34.19,included:false},{start:34.19,end:89.24}]))"));
  assert.deepEqual(adjacent.map(r=>r.row),[0,0,0],'the excluded 0.25-second short never pushes its adjacent short into another row');
  assert.deepEqual(adjacent.map(r=>r.index),[1,2,0],'time ordering preserves original short numbers');
  const small=JSON.parse(run("JSON.stringify(overviewRows([{start:0,end:.25},{start:.25,end:.5},{start:.49,end:.8}]))"));
  assert.deepEqual(small.map(r=>r.row),[0,0,1],'touching short ranges share a row, while even a tiny real overlap gets another');
  for (const width of [320,600,1000]) for (const start of [0,33.94,99.9]) {
    const offset=run(`compactClipOffset({start:${start},end:100},100,${width})`);
    const left=start/100*width-6+offset;
    assert.ok(left>=0 && left+132<=width,'compact hover preview stays inside both source edges without reserving time');
  }
  console.log('Cut editor: global Space playback, frame timing, cut/source timeline drags, trim save/cancel/conflict, selection/save isolation, and callback lifecycle passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
