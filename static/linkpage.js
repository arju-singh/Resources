/* Shared renderer + animated background for Link pages.
   Data lives centrally in /static/links-data.js (window.LINKS + window.LINK_CATS).
   A page sets window.PAGE = { cat: "Websites" }  OR  { hub: true }  then loads this module. */
let animate, inView;
try { ({ animate, inView } = await import('https://cdn.jsdelivr.net/npm/motion@11.11.13/+esm')); } catch (e) {}

const REDUCE = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const PAGE  = window.PAGE || {};
const LINKS = window.LINKS || [];
const CATS  = window.LINK_CATS || [];
const grid  = document.getElementById('grid');

const esc = s => String(s).replace(/[&<>"]/g, c => ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;' }[c]));
const countFor = cat => LINKS.filter(l => l.cat === cat).length;

let cards;
if (PAGE.hub) {
  cards = CATS.map(c => ({
    name: c.cat, kind: c.kind || 'Category', icon: c.icon, url: c.page,
    domain: `${countFor(c.cat)} link${countFor(c.cat) === 1 ? '' : 's'}`,
    desc: c.desc, c1: c.c1, c2: c.c2
  }));
} else if (PAGE.cat) {
  cards = LINKS.filter(l => l.cat === PAGE.cat);
} else {
  cards = PAGE.cards || [];
}

function cardHTML(t){
  const internal = t.url.startsWith('/');
  const target = internal ? '' : ' target="_blank" rel="noopener"';
  return `
    <a class="lcard" href="${esc(t.url)}"${target} data-cat="${esc(t.kind||'')}">
      <div class="ltilt" style="--c1:${t.c1||'#22d3ee'};--c2:${t.c2||'#a855f7'}">
        <span class="glare"></span>
        <div class="lbadge" style="--c1:${t.c1||'#22d3ee'};--c2:${t.c2||'#a855f7'}">${t.icon||'🔗'}</div>
        <div class="lkind">${esc(t.kind||'Link')}</div>
        <div class="lname">${esc(t.name)}</div>
        <div class="ldesc">${esc(t.desc||'')}</div>
        <div class="lfoot"><span class="ldomain">${esc(t.domain||'')}</span><span class="lgo">${internal?'→':'↗'}</span></div>
      </div>
    </a>`;
}

function attachTilt(el){
  const tilt = el.querySelector('.ltilt');
  el.addEventListener('pointermove', e => {
    const r = el.getBoundingClientRect();
    const px = (e.clientX-r.left)/r.width, py = (e.clientY-r.top)/r.height;
    tilt.style.transform = `rotateY(${(px-.5)*9}deg) rotateX(${(.5-py)*9}deg)`;
    tilt.style.setProperty('--mx', px*100+'%'); tilt.style.setProperty('--my', py*100+'%');
  });
  el.addEventListener('pointerleave', () => tilt.style.transform = '');
}

if (!cards.length) {
  grid.outerHTML = `<div class="lp-empty">Nothing here yet — links will appear once added.</div>`;
} else {
  grid.innerHTML = cards.map(cardHTML).join('');
  // Reveal via IntersectionObserver + CSS class — robust: a class toggle can't fail
  // to commit and leave cards stuck at the stylesheet's opacity:0 (unlike a WAAPI
  // animation whose end state may not persist).
  const io = ('IntersectionObserver' in window)
    ? new IntersectionObserver((entries, obs) => {
        entries.forEach(e => { if (e.isIntersecting) { e.target.classList.add('in'); obs.unobserve(e.target); } });
      }, { rootMargin: '0px 0px -6% 0px', threshold: 0.1 })
    : null;
  [...grid.children].forEach((el, idx) => {
    attachTilt(el);
    if (REDUCE || !io) { el.classList.add('in'); return; }
    el.style.transitionDelay = ((idx % 4) * 60) + 'ms';
    io.observe(el);
  });
}

if (!REDUCE && animate){
  animate('.lp-hero .eyebrow-pill', { opacity:[0,1], transform:['translateY(12px)','translateY(0)'] }, { duration:.6 });
  animate('.lp-hero h1', { opacity:[0,1], transform:['translateY(20px)','translateY(0)'] }, { duration:.7, delay:.08 });
  animate('.lp-hero p', { opacity:[0,1], transform:['translateY(16px)','translateY(0)'] }, { duration:.7, delay:.16 });
}

/* THREE.JS animated background */
(async function bg(){
  if (REDUCE) return;
  let THREE; try { THREE = await import('https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js'); } catch(e){ return; }
  const canvas = document.getElementById('bg-canvas');
  const renderer = new THREE.WebGLRenderer({ canvas, alpha:true, antialias:true });
  renderer.setPixelRatio(Math.min(devicePixelRatio,2)); renderer.setSize(innerWidth,innerHeight);
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(60, innerWidth/innerHeight, 0.1, 100); camera.position.z = 16;
  const group = new THREE.Group(); scene.add(group);
  const COUNT = 1700, pos = new Float32Array(COUNT*3), col = new Float32Array(COUNT*3);
  const pal = PAGE.palette || [[0.40,0.91,0.95],[0.75,0.52,0.99],[0.94,0.45,0.71]];
  for (let i=0;i<COUNT;i++){ const r=6+Math.random()*14,t=Math.random()*Math.PI*2,p=Math.acos(2*Math.random()-1);
    pos[i*3]=r*Math.sin(p)*Math.cos(t); pos[i*3+1]=r*Math.sin(p)*Math.sin(t)*0.6; pos[i*3+2]=r*Math.cos(p);
    const c=pal[(Math.random()*pal.length)|0]; col[i*3]=c[0];col[i*3+1]=c[1];col[i*3+2]=c[2]; }
  const pg = new THREE.BufferGeometry();
  pg.setAttribute('position', new THREE.BufferAttribute(pos,3)); pg.setAttribute('color', new THREE.BufferAttribute(col,3));
  group.add(new THREE.Points(pg, new THREE.PointsMaterial({ size:0.085, vertexColors:true, transparent:true, opacity:.85, depthWrite:false, blending:THREE.AdditiveBlending })));
  const shapes=[]; const geos=[new THREE.IcosahedronGeometry(2.2,0), new THREE.TorusGeometry(1.7,0.5,10,28), new THREE.OctahedronGeometry(1.6,0)];
  const cols = PAGE.shapeCols || [0x22d3ee,0xc084fc,0xf472b6];
  for (let i=0;i<3;i++){ const m=new THREE.Mesh(geos[i], new THREE.MeshBasicMaterial({ color:cols[i], wireframe:true, transparent:true, opacity:.26 }));
    m.position.set((i-1)*8.5,(i%2?3:-3),-3-i*2); scene.add(m); shapes.push(m); }
  let tx=0,ty=0,cx=0,cy=0;
  addEventListener('pointermove', e=>{ tx=e.clientX/innerWidth-.5; ty=e.clientY/innerHeight-.5; }, { passive:true });
  addEventListener('resize', ()=>{ camera.aspect=innerWidth/innerHeight; camera.updateProjectionMatrix(); renderer.setSize(innerWidth,innerHeight); });
  let running=true; document.addEventListener('visibilitychange', ()=>{ running=!document.hidden; if(running) loop(); });
  const clock=new THREE.Clock();
  function loop(){ if(!running) return; requestAnimationFrame(loop); const t=clock.getElapsedTime();
    group.rotation.y=t*0.04; group.rotation.x=Math.sin(t*0.15)*0.12;
    shapes.forEach((s,i)=>{ s.rotation.x+=0.002+i*0.0008; s.rotation.y+=0.003; s.position.y+=Math.sin(t*0.6+i)*0.004; });
    cx+=(tx-cx)*0.04; cy+=(ty-cy)*0.04; camera.position.x=cx*4; camera.position.y=-cy*3; camera.lookAt(0,0,0);
    renderer.render(scene,camera); }
  loop();
})();
