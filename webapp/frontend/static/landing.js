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

  // --- 1. the model: hover to take it apart ----------------------------------------
  // Hover and focus are handled in CSS. This only covers touch, where there is
  // no hover to speak of: a tap toggles the same state, so the model is not
  // simply inert on a phone.
  const stage = document.getElementById("tower-stage");
  const scene = stage?.querySelector(".tower-scene");
  if (stage && scene) {
    const canHover = window.matchMedia("(hover: hover) and (pointer: fine)").matches;
    if (!canHover) {
      scene.addEventListener("click", () => stage.classList.toggle("is-exploded"));
    }
    const caption = stage.querySelector(".tower-caption");
    if (caption) {
      caption.textContent = canHover ? "Hover to take it apart" : "Tap to take it apart";
      const dot = document.createElement("span");
      dot.className = "scan-dot";
      caption.prepend(dot);
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
