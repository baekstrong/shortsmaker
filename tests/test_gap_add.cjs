// Run with: node tests/test_gap_add.cjs
// Exercise the real pointer handlers and add/prepare flow without a browser dependency.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'static/index.html'), 'utf8');
const script = fs.readFileSync(path.join(root, 'static/app.js'), 'utf8');
const ids = new Set([...html.matchAll(/id="([^"]+)"/g)].map(match => match[1]));
const plain = value => JSON.parse(JSON.stringify(value));
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
};
const tick = () => new Promise(resolve => setImmediate(resolve));

function fixture() {
  const elements = new Map(), calls = [];
  function element(id) {
    assert.ok(ids.has(id) || id === '.preview-pane', `HTML contains ${id}`);
    if (!elements.has(id)) elements.set(id, {
      value:'', checked:true, style:{}, dataset:{}, children:[], clientWidth:700,
      classList:{toggle(){}, add(){}, remove(){}}, listeners:{},
      currentTime:0, paused:true, seeking:false,
      addEventListener(name, fn){this.listeners[name] = fn;}, setAttribute(){},
      pause(){this.paused = true;}, focus(){}, scrollIntoView(){},
      getBoundingClientRect(){return this.rect || {left:100, width:700};},
      setPointerCapture(id){this.captured = id;},
      hasPointerCapture(id){return this.captured === id;},
      releasePointerCapture(){this.captured = null;},
    });
    return elements.get(id);
  }
  const context = vm.createContext({
    console, setTimeout(){}, clearTimeout(){}, setInterval(){},
    requestAnimationFrame(){return 1;}, cancelAnimationFrame(){},
    localStorage:{getItem(){return null;}, setItem(){}},
    document:{getElementById:element, querySelectorAll(){return [];}, querySelector:element, addEventListener(){}},
    window:{addEventListener(){}}, ResizeObserver:class {observe(){}},
    fetch(){return new Promise(() => {});},
  });
  vm.runInContext(script, context);
  const run = code => vm.runInContext(code, context);
  run(`
    pid='p'; cid='a'; selectFirstShot=false;
    state.projects=[{id:'p',revision:1,metadata:{fps:'30',width:1920,height:1080,duration:700},clips:[
      {id:'a',start:0,end:50,included:true},
      {id:'excluded',start:40,end:75,included:false},
      {id:'b',start:100,end:150,included:true},
      {id:'overlap',start:140,end:200,included:true},
      {id:'last',start:500,end:550,included:true}
    ]}];
    updatePreview=()=>{};
    // Keep the real renderer's auto-shot trigger: this is the competing job under test.
    render=()=>{renderClipTimeline(); if(selectFirstShot && !sourceShots().length) ensureClipShots();};
    refresh=async()=>{};
  `);
  let apiImpl = async (url, body) => {
    if (url.endsWith('/edit')) {
      assert.equal(body.action, 'add_gap', 'creation uses the non-overlapping add API');
      const p = plain(run(`state.projects.find(p=>p.id==='${url.split('/')[3]}')`));
      p.revision++;
      p.clips.push({id:'new-clip', start:body.start, end:body.end, included:true,
        gap_preparation:{status:'pending'}});
      p.clips.sort((a,b) => a.start-b.start);
      return p;
    }
    return {id:'job', status:'queued'};
  };
  context.testApi = (url, body) => {
    calls.push({url, body:plain(body)});
    return apiImpl(url, body);
  };
  run('api=(url,body)=>testApi(url,body)');
  function pointer(at, pointerId=7, target={closest(){return null;}}) {
    const rect = element('clip-timeline').getBoundingClientRect();
    return {clientX:rect.left+at/700*rect.width, pointerId, button:0, isPrimary:true,
      target, currentTarget:element('clip-strip'), preventDefault(){}};
  }
  async function draft(start, end) {
    element('clip-strip').onpointerdown(pointer(start));
    element('clip-timeline').onpointermove(pointer(end));
    await element('clip-timeline').onpointerup(pointer(end));
  }
  return {element, run, pointer, draft, calls,
    setApi(fn){const original = apiImpl; apiImpl = (url,body)=>fn(url,body,original);},
    selection(){return plain(run('clipAddDraft'));},
  };
}

