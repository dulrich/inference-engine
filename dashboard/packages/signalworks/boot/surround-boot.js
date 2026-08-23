/* signalworks:boot v1 */
// No-flash surround resolution: stored choice -> OS preference -> Med (CC-03).
// Inline this script in <head> BEFORE any stylesheet so first paint already has
// data-surround set. Per app, set KEY to "<app>.surround.v1" and list any
// pre-rename keys in LEGACY_KEYS (SurroundControl migrates and removes them).
(function () {
  var KEY = "app.surround.v1";
  var LEGACY_KEYS = [];
  var s = null;
  try {
    s = localStorage.getItem(KEY);
    for (var i = 0; s === null && i < LEGACY_KEYS.length; i++) {
      s = localStorage.getItem(LEGACY_KEYS[i]);
    }
  } catch (e) {
    /* storage unavailable: fall through to OS preference */
  }
  if (s !== "dark" && s !== "med" && s !== "light") {
    s = matchMedia("(prefers-color-scheme: dark)").matches
      ? "dark"
      : matchMedia("(prefers-color-scheme: light)").matches
        ? "light"
        : "med";
  }
  document.documentElement.dataset.surround = s;
})();
/* signalworks:boot end */
