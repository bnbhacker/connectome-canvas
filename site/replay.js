/* Replay engine: plays a recorded sitting (a "score") in the browser, no brain required.
   A score is site/recordings/canvas-XXXX.json written by canvas/painter.py. */
(function () {
  const COLS = ["x", "y", "lift", "reverse", "width", "colour", "spikes", "firing",
                "dna02_l", "dna02_r", "dna01", "mdn", "dnp09", "fired"];

  function hexColumns(n) {
    const R = 18, pts = [];
    for (let q = -R; q <= R; q++) for (let r = -R; r <= R; r++) {
      const s = -q - r;
      if (Math.max(Math.abs(q), Math.abs(r), Math.abs(s)) <= R) pts.push([Math.sqrt(3) * (q + r / 2), 1.5 * r]);
    }
    pts.sort((a, b) => Math.hypot(a[0], a[1]) - Math.hypot(b[0], b[1]));
    const kept = pts.slice(0, n);
    let m = 0; for (const p of kept) m = Math.max(m, Math.abs(p[0]), Math.abs(p[1]));
    return kept.map(p => [p[0] / m, p[1] / m]);
  }

  window.Replay = function Replay(opts) {
    const { canvas, eyeCanvas, cursorEl, stageEl, onTick, onEnd } = opts;
    const ctx = canvas.getContext("2d");
    const ectx = eyeCanvas ? eyeCanvas.getContext("2d") : null;
    const hex = hexColumns(892);
    let score = null, i = 0, playing = false, speed = 30, acc = 0, last = 0, raf = 0;
    let prev = null, heading = 0, strokes = 0, spikes = 0, lifts = 0, reversals = 0;

    function reset() {
      i = 0; prev = null; strokes = 0; spikes = 0; lifts = 0; reversals = 0; acc = 0;
      const p = score.paper || [12, 12, 16];
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.fillStyle = `rgb(${p[0]},${p[1]},${p[2]})`;
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      placeCursor(score.ticks[0]);
    }

    function load(s) {
      score = s;
      canvas.width = s.size; canvas.height = s.size;
      reset();
      emit();
    }

    function stroke(a, b, width, colour) {
      ctx.strokeStyle = colour; ctx.fillStyle = colour;
      ctx.lineWidth = Math.max(1, width); ctx.lineCap = "round"; ctx.lineJoin = "round";
      ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); ctx.stroke();
    }

    function placeCursor(t) {
      if (!cursorEl || !stageEl || !t) return;
      const r = stageEl.getBoundingClientRect();
      cursorEl.style.left = (t[0] / score.size * r.width) + "px";
      cursorEl.style.top = (t[1] / score.size * r.height) + "px";
      cursorEl.style.transform = `rotate(${heading}rad)`;
      cursorEl.classList.toggle("lift", !!t[2]);
      if (window.CC) cursorEl.innerHTML = CC.flySVG(t[5]);
    }

    function eye(t) {
      if (!ectx) return;
      const size = eyeCanvas.width, half = 128;
      const x0 = Math.max(0, Math.min(score.size - 256, Math.round(t[0] - half)));
      const y0 = Math.max(0, Math.min(score.size - 256, Math.round(t[1] - half)));
      const img = ctx.getImageData(x0, y0, 256, 256).data;
      ectx.fillStyle = "#0c0c10"; ectx.fillRect(0, 0, size, size);
      const rad = size / 2 - 6;
      for (const [hx, hy] of hex) {
        const px = Math.min(255, Math.max(0, Math.round(128 + hx * 127)));
        const py = Math.min(255, Math.max(0, Math.round(128 + hy * 127)));
        const k = (py * 256 + px) * 4;
        const lum = (img[k] * 0.299 + img[k + 1] * 0.587 + img[k + 2] * 0.114) | 0;
        ectx.fillStyle = `rgb(${lum},${lum},${Math.min(255, lum + 12)})`;
        ectx.fillRect(size / 2 + hx * rad - 1, size / 2 + hy * rad - 1, 3, 3);
      }
    }

    function step() {
      if (!score || i >= score.ticks.length) { playing = false; onEnd && onEnd(); return false; }
      const t = score.ticks[i];
      const pos = [t[0], t[1]];
      if (prev) {
        const dx = pos[0] - prev[0], dy = pos[1] - prev[1];
        if (dx || dy) heading = Math.atan2(dy, dx);
        if (!t[2]) {
          stroke(prev, pos, t[4], t[5]);
          if (t[16] || score.mirror) stroke([score.size - prev[0], prev[1]], [score.size - pos[0], pos[1]], t[4], t[5]);
          if (prevLift) strokes++;
        }
      } else { strokes = 1; }
      if (t[2] && !prevLift) lifts++;
      if (t[3]) reversals++;
      prevLift = !!t[2];
      spikes += t[6];
      prev = pos;
      i++;
      placeCursor(t);
      if (i % 3 === 0) eye(t);
      emit(t);
      return true;
    }
    let prevLift = false;

    function emit(t) {
      onTick && onTick({
        i, n: score.ticks.length, t: t || score.ticks[Math.max(0, i - 1)], strokes, spikes, lifts, reversals, heading,
        brainMs: i * score.tick_ms, piece: score.piece,
      });
    }

    function loop(ts) {
      if (!playing) return;
      if (!last) last = ts;
      acc += (ts - last) / 1000 * speed; last = ts;
      let n = Math.floor(acc); acc -= n;
      while (n-- > 0 && step()) {}
      raf = requestAnimationFrame(loop);
    }

    return {
      load,
      play() { if (!score) return; if (i >= score.ticks.length) reset(); playing = true; last = 0; cancelAnimationFrame(raf); raf = requestAnimationFrame(loop); },
      pause() { playing = false; cancelAnimationFrame(raf); },
      toggle() { playing ? this.pause() : this.play(); return playing; },
      restart() { reset(); emit(); this.play(); },
      seek(k) { // redraw from the start up to tick k — cheap, a score is a few hundred lines
        if (!score) return; const was = playing; this.pause(); reset();
        k = Math.max(0, Math.min(score.ticks.length, k | 0));
        while (i < k) step();
        if (was) this.play();
      },
      setSpeed(v) { speed = v; },
      get playing() { return playing; },
      get index() { return i; },
      get length() { return score ? score.ticks.length : 0; },
      get score() { return score; },
    };
  };
})();