async function main() {
  {
    const f = fixture();
    assert.deepEqual(plain(f.run('sourceGaps()')), [
      {start:75,end:100}, {start:200,end:500}, {start:550,end:700},
    ], 'gaps are the complement of the union, including excluded and overlapping clips');
    assert.equal(f.run('gapAt(60)'), undefined, 'an excluded clip still occupies source time');
    assert.equal(f.run('gapAt(150)'), undefined, 'an overlapping range does not create a false gap');
    f.element('clip-strip').onpointerdown(f.pointer(60));
    assert.equal(f.selection(), null, 'pointer down inside an occupied time cannot begin an addition');
    await f.draft(80, 95);
    assert.equal(f.selection().start, 80);
    assert.equal(f.selection().end, 95);
    assert.equal(f.element('clip-timeline').captured, null, 'release ends pointer capture');
    assert.equal(f.element('clip-add-panel').hidden, false, 'release leaves a reviewable draft');
    assert.equal(f.calls.length, 0, 'dragging and releasing never creates a short or starts AI work');
    assert.equal(f.run('project().clips.length'), 5);
    await f.draft(95, 80);
    assert.deepEqual([f.selection().start,f.selection().end], [80,95], 'reverse drag has the same ordered bounds');
    await f.draft(80, 130);
    assert.equal(f.selection().end, 100, 'forward drag stops at the next occupied range');
    await f.draft(95, 0);
    assert.equal(f.selection().start, 75, 'reverse drag stops at the preceding occupied union');
    await f.draft(220, 490);
    assert.deepEqual([f.selection().start,f.selection().end], [220,400], 'forward selection is limited to 180 seconds');
    await f.draft(480, 210);
    assert.deepEqual([f.selection().start,f.selection().end], [300,480], 'reverse selection is limited to 180 seconds');
    f.element('clip-timeline').rect = {left:-300,width:1400};
    await f.draft(80.01,95.02);
    assert.deepEqual([f.selection().start,f.selection().end], [80,95.033333], 'scroll/zoom coordinates still snap to source frames');
    f.element('cancel-add-clip').onclick();
    assert.equal(f.selection(), null, 'explicit cancel discards a finished draft');
    assert.equal(f.calls.length, 0);
    await f.draft(80,80.01);
    assert.equal(f.selection(), null, 'a click or sub-minimum drag does not leave an invalid draft');
  }
  {
    const f = fixture();
    await f.draft(250,300);
    const original = f.selection();
    f.run('startClipAdd')(f.pointer(252), 'start');
    f.element('clip-timeline').onpointermove(f.pointer(260));
    assert.equal(f.selection().start, 258, 'grabbing inside a handle preserves its initial pointer offset');
    f.element('clip-timeline').onpointercancel(f.pointer(260,8));
    assert.ok(f.run('clipInteraction'), 'unrelated pointer cancellation is ignored');
    f.element('clip-timeline').onpointercancel(f.pointer(260));
    assert.deepEqual(f.selection(), original, 'cancelled handle movement restores the prior draft');
    f.run('startClipAdd')(f.pointer(250), 'start');
    await f.element('clip-timeline').onpointerup(f.pointer(0));
    assert.equal(f.selection().start, 200, 'start handle cannot cross the preceding short');
    f.run('startClipAdd')(f.pointer(300), 'end');
    await f.element('clip-timeline').onpointerup(f.pointer(700));
    assert.equal(f.selection().end, 380, 'end handle respects the 180-second maximum');
    f.run('startClipAdd')(f.pointer(200), 'start');
    await f.element('clip-timeline').onpointerup(f.pointer(400));
    assert.ok(Math.abs(f.selection().end-f.selection().start-.1)<1e-6, 'handles cannot invert or shrink below the minimum');
    const startHandle = {dataset:{selectionEdge:'start'},closest(){return this;}};
    await f.element('clip-timeline-scroll').onkeydown({key:'ArrowLeft',target:startHandle,preventDefault(){}});
    assert.ok(f.selection().start<379.9, 'keyboard adjustment remains available on draft handles');
    assert.equal(f.calls.length,0,'handle and keyboard adjustment remain local until confirmation');
    f.element('clip-strip').onpointerdown(f.pointer(80));
    f.element('clip-timeline').onpointermove(f.pointer(90));
    f.run("state.projects.push({id:'other',revision:1,metadata:{duration:700},clips:[]}); pid='other'; cid=null;");
    f.element('clip-timeline').onpointermove(f.pointer(95));
    assert.equal(f.run('clipInteraction'),null,'a project switch cancels an old pointer interaction');
    assert.equal(f.selection(),null,'a draft from the previous project is not displayed in the new project');
  }
  {
    const f = fixture(), gate = deferred();
    await f.draft(80,95);
    f.setApi(async (url,body,original) => {
      if (url.endsWith('/edit')) await gate.promise;
      return original(url,body);
    });
    const adding = f.run('confirmClipAdd()');
    await tick();
    await f.run('confirmClipAdd()');
    assert.equal(f.calls.length,1,'a duplicate confirmation cannot enqueue a second creation');
    assert.equal(f.calls[0].body.start,80);
    assert.equal(f.calls[0].body.end,95);
    gate.resolve();
    await adding;
    await tick();
    assert.equal(f.run('cid'),'new-clip','the actual added ID is selected after sorting by source time');
    assert.equal(f.run('project().clips.at(-1).id'),'last','selection does not assume the new clip is last');
    assert.deepEqual(f.calls.map(call=>call.url), ['/api/projects/p/edit','/api/projects/p/jobs/prepare_clip'],
      'creation submits one targeted preparation job without an automatic shots race');
    assert.deepEqual(f.calls[1].body,{clip_ids:['new-clip']},'existing shorts are excluded from automatic preparation');
    assert.equal(f.selection(),null);
    assert.equal(f.run('clipCreationPending'),false);
    await f.run('ensureClipShots()');
    assert.equal(f.calls.length,2,'pending gap preparation suppresses a competing automatic cuts job');
  }
  {
    const f = fixture();
    await f.draft(80,95);
    const before = f.selection();
    let fail = true;
    f.setApi((url,body,original) => {
      if (url.endsWith('/edit') && fail) {fail=false; throw Error('temporary add failure');}
      return original(url,body);
    });
    await assert.rejects(f.run('confirmClipAdd()'), /temporary add failure/);
    assert.deepEqual(f.selection(),before,'an add failure preserves the exact draft for retry');
    assert.equal(f.run('project().clips.length'),5,'a failed add leaves saved clips unchanged');
    assert.equal(f.run('clipCreationPending'),false,'failure releases the duplicate-click guard');
    await f.run('confirmClipAdd()');
    assert.equal(f.calls.filter(call=>call.url.endsWith('/prepare_clip')).length,1,'retry starts preparation once after a successful add');
  }
  {
    const f = fixture();
    await f.draft(80,95);
    let fail = true;
    f.setApi((url,body,original) => {
      if (url.endsWith('/prepare_clip') && fail) {fail=false; throw Error('temporary prepare failure');}
      return original(url,body);
    });
    await assert.rejects(f.run('confirmClipAdd()'), /자동 준비 재시도/);
    assert.equal(f.run('cid'),'new-clip','preparation failure retains the successfully added short');
    assert.equal(f.run('project().clips.length'),6);
    assert.equal(f.element('retry-clip-prepare').hidden,false,'a failed preparation submission exposes a retry action');
    assert.equal(f.selection(),null,'retrying preparation cannot accidentally recreate the short');
    await f.element('retry-clip-prepare').onclick();
    assert.equal(f.calls.filter(call=>call.url.endsWith('/edit')).length,1,'preparation retry does not add another short');
    assert.equal(f.calls.filter(call=>call.url.endsWith('/prepare_clip')).length,2);
    assert.ok(f.calls.filter(call=>call.url.endsWith('/prepare_clip')).every(call=>call.body.clip_ids[0]==='new-clip'));
  }
  {
    const f = fixture(), gate = deferred();
    await f.draft(80,95);
    f.setApi(async (url,body,original) => {
      if (url.endsWith('/edit')) await gate.promise;
      return original(url,body);
    });
    const adding = f.run('confirmClipAdd()');
    await tick();
    f.run("state.projects.push({id:'other',revision:1,metadata:{duration:700},clips:[{id:'other-clip',start:0,end:1}]}); pid='other'; cid='other-clip';");
    gate.resolve();
    await adding;
    assert.equal(f.run('pid'),'other');
    assert.equal(f.run('cid'),'other-clip','an old creation response cannot switch the new project selection');
    assert.equal(f.calls[1].url,'/api/projects/p/jobs/prepare_clip','preparation remains scoped to the project that requested creation');
    assert.deepEqual(f.calls[1].body,{clip_ids:['new-clip']});
    assert.equal(f.run('project().clips.length'),1,'the other project remains unchanged');
  }
  console.log('Gap addition: pointer drafts, occupied bounds, confirmation, targeted preparation, retry, and project isolation passed.');
}

main().catch(error => {console.error(error); process.exitCode=1;});
