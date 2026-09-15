/* Shared bits: nav, formatting, the neuron scatter, the fly cursor. */
(function () {
  const NAV = [
    ["index.html", "Studio"],
    ["demo.html", "Demonstration"],
    ["gallery.html", "Gallery"],
    ["brain.html", "The brain"],
  ];

  function here() {
    const p = location.pathname.split("/").pop() || "index.html";
    return p;
  }

  window.CC = {
    fmt(n) { return n == null ? "—" : Number(n).toLocaleString("en-US"); },
    fmt1(n) { return n == null ? "—" : Number(n).toFixed(1); },
    hhmm(t) {
      const d = new Date(t * 1000);
      return d.toISOString().slice(11, 19);
    },
    short(a) { return a ? a.slice(0, 6) + "…" + a.slice(-4) : "—"; },
    esc(s) { return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); },

    mountNav(status) {
      const nav = document.querySelector(".nav");
      if (!nav) return;
      const cur = here();
      nav.innerHTML = `
        <a class="brand" href="index.html">Canvas Fly<small>a connectome that paints</small></a>
        <div class="links">${NAV.map(([h, t]) => `<a href="${h}" class="${h === cur ? "on" : ""}">${t}</a>`).join("")}</div>
        <a class="x xbtn" href="https://x.com/ConVas_fly" target="_blank" rel="noopener" title="@ConVas_fly on X">
          <svg viewBox="0 0 24 24" width="13" height="13" aria-hidden="true"><path fill="currentColor" d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231zm-1.161 17.52h1.833L7.084 4.126H5.117z"/></svg>
          @ConVas_fly</a>
        <a class="x" href="https://github.com/bnbhacker/connectome-canvas" target="_blank" rel="noopener">source ↗</a>`;
    },

    /* Where is the live studio? Same origin (local server or a Vercel rewrite) first,
       then the address the studio wrote into live.json when it opened its tunnel. */
    fetchT(u, ms) {
      const c = new AbortController(); const t = setTimeout(() => c.abort(), ms);
      return fetch(u, { cache: "no-store", signal: c.signal }).finally(() => clearTimeout(t));
    },
    async resolveBase() {
      if (CC._base !== undefined) return CC._base;
      const local = /^(localhost|127\.0\.0\.1|\[::1\])$/.test(location.hostname);
      const sameOrigin = async () => {
        try { const r = await CC.fetchT("api/info", local ? 4000 : 2500); if (r.ok) { const j = await r.json(); if (j && j.neurons) return ""; } } catch (e) {}
        return null;
      };
      const viaLive = async () => {
        try {
          const l = await (await CC.fetchT("live.json", 3000)).json();
          if (l && l.studio) {
            const base = String(l.studio).replace(/\/$/, "");
            const r = await CC.fetchT(base + "/api/info", 8000);
            if (r.ok) return base;
          }
        } catch (e) {}
        return null;
      };
      // on the public site the tunnel address is the likely answer, so ask for it first
      const order = local ? [sameOrigin, viaLive] : [viaLive, sameOrigin];
      for (const probe of order) { const b = await probe(); if (b !== null) return (CC._base = b); }
      return (CC._base = null);
    },
    U(p) { return CC._base ? CC._base + "/" + p : p; },

    copy(text, btn) {
      navigator.clipboard?.writeText(text).then(() => {
        if (!btn) return;
        const old = btn.textContent; btn.textContent = "copied";
        setTimeout(() => (btn.textContent = old), 1200);
      });
    },

    /* the fly cursor: a small SVG fly seen from above, body along +x, rotated by heading */
    flySVG(colour) {
      const c = colour || "#f0b35a";
      return `<svg viewBox="-13 -13 26 26" xmlns="http://www.w3.org/2000/svg">
        <ellipse cx="-2" cy="-6" rx="7" ry="3.2" fill="rgba(255,255,255,.35)" transform="rotate(-25 -2 -6)"/>
        <ellipse cx="-2" cy="6" rx="7" ry="3.2" fill="rgba(255,255,255,.35)" transform="rotate(25 -2 6)"/>
        <ellipse cx="0" cy="0" rx="6.5" ry="3.4" fill="${c}"/>
        <circle cx="6.5" cy="0" r="2.6" fill="#1a1a1f" stroke="${c}" stroke-width="1"/>
        <circle cx="7.6" cy="-1.1" r=".8" fill="#ff5a5a"/><circle cx="7.6" cy="1.1" r=".8" fill="#ff5a5a"/>
        <path d="M-6 0 L-10 0" stroke="${c}" stroke-width="1.2"/>
      </svg>`;
    },

    /* scatter of soma positions; xyz in 0..1000, cls codes; fired = indices to flash */
    Scatter(canvas, neurons) {
      const ctx = canvas.getContext("2d");
      const COL = ["#4fb7c4", "#b06fd6", "#d9a441", "#f0b35a", "#8de08a"]; // optic, central, vnc, descending, kenyon
      const pts = neurons.xyz, cls = neurons.cls;
      let flash = new Float32Array(pts.length);
      let w = 0, h = 0, dpr = 1;
      function size() {
        dpr = Math.min(2, window.devicePixelRatio || 1);
        w = canvas.clientWidth; h = canvas.clientHeight;
        canvas.width = w * dpr; canvas.height = h * dpr;
      }
      size();
      window.addEventListener("resize", () => { size(); draw(); });
      // project: x -> horizontal, y -> vertical (front view); z ignored, keeps it deterministic
      function px(p) { return [(p[0] / 1000) * (w - 24) + 12, (p[1] / 1000) * (h - 24) + 12]; }
      function draw() {
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.fillStyle = "#0b0b0f"; ctx.fillRect(0, 0, w, h);
        for (let i = 0; i < pts.length; i++) {
          const [x, y] = px(pts[i]);
          const f = flash[i];
          if (f > 0.02) {
            ctx.globalAlpha = Math.min(1, 0.35 + f);
            ctx.fillStyle = cls[i] === 3 ? "#ffd27a" : "#ffffff";
            ctx.beginPath(); ctx.arc(x, y, cls[i] === 3 ? 2.6 : 1.6 + f * 1.4, 0, 6.283); ctx.fill();
          } else {
            ctx.globalAlpha = cls[i] === 3 ? 0.9 : 0.28;
            ctx.fillStyle = COL[cls[i]] || "#888";
            ctx.beginPath(); ctx.arc(x, y, cls[i] === 3 ? 1.8 : 0.9, 0, 6.283); ctx.fill();
          }
        }
        ctx.globalAlpha = 1;
      }
      function fire(indices) {
        for (let i = 0; i < flash.length; i++) flash[i] *= 0.55;
        for (const i of indices) if (i < flash.length) flash[i] = 1;
        draw();
      }
      draw();
      return { fire, draw };
    },
  };

  document.addEventListener("DOMContentLoaded", () => CC.mountNav());
})();
