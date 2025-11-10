/* 4d-common-style.js
 * 4DReplay shared dark theme + components.
 * Works across plain HTML pages without dependencies.
 *
 * Usage (auto-injects on load):
 *   <script src="./4d-common-style.js"></script>
 *   // optional: window.FourDCommon.inject(); // to reinject into ShadowRoot/iframes
 */
(function (global) {
  const STYLE_ID = "fourd-common-style";
  const VERSION = "1.1.0";

  const css = `
/* ───────── 4DReplay Common Theme (Dark) ───────── */
:root{
  --bg:#0b0f14; --fg:#eef2f7; --muted:#94a3b8;
  --card:#0b1220; --line:#243045; --line2:#1f2937;
  --btn:#1e40af; --btn2:#334155; --ok:#16a34a; --bad:#ef4444;
}

/* Ensure dark baseline even if page had its own styles */
html, body { height: 100% }
body { background: var(--bg) !important; color: var(--fg) !important; }

/* Typography */
body{
  font: 14px/1.5 -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
  margin: 24px;
}

h1,h2,h3,h4{font-weight:600}
h2{font-size:28px;margin:0 0 12px;display:flex;align-items:center;gap:10px}
h2 small{font-size:14px;opacity:.8}

/* Layout helpers */
.right{margin-left:auto}
.sep{height:1px;background:var(--line2);margin:12px 0}
.muted{color:var(--muted)}
.card{background:var(--card);border:1px solid var(--line2);border-radius:10px}
.container{max-width:1300px;margin:0 auto}
.toolbar{display:flex;gap:10px;align-items:center;margin:10px 0 14px}

/* Buttons */
button{background:var(--btn);color:#fff;border:0;border-radius:10px;padding:8px 12px;cursor:pointer;display:inline-flex;align-items:center;gap:8px;transition:filter .2s ease}
button:hover{filter:brightness(1.15)}
button[disabled]{opacity:.55;cursor:not-allowed}
.btn-ghost{background:transparent;border:1px solid var(--line);color:#e5e7eb}
.btn-secondary{background:var(--btn2)}
.btn-danger{background:var(--bad)}
.btn-success{background:var(--ok)}

/* Pills / badges */
.pill{display:inline-block;border:1px solid var(--line);background:#0f172a;border-radius:999px;font-size:12px;padding:2px 10px}
.pill-ok{border-color:var(--ok);color:#a7f3d0;background:#0d3b1a}
.pill-bad{border-color:var(--bad);color:#fecaca;background:#3b0d0d}

/* Icons */
.ico{width:16px;height:16px;display:inline-block;vertical-align:-2px;fill:currentColor}

/* Inputs */
input[type="text"], input[type="number"], textarea, select{
  width:100%; box-sizing:border-box; background:#0b1220; color:#e5e7eb;
  border:1px solid var(--line); border-radius:6px; padding:4px 6px;
  font:13px ui-monospace,monospace;
}
input::placeholder, textarea::placeholder{color:#9aa4b2}
textarea{height:36px; resize:none}

/* Tables */
table{border-collapse:collapse;width:100%;max-width:1300px}
th,td{border:1px solid var(--line2);padding:4px 6px;vertical-align:middle}
th{background:#0f172a;text-align:left;position:sticky;top:0;z-index:1}
tbody tr:nth-child(odd){background:#0c121b}

/* Column helpers */
.col-narrow{width:90px}
.col-btns{white-space:nowrap}

/* Panels */
.ghost-panel{background:#0c121b;border:1px solid var(--line2);border-radius:8px;padding:10px}

/* Forms grid */
.form-grid{display:grid;grid-template-columns:240px 1fr;gap:10px;align-items:center}

/* Links */
a{color:#93c5fd;text-decoration:none}
a:hover{text-decoration:underline}

/* Code */
code{background:#111827;border:1px solid var(--line);border-radius:6px;padding:2px 6px}
pre{background:#0b1220;border:1px solid var(--line2);border-radius:8px;padding:10px;overflow:auto}
`;

  function rootOf(target){
    if (!target) return document;
    if (target instanceof Document || target instanceof ShadowRoot) return target;
    if (target && target.getRootNode) return target.getRootNode();
    return document;
  }
  function hasStyle(where){
    return !!(where && where.querySelector && where.querySelector(`#${STYLE_ID}`));
  }
  function inject(target){
    const root = rootOf(target);
    const where = root === document ? document.head : root;
    if (!where) return false;
    if (hasStyle(where)) return true;
    const el = document.createElement('style');
    el.id = STYLE_ID; el.type = 'text/css';
    el.appendChild(document.createTextNode(css));
    where.appendChild(el);
    return true;
  }
  function remove(target){
    const root = rootOf(target);
    const where = root === document ? document.head : root;
    const el = where && where.querySelector && where.querySelector(`#${STYLE_ID}`);
    if (el && el.parentNode){ el.parentNode.removeChild(el); return true; }
    return false;
  }

  // Expose API
  const api = { inject, ensure: inject, remove, version: VERSION };
  if (typeof module !== 'undefined' && module.exports){ module.exports = api; } else { global.FourDCommon = api; }

  // Auto-inject on load (in case user forgets to call inject())
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function(){ try{ inject(); }catch(e){ console && console.warn('[4D] style inject failed', e); } });
  } else {
    try{ inject(); }catch(e){ console && console.warn('[4D] style inject failed', e); }
  }
})(typeof window !== 'undefined' ? window : globalThis);
