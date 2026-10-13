(() => {
  const root = document.documentElement;
  const key = "socio-theme";
  const meta = document.querySelector('meta[name="theme-color"]');

  function applyTheme(theme) {
    root.dataset.theme = theme;
    const nextTheme = theme === "dark" ? "light" : "dark";
    document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
      button.setAttribute("aria-label", `Switch to ${nextTheme} mode`);
      button.setAttribute("title", `Switch to ${nextTheme} mode`);
      const icon = button.querySelector(".theme-icon");
      const label = button.querySelector(".theme-label");
      if (icon) icon.textContent = nextTheme === "dark" ? "☾" : "☀";
      if (label) label.textContent = `${nextTheme[0].toUpperCase()}${nextTheme.slice(1)} mode`;
    });
    if (meta) meta.setAttribute("content", theme === "dark" ? "#111113" : "#f7f5f2");
  }

  let savedTheme = null;
  try {
    savedTheme = localStorage.getItem(key);
  } catch (_) {
    // The theme still works for this page when browser storage is unavailable.
  }
  const initialTheme = savedTheme === "light" || savedTheme === "dark"
    ? savedTheme
    : (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");

  applyTheme(initialTheme);
  document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
    button.addEventListener("click", () => {
      const nextTheme = root.dataset.theme === "dark" ? "light" : "dark";
      applyTheme(nextTheme);
      try {
        localStorage.setItem(key, nextTheme);
      } catch (_) {
        // The selected theme remains active until the current page is closed.
      }
    });
  });
})();
