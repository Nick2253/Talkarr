// Talkarr UI: progressive enhancement only. Every page works without this file.
(() => {
  "use strict";

  // Destructive actions take two clicks: the first arms the button and says what
  // will happen, the second submits. Disarms itself after a few seconds.
  document.querySelectorAll("button[data-confirm]").forEach((btn) => {
    const label = btn.textContent;
    let timer;
    btn.addEventListener("click", (event) => {
      if (btn.hasAttribute("data-armed")) return;
      event.preventDefault();
      btn.setAttribute("data-armed", "");
      btn.textContent = btn.dataset.confirm + " Click again";
      timer = setTimeout(() => {
        btn.removeAttribute("data-armed");
        btn.textContent = label;
      }, 4000);
    });
    btn.form && btn.form.addEventListener("submit", () => clearTimeout(timer));
  });

  // Library: keep the queue fresh while a scan is running.
  const progress = document.getElementById("progress");
  if (progress && Number(progress.dataset.busy) > 0) {
    setTimeout(() => window.location.reload(), 10000);
  }

  // Job page: cue ribbon + adjust preview.
  const dataEl = document.getElementById("cue-data");
  if (!dataEl) return;
  const data = JSON.parse(dataEl.textContent);
  const ribbon = document.getElementById("ribbon");
  const scaleEl = document.getElementById("scale");
  const offsetEl = document.getElementById("offset");

  const current = () => ({
    scale: scaleEl ? parseFloat(scaleEl.value) || 1 : data.scale,
    offset: offsetEl ? parseFloat(offsetEl.value) || 0 : data.offset,
  });

  const tc = (s) => {
    s = Math.max(0, s);
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = (s % 60).toFixed(3).padStart(6, "0").replace(".", ",");
    return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${sec}`;
  };

  function onSpeech(t) {
    if (!data.speech.length) return null;
    return data.speech.some(([a, b]) => t >= a - 0.3 && t <= b + 0.3);
  }

  function drawRibbon({ scale, offset }) {
    if (!ribbon) return;
    const W = 900, H = data.speech.length ? 150 : 112, X0 = 70, X1 = W - 12;
    const runtime = Math.max(data.runtime, 1);
    const x = (t) => X0 + (Math.min(Math.max(t, 0), runtime) / runtime) * (X1 - X0);
    const parts = [`<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Cue positions now and after the fix">`];
    const step = runtime > 5400 ? 1800 : 900;
    for (let t = 0; t <= runtime; t += step) {
      const label = `${Math.floor(t / 3600)}:${String(Math.floor((t % 3600) / 60)).padStart(2, "0")}`;
      parts.push(`<line x1="${x(t)}" y1="12" x2="${x(t)}" y2="${H - 18}" stroke="var(--rule)"/>`);
      parts.push(`<text x="${x(t)}" y="${H - 4}" fill="var(--slate-3)" font-family="var(--tc)" font-size="10" text-anchor="middle">${label}</text>`);
    }
    const lane = (y, label) =>
      `<text x="0" y="${y + 4}" fill="var(--slate-3)" font-family="var(--ui)" font-size="10" font-weight="600" letter-spacing="1.2">${label}</text>` +
      `<line x1="${X0}" y1="${y}" x2="${X1}" y2="${y}" stroke="var(--rule-strong)"/>`;
    parts.push(lane(30, "NOW"), lane(68, "AFTER"));
    if (data.speech.length) {
      parts.push(lane(106, "SPEECH"));
      for (const [a, b] of data.speech) {
        parts.push(`<rect x="${x(a)}" y="98" width="${Math.max(1.5, x(b) - x(a))}" height="16" fill="var(--speech)"/>`);
      }
    }
    // Connect each cue's position now to where the fix puts it: direction and size of
    // the change read at a glance even when the shift is small against the film.
    for (const [a] of data.cues) {
      const after = scale * a + offset;
      if (Math.abs(x(after) - x(a)) >= 0.5) {
        parts.push(`<line x1="${x(a)}" y1="39" x2="${x(after)}" y2="59" stroke="var(--slate-4)" stroke-width="1"/>`);
      }
      parts.push(`<rect x="${x(a) - 1}" y="21" width="2" height="18" fill="var(--slate-3)"/>`);
    }
    for (const [a, b] of data.cues) {
      const mid = scale * ((a + b) / 2) + offset;
      const hit = onSpeech(mid);
      const fill = hit === false ? "var(--tally)" : "var(--subtitle)";
      parts.push(`<rect x="${x(scale * a + offset) - 1}" y="59" width="2" height="18" fill="${fill}"/>`);
    }
    parts.push("</svg>");
    ribbon.innerHTML = parts.join("");
  }

  function updateTable({ scale, offset }) {
    document.querySelectorAll("tr[data-start]").forEach((row) => {
      const cell = row.querySelector("[data-after]");
      if (cell) cell.textContent = tc(scale * parseFloat(row.dataset.start) + offset);
    });
  }

  function render() {
    const now = current();
    drawRibbon(now);
    updateTable(now);
  }

  scaleEl && scaleEl.addEventListener("change", render);
  offsetEl && offsetEl.addEventListener("input", render);
  document.querySelectorAll("[data-step]").forEach((btn) =>
    btn.addEventListener("click", () => {
      offsetEl.value = ((parseFloat(offsetEl.value) || 0) + parseFloat(btn.dataset.step)).toFixed(2);
      render();
    })
  );
  render();
})();
