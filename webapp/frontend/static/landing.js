/* ComplyBIM — landing page behavior.
 *
 * Three independent pieces, all presentational and all no-ops once the user
 * signs in and the landing section is hidden:
 *
 *   1. the scroll-driven 3D model: writes a single 0..1 progress value onto
 *      the stage as --p; every transform in the CSS derives from it;
 *   2. the two workflow panels: hover previews the detail (CSS), click pins
 *      it open and keeps aria-expanded honest;
 *   3. the stat counters: count up once, the first time they are seen.
 *
 * Nothing here touches app.js or auth.js state. The sign-in routing still
 * belongs to layout.js, which binds every .landing-option by data-intent —
 * including the CTA buttons inside these panels.
 */
"use strict";

(() => {
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

  // --- 1. scroll-driven model -----------------------------------------------------
  const stage = document.getElementById("tower-stage");
  if (stage) {
    // The model completes its assembly over roughly the first viewport of
    // scrolling, which is the span where it is actually on screen.
    const travel = () => Math.max(window.innerHeight * 0.9, 1);
    let ticking = false;

    function render() {
      ticking = false;
      const p = Math.min(Math.max(window.scrollY / travel(), 0), 1);
      stage.style.setProperty("--p", p.toFixed(4));
    }
    function onScroll() {
      if (ticking) return;
      ticking = true;
      requestAnimationFrame(render);
    }

    if (reducedMotion.matches) {
      // Hold the assembled pose; no scroll coupling.
      stage.style.setProperty("--p", "1");
    } else {
      window.addEventListener("scroll", onScroll, { passive: true });
      window.addEventListener("resize", onScroll, { passive: true });
      render();
    }
  }

  // --- 2. workflow panels ---------------------------------------------------------
  document.querySelectorAll(".wf-panel").forEach((panel) => {
    const head = panel.querySelector(".wf-head");
    if (!head) return;
    head.addEventListener("click", () => {
      const open = panel.classList.toggle("is-open");
      head.setAttribute("aria-expanded", String(open));
      // Only one panel pinned at a time — two open panels push the page
      // around more than they help.
      if (open) {
        document.querySelectorAll(".wf-panel.is-open").forEach((other) => {
          if (other === panel) return;
          other.classList.remove("is-open");
          other.querySelector(".wf-head")?.setAttribute("aria-expanded", "false");
        });
      }
    });
  });

  // --- 3. stat counters -----------------------------------------------------------
  const stats = document.getElementById("landing-stats");
  if (stats) {
    const numbers = [...stats.querySelectorAll(".stat-num")];

    function countUp(el) {
      const target = parseFloat(el.dataset.to);
      const decimals = parseInt(el.dataset.decimals || "0", 10);
      if (!isFinite(target)) return;
      if (reducedMotion.matches) {
        el.textContent = target.toFixed(decimals);
        return;
      }
      const duration = 1100;
      const start = performance.now();
      (function step(now) {
        const t = Math.min((now - start) / duration, 1);
        // ease-out cubic: fast first, settles on the real figure
        const eased = 1 - Math.pow(1 - t, 3);
        el.textContent = (target * eased).toFixed(decimals);
        if (t < 1) requestAnimationFrame(step);
        else el.textContent = target.toFixed(decimals);
      })(start);
    }

    if (!("IntersectionObserver" in window)) {
      numbers.forEach(countUp);
    } else {
      const observer = new IntersectionObserver((entries) => {
        entries.forEach((entry) => {
          if (!entry.isIntersecting) return;
          countUp(entry.target);
          observer.unobserve(entry.target);
        });
      }, { threshold: 0.6 });
      numbers.forEach((el) => observer.observe(el));
    }
  }

  // --- 4. sign-in backdrop parallax -------------------------------------------------
  const skeleton = document.getElementById("skeleton-bg");
  if (skeleton && !reducedMotion.matches && window.matchMedia("(hover: hover) and (pointer: fine)").matches) {
    let pending = false;
    let mx = 0;
    let my = 0;
    window.addEventListener("pointermove", (event) => {
      // -1..1 from the centre of the viewport
      mx = (event.clientX / window.innerWidth) * 2 - 1;
      my = (event.clientY / window.innerHeight) * 2 - 1;
      if (pending) return;
      pending = true;
      requestAnimationFrame(() => {
        pending = false;
        skeleton.style.setProperty("--mx", mx.toFixed(3));
        skeleton.style.setProperty("--my", my.toFixed(3));
      });
    }, { passive: true });
  }

  // --- reveal-on-scroll ------------------------------------------------------------
  const revealables = document.querySelectorAll(".landing-stats, .wf-panel, .section-lede, .landing-credits");
  if (revealables.length && "IntersectionObserver" in window && !reducedMotion.matches) {
    revealables.forEach((el) => el.classList.add("reveal"));
    const observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        entry.target.classList.add("in-view");
        observer.unobserve(entry.target);
      });
    }, { threshold: 0.15, rootMargin: "0px 0px -8% 0px" });
    revealables.forEach((el) => observer.observe(el));
  }
})();
