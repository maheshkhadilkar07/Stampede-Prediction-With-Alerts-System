/**
 * simulation.js
 * ---------------
 * Crowd-crush ("stampede") simulation for presentations, now with GATES,
 * OFFICERS and PEOPLE you can add (sliders or click-to-place).
 *
 *   * People flow toward the NEAREST gate and exit through it; too few gates /
 *     too many people => a jam builds and density rises (more gates relieve it).
 *   * When risk reaches HIGH/CRITICAL, OFFICERS move to the hotspot (densest
 *     cell); the console reports how many officers are "on scene".
 *   * The density grid + risk classifier reuse the SAME thresholds as the real
 *     backend (6x8 grid, density 1.5/3/5/7.5, surge 0.35), so the demo analyzes
 *     crowds the way the real system does.
 *
 * Two renderers read the same model each frame: a 2D top-down canvas (with
 * click-to-place) and a 3D Three.js scene. The pure model + risk functions are
 * exported for Node unit tests.
 */
(function (global) {
  "use strict";

  const DEFAULT_CFG = {
    rows: 6, cols: 8,
    densities: { LOW: 1.5, MEDIUM: 3.0, HIGH: 5.0, CRITICAL: 7.5 },
    surge: 0.35,
    colors: { SAFE: "#28a745", LOW: "#ffc107", MEDIUM: "#fd7e14", HIGH: "#dc3545", CRITICAL: "#7d0d1b" },
  };
  const LEVELS = ["SAFE", "LOW", "MEDIUM", "HIGH", "CRITICAL"];
  const OFFICER_COLOR = "#2563eb";

  function riskFromDensity(maxCellDensity, cfg) {
    const d = (cfg || DEFAULT_CFG).densities;
    if (maxCellDensity >= d.CRITICAL) return "CRITICAL";
    if (maxCellDensity >= d.HIGH) return "HIGH";
    if (maxCellDensity >= d.MEDIUM) return "MEDIUM";
    if (maxCellDensity >= d.LOW) return "LOW";
    return "SAFE";
  }
  function escalateForSurge(level, growthRate, cfg) {
    const surge = (cfg || DEFAULT_CFG).surge;
    if (growthRate > surge) return LEVELS[Math.min(LEVELS.indexOf(level) + 1, LEVELS.length - 1)];
    return level;
  }
  function classifyRisk(maxCellDensity, growthRate, cfg) {
    return escalateForSurge(riskFromDensity(maxCellDensity, cfg), growthRate, cfg);
  }
  function clamp(v, lo, hi) { return v < lo ? lo : v > hi ? hi : v; }

  // -------------------------------------------------------------------------
  // MODEL
  // -------------------------------------------------------------------------
  class CrowdModel {
    constructor(opts) {
      opts = opts || {};
      this.W = opts.width || 120;
      this.H = opts.height || 70;
      this.cfg = opts.cfg || DEFAULT_CFG;
      this.r = 1.0;
      this.people = [];
      this.officers = [];
      this.gates = [];
      this.panic = 0;
      this._countHist = [];
      this._t = 0; this._sampleAt = 0; this.growthRate = 0;
      this.addGate(this.W - 2, this.H / 2);           // one exit by default
      this.spawnPeople(opts.initial != null ? opts.initial : 60);
    }

    // Backward-compat alias (older callers/tests use `.agents`).
    get agents() { return this.people; }

    _rand(a, b) { return a + Math.random() * (b - a); }

    addPerson(x, y) {
      this.people.push({
        x: x != null ? clamp(x, this.r, this.W - this.r) : this._rand(2, this.W * 0.28),
        y: y != null ? clamp(y, this.r, this.H - this.r) : this._rand(this.r + 1, this.H - this.r - 1),
        vx: 0, vy: 0, speed: this._rand(7, 11),
      });
    }
    spawnPeople(n) { for (let i = 0; i < n; i++) this.addPerson(); }
    spawn(n) { this.spawnPeople(n); }  // alias

    addOfficer(x, y) {
      const px = x != null ? x : this._rand(4, this.W * 0.3);
      const py = y != null ? y : this._rand(this.H * 0.6, this.H - 3);
      this.officers.push({ x: px, y: py, vx: 0, vy: 0, home: { x: px, y: py }, onScene: false });
    }
    addGate(x, y) {
      this.gates.push({ x: clamp(x, 1, this.W - 1), y: clamp(y, 2, this.H - 2) });
    }

    setPeople(n) {
      n = Math.max(0, Math.min(n, 800));
      while (this.people.length < n) this.addPerson();
      while (this.people.length > n) this.people.pop();
    }
    setOfficers(n) {
      n = Math.max(0, Math.min(n, 60));
      while (this.officers.length < n) this.addOfficer();
      while (this.officers.length > n) this.officers.pop();
    }
    setGates(n) {
      n = Math.max(0, Math.min(n, 12));
      // reposition evenly on the right wall for a tidy layout
      this.gates = [];
      for (let i = 0; i < n; i++) this.addGate(this.W - 2, this.H * (i + 1) / (n + 1));
    }

    surge(n) { this.panic = 1; this.spawnPeople(n != null ? n : 120); }

    reset(initial) {
      this.people = []; this.officers = []; this.gates = [];
      this.panic = 0; this._countHist = []; this._t = 0; this._sampleAt = 0; this.growthRate = 0;
      this.addGate(this.W - 2, this.H / 2);
      this.spawnPeople(initial != null ? initial : 60);
    }

    _nearestGate(x, y) {
      let best = null, bd = Infinity;
      for (const g of this.gates) {
        const d = (g.x - x) * (g.x - x) + (g.y - y) * (g.y - y);
        if (d < bd) { bd = d; best = g; }
      }
      return best;
    }

    densityGrid() {
      const rows = this.cfg.rows, cols = this.cfg.cols;
      const counts = Array.from({ length: rows }, () => new Array(cols).fill(0));
      const cw = this.W / cols, ch = this.H / rows;
      for (const a of this.people) {
        const c = clamp(Math.floor(a.x / cw), 0, cols - 1);
        const rr = clamp(Math.floor(a.y / ch), 0, rows - 1);
        counts[rr][c] += 1;
      }
      let max = 0, mr = 0, mc = 0;
      for (let r = 0; r < rows; r++) for (let c = 0; c < cols; c++) {
        if (counts[r][c] > max) { max = counts[r][c]; mr = r; mc = c; }
      }
      return { counts, max, rows, cols,
               hotCell: { r: mr, c: mc, x: (mc + 0.5) * cw, y: (mr + 0.5) * ch } };
    }

    step(dt) {
      dt = Math.min(dt, 0.05);
      this._t += dt;
      this.panic = Math.max(0, this.panic - dt * 0.15);
      const W = this.W, H = this.H, r = this.r, speedMul = 1 + this.panic * 0.8;

      // People: steer to nearest gate.
      for (const a of this.people) {
        const g = this._nearestGate(a.x, a.y);
        let dx, dy;
        if (g) { dx = g.x - a.x; dy = g.y - a.y; } else { dx = W / 2 - a.x; dy = H / 2 - a.y; }
        const len = Math.hypot(dx, dy) || 1;
        a.vx = (dx / len) * a.speed * speedMul;
        a.vy = (dy / len) * a.speed * speedMul;
      }
      // Repulsion between people (crowd pressure).
      const md = r * 2;
      const P = this.people;
      for (let i = 0; i < P.length; i++) {
        for (let j = i + 1; j < P.length; j++) {
          const dx = P[j].x - P[i].x, dy = P[j].y - P[i].y, d2 = dx * dx + dy * dy;
          if (d2 < md * md && d2 > 1e-6) {
            const d = Math.sqrt(d2), f = ((md - d) / md) * 6 * speedMul, ux = dx / d, uy = dy / d;
            P[i].vx -= ux * f; P[i].vy -= uy * f; P[j].vx += ux * f; P[j].vy += uy * f;
          }
        }
      }
      // Integrate people; exit when they reach their gate.
      const keep = [];
      for (const a of P) {
        a.x = clamp(a.x + a.vx * dt, r, W - r);
        a.y = clamp(a.y + a.vy * dt, r, H - r);
        const g = this._nearestGate(a.x, a.y);
        const atGate = g && Math.hypot(g.x - a.x, g.y - a.y) < 2.0;
        if (!atGate) keep.push(a);
      }
      this.people = keep;

      // Officers: respond to the hotspot when risk is HIGH/CRITICAL, else go home.
      const g = this.densityGrid();
      const level = classifyRisk(g.max, this.growthRate, this.cfg);
      const responding = (level === "HIGH" || level === "CRITICAL");
      const target = responding ? g.hotCell : null;
      for (const o of this.officers) {
        const tx = target ? target.x : o.home.x, ty = target ? target.y : o.home.y;
        const dx = tx - o.x, dy = ty - o.y, len = Math.hypot(dx, dy);
        if (len > 0.6) { o.x += (dx / len) * 14 * dt; o.y += (dy / len) * 14 * dt; }
        o.x = clamp(o.x, r, W - r); o.y = clamp(o.y, r, H - r);
        o.onScene = responding && target && Math.hypot(target.x - o.x, target.y - o.y) < 12;
      }

      // Sample people count every 0.5s for the growth rate.
      if (this._t - this._sampleAt >= 0.5) {
        this._sampleAt = this._t;
        this._countHist.push(this.people.length);
        if (this._countHist.length > 6) this._countHist.shift();
        const first = this._countHist[0], last = this._countHist[this._countHist.length - 1];
        this.growthRate = first > 0 ? (last - first) / Math.max(first, 1) : 0;
      }
    }

    hotspot() { return this.densityGrid().hotCell; }

    stats() {
      const g = this.densityGrid();
      const level = classifyRisk(g.max, this.growthRate, this.cfg);
      const responding = (level === "HIGH" || level === "CRITICAL");
      const onScene = responding ? this.officers.filter((o) => o.onScene).length : 0;
      return {
        count: this.people.length,
        maxCellDensity: g.max,
        growthRate: this.growthRate,
        riskLevel: level,
        riskColor: this.cfg.colors[level],
        officersTotal: this.officers.length,
        officersOnScene: onScene,
        gates: this.gates.length,
        hotspot: responding ? g.hotCell : null,
      };
    }
  }

  function cellColor(count, cfg) { return cfg.colors[riskFromDensity(count, cfg)]; }

  // -------------------------------------------------------------------------
  // 2D RENDERER
  // -------------------------------------------------------------------------
  class Renderer2D {
    constructor(canvas, model) { this.canvas = canvas; this.ctx = canvas.getContext("2d"); this.model = model; }
    resize() {
      const rect = this.canvas.getBoundingClientRect(), dpr = global.devicePixelRatio || 1;
      this.canvas.width = rect.width * dpr; this.canvas.height = rect.height * dpr;
      this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      this._w = rect.width; this._h = rect.height;
    }
    canvasToModel(px, py) {
      if (!this._w) this.resize();
      return { x: px / (this._w / this.model.W), y: py / (this._h / this.model.H) };
    }
    draw() {
      const m = this.model, ctx = this.ctx;
      if (!this._w) this.resize();
      const sx = this._w / m.W, sy = this._h / m.H;
      ctx.clearRect(0, 0, this._w, this._h);

      const g = m.densityGrid();
      const cw = this._w / g.cols, ch = this._h / g.rows;
      for (let r = 0; r < g.rows; r++) for (let c = 0; c < g.cols; c++) {
        const n = g.counts[r][c];
        if (n <= 0) continue;
        ctx.globalAlpha = Math.min(0.08 + n * 0.05, 0.5);
        ctx.fillStyle = cellColor(n, m.cfg);
        ctx.fillRect(c * cw, r * ch, cw, ch);
      }
      ctx.globalAlpha = 1;
      ctx.strokeStyle = "rgba(15,23,42,0.06)"; ctx.lineWidth = 1;
      for (let c = 1; c < g.cols; c++) { ctx.beginPath(); ctx.moveTo(c * cw, 0); ctx.lineTo(c * cw, this._h); ctx.stroke(); }
      for (let r = 1; r < g.rows; r++) { ctx.beginPath(); ctx.moveTo(0, r * ch); ctx.lineTo(this._w, r * ch); ctx.stroke(); }

      // Gates (green doorways).
      for (const ga of m.gates) {
        ctx.fillStyle = "#16a34a";
        ctx.fillRect(ga.x * sx - 3, ga.y * sy - 11, 6, 22);
        ctx.fillStyle = "#166534"; ctx.font = "10px 'Inter',sans-serif";
        ctx.fillText("GATE", ga.x * sx - 26, ga.y * sy + 4);
      }

      // People (coloured by local cell density).
      const rad = Math.max(2.0, m.r * ((sx + sy) / 2) * 0.6);
      for (const a of m.people) {
        const c = clamp(Math.floor(a.x / (m.W / g.cols)), 0, g.cols - 1);
        const rr = clamp(Math.floor(a.y / (m.H / g.rows)), 0, g.rows - 1);
        ctx.fillStyle = cellColor(g.counts[rr][c], m.cfg);
        ctx.beginPath(); ctx.arc(a.x * sx, a.y * sy, rad, 0, Math.PI * 2); ctx.fill();
      }

      // Hotspot ring when officers are responding.
      const st = m.stats();
      if (st.hotspot) {
        ctx.strokeStyle = "#7d0d1b"; ctx.lineWidth = 3;
        ctx.beginPath(); ctx.arc(st.hotspot.x * sx, st.hotspot.y * sy, 16, 0, Math.PI * 2); ctx.stroke();
      }

      // Officers (blue diamonds; filled when on scene).
      for (const o of m.officers) {
        const X = o.x * sx, Y = o.y * sy, s = 6;
        ctx.beginPath();
        ctx.moveTo(X, Y - s); ctx.lineTo(X + s, Y); ctx.lineTo(X, Y + s); ctx.lineTo(X - s, Y); ctx.closePath();
        ctx.fillStyle = o.onScene ? OFFICER_COLOR : "#ffffff";
        ctx.fill(); ctx.lineWidth = 2; ctx.strokeStyle = OFFICER_COLOR; ctx.stroke();
      }
    }
  }

  // -------------------------------------------------------------------------
  // 3D RENDERER (Three.js) — minimal orbit camera
  // -------------------------------------------------------------------------
  class Renderer3D {
    constructor(container, model, THREE) {
      this.container = container; this.model = model; this.THREE = THREE;
      this.pMeshes = []; this.oMeshes = []; this.gMeshes = [];
      this.angle = 0.6; this.elev = 0.9; this.dist = 170; this.autoRotate = true;
      this._init(); this._bindDrag();
    }
    _init() {
      const THREE = this.THREE, m = this.model;
      const w = this.container.clientWidth || 600, h = this.container.clientHeight || 400;
      this.scene = new THREE.Scene(); this.scene.background = new THREE.Color(0xeef2f8);
      this.camera = new THREE.PerspectiveCamera(50, w / h, 0.1, 3000);
      this.renderer = new THREE.WebGLRenderer({ antialias: true });
      this.renderer.setPixelRatio(global.devicePixelRatio || 1);
      this.renderer.setSize(w, h); this.container.appendChild(this.renderer.domElement);
      this.scene.add(new THREE.AmbientLight(0xffffff, 0.78));
      const dir = new THREE.DirectionalLight(0xffffff, 0.6); dir.position.set(60, 120, 40); this.scene.add(dir);
      const ground = new THREE.Mesh(new THREE.PlaneGeometry(m.W, m.H), new THREE.MeshStandardMaterial({ color: 0xdbe3ee }));
      ground.rotation.x = -Math.PI / 2; this.scene.add(ground);
      this.personGeo = new THREE.CylinderGeometry(0.9, 0.9, 4, 8);
      this.officerGeo = new THREE.CylinderGeometry(1.1, 1.1, 6, 10);
      this.gateGeo = new THREE.BoxGeometry(1.5, 10, 8);
      this.hotMat = new THREE.MeshStandardMaterial({ color: 0x7d0d1b });
      this.hotMesh = new THREE.Mesh(new THREE.TorusGeometry(10, 0.8, 8, 24), this.hotMat);
      this.hotMesh.rotation.x = -Math.PI / 2; this.hotMesh.visible = false; this.scene.add(this.hotMesh);
      this._sync();
    }
    _bindDrag() {
      const el = this.renderer.domElement; let drag = false, lx = 0, ly = 0; el.style.cursor = "grab";
      el.addEventListener("mousedown", (e) => { drag = true; this.autoRotate = false; lx = e.clientX; ly = e.clientY; el.style.cursor = "grabbing"; });
      global.addEventListener("mouseup", () => { drag = false; el.style.cursor = "grab"; });
      global.addEventListener("mousemove", (e) => {
        if (!drag) return;
        this.angle -= (e.clientX - lx) * 0.01; this.elev = clamp(this.elev - (e.clientY - ly) * 0.01, 0.2, 1.45);
        lx = e.clientX; ly = e.clientY;
      });
      el.addEventListener("wheel", (e) => { e.preventDefault(); this.dist = clamp(this.dist + e.deltaY * 0.1, 80, 360); }, { passive: false });
    }
    _grow(arr, geo, color, need) {
      const THREE = this.THREE;
      while (arr.length < need) { const mesh = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ color: color })); this.scene.add(mesh); arr.push(mesh); }
      while (arr.length > need) { this.scene.remove(arr.pop()); }
    }
    _sync() {
      const m = this.model;
      this._grow(this.pMeshes, this.personGeo, 0x28a745, m.people.length);
      this._grow(this.oMeshes, this.officerGeo, 0x2563eb, m.officers.length);
      this._grow(this.gMeshes, this.gateGeo, 0x16a34a, m.gates.length);
    }
    resize() {
      const w = this.container.clientWidth || 600, h = this.container.clientHeight || 400;
      this.camera.aspect = w / h; this.camera.updateProjectionMatrix(); this.renderer.setSize(w, h);
    }
    draw() {
      const m = this.model, THREE = this.THREE; this._sync();
      const g = m.densityGrid();
      for (let i = 0; i < m.people.length; i++) {
        const a = m.people[i], mesh = this.pMeshes[i];
        mesh.position.set(a.x - m.W / 2, 2, a.y - m.H / 2);
        const c = clamp(Math.floor(a.x / (m.W / g.cols)), 0, g.cols - 1);
        const rr = clamp(Math.floor(a.y / (m.H / g.rows)), 0, g.rows - 1);
        mesh.material.color.set(cellColor(g.counts[rr][c], m.cfg));
      }
      for (let i = 0; i < m.officers.length; i++) {
        const o = m.officers[i]; this.oMeshes[i].position.set(o.x - m.W / 2, 3, o.y - m.H / 2);
      }
      for (let i = 0; i < m.gates.length; i++) {
        const ga = m.gates[i]; this.gMeshes[i].position.set(ga.x - m.W / 2, 5, ga.y - m.H / 2);
      }
      const st = m.stats();
      if (st.hotspot) { this.hotMesh.visible = true; this.hotMesh.position.set(st.hotspot.x - m.W / 2, 0.5, st.hotspot.y - m.H / 2); }
      else { this.hotMesh.visible = false; }

      if (this.autoRotate) this.angle += 0.0035;
      const cx = Math.cos(this.angle) * Math.cos(this.elev) * this.dist;
      const cz = Math.sin(this.angle) * Math.cos(this.elev) * this.dist;
      const cy = Math.sin(this.elev) * this.dist;
      this.camera.position.set(cx, cy, cz); this.camera.lookAt(0, 0, 0);
      this.renderer.render(this.scene, this.camera);
    }
    dispose() {
      if (this.renderer) { this.renderer.dispose(); const d = this.renderer.domElement; if (d && d.parentNode) d.parentNode.removeChild(d); }
    }
  }

  const API = { DEFAULT_CFG, LEVELS, OFFICER_COLOR, CrowdModel, Renderer2D, Renderer3D,
                riskFromDensity, escalateForSurge, classifyRisk };
  global.StampedeSim = API;
  if (typeof module !== "undefined" && module.exports) module.exports = API;
})(typeof window !== "undefined" ? window : globalThis);
