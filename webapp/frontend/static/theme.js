/* ComplyBIM — day/night theme.
 *
 * Loaded synchronously in <head> so data-theme is set before first paint
 * (no flash of the wrong theme). Until the user picks one explicitly, the
 * theme follows the OS preference live; after that the choice is kept in
 * localStorage. The toggle uses a circular View Transition reveal where the
 * browser supports it and the user hasn't asked for reduced motion.
 */
"use strict";

(() => {
  const STORAGE_KEY = "complybim:theme";
  const root = document.documentElement;
  const systemDark = window.matchMedia("(prefers-color-scheme: dark)");
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

  function storedTheme() {
    try {
      const value = localStorage.getItem(STORAGE_KEY);
      return value === "light" || value === "dark" ? value : null;
    } catch (_) {
      return null;
    }
  }

  function apply(theme) {
    root.dataset.theme = theme;
    const button = document.getElementById("theme-toggle");
    if (button) {
      const next = theme === "dark" ? "day" : "night";
      button.setAttribute("aria-label", `Switch to ${next} mode`);
      button.title = `Switch to ${next} mode`;
    }
  }

  apply(storedTheme() || (systemDark.matches ? "dark" : "light"));

  systemDark.addEventListener("change", (event) => {
    if (!storedTheme()) apply(event.matches ? "dark" : "light");
  });

  function toggle(event) {
    const next = root.dataset.theme === "dark" ? "light" : "dark";
    try { localStorage.setItem(STORAGE_KEY, next); } catch (_) {}

    if (!document.startViewTransition || reducedMotion.matches) {
      apply(next);
      return;
    }
    // Reveal the new theme as a circle growing out of the toggle button.
    const rect = event.currentTarget.getBoundingClientRect();
    const x = rect.left + rect.width / 2;
    const y = rect.top + rect.height / 2;
    const radius = Math.hypot(Math.max(x, innerWidth - x), Math.max(y, innerHeight - y));
    const transition = document.startViewTransition(() => apply(next));
    transition.ready.then(() => {
      root.animate(
        {clipPath: [`circle(0px at ${x}px ${y}px)`, `circle(${radius}px at ${x}px ${y}px)`]},
        {duration: 520, easing: "cubic-bezier(.4, 0, .2, 1)", pseudoElement: "::view-transition-new(root)"},
      );
    }).catch(() => {});
  }

  document.addEventListener("DOMContentLoaded", () => {
    const button = document.getElementById("theme-toggle");
    if (!button) return;
    apply(root.dataset.theme);
    button.addEventListener("click", toggle);
  });
})();
