/* oms-common.js
 * Shared utilities for 4DReplay OMS pages.
 * Loads common config, injects titles, favicon, and shared styles.
 */

(function () {

  window.OMS = window.OMS || {};
  OMS.config = null;

  // -----------------------------------------
  // Helper: prefix for proxy-safe path
  // -----------------------------------------
  OMS.prefix = function (path) {
    const m = location.pathname.match(/^\/proxy\/([^/]+)/);
    const p = m ? `/proxy/${encodeURIComponent(m[1])}` : "";
    return p + path;
  };

  // -----------------------------------------
  // Load /web/config/user-config.json
  // -----------------------------------------
  OMS.loadCommonJSON = async function () {
    if (OMS.config) return OMS.config;

    try {
      const res = await fetch( OMS.prefix("/web/config/user-config.json") );
      OMS.config = await res.json();
      return OMS.config;
    } catch (e) {
      console.warn("[OMS] Failed to load user-config.json", e);
      OMS.config = {};
      return OMS.config;
    }
  };

  // -----------------------------------------
  // Apply Page Title & Favicon & H1 Title
  // pageName  → visible sub-page name (e.g., "System")
  // -----------------------------------------
  OMS.applyPageConfig = async function (pageName) {
    const cfg = await OMS.loadCommonJSON();

    const titleCfg = cfg.title || {};
    const baseName = titleCfg.targetName || "4DReplay";
    const iconPath = titleCfg.icon ? OMS.prefix(titleCfg.icon) : null;

    // Final HTML title
    const finalTitle = `${baseName} - ${pageName}`;
    document.title = finalTitle;

    // Apply favicon
    if (iconPath) {
      let link = document.querySelector("link[rel='icon']");
      if (!link) {
        link = document.createElement("link");
        link.rel = "icon";
        document.head.appendChild(link);
      }
      link.href = iconPath;
    }

    // Apply visible <h1 id="pageTitle">
    const h1 = document.getElementById("pageTitle");
    if (h1) {
      h1.textContent = finalTitle;

      // insert icon if not exists
      if (iconPath && !h1.querySelector(".title-icon")) {
        const img = document.createElement("img");
        img.src = iconPath;
        img.className = "title-icon";
        img.style.height = "1em";
        img.style.width = "auto";
        img.style.verticalAlign = "-0.15em";
        img.style.marginRight = "8px";
        h1.prepend(img);
      }
    }
  };

  // -----------------------------------------
  // Inject shared common CSS/JS (fallback + proxy-safe)
  // -----------------------------------------
  OMS.initAssets = function () {
    const bust = "?v=" + Date.now();

    const m = location.pathname.match(/^\/proxy\/([^/]+)/);
    const PROXY = m ? "/proxy/" + encodeURIComponent(m[1]) : "";

    const CSS_CANDIDATES = [
      PROXY + "/web/4d-common-style.css",
      "./4d-common-style.css",
    ];

    const JS_CANDIDATES = [
      PROXY + "/web/4d-common-style.js",
      "./4d-common-style.js",
    ];

    function injectCSS(list, i = 0) {
      if (i >= list.length) return;
      const el = document.createElement("link");
      el.rel = "stylesheet";
      el.href = list[i] + bust;
      el.onerror = () => {
        console.warn("[OMS] CSS load failed:", list[i]);
        injectCSS(list, i + 1);
      };
      document.head.appendChild(el);
    }

    function injectJS(list, i = 0) {
      if (i >= list.length) return;
      const el = document.createElement("script");
      el.defer = true;
      el.src = list[i] + bust;
      el.onerror = () => {
        console.warn("[OMS] JS load failed:", list[i]);
        injectJS(list, i + 1);
      };
      document.head.appendChild(el);
    }

    injectCSS(CSS_CANDIDATES);
    injectJS(JS_CANDIDATES);
  };

  // -----------------------------------------
  // Auto-load oms-actions.js
  // -----------------------------------------
  OMS.initActions = function () {
    const bust = "?v=" + Date.now();

    const m = location.pathname.match(/^\/proxy\/([^/]+)/);
    const PROXY = m ? "/proxy/" + encodeURIComponent(m[1]) : "";

    const ACTIONS = [
      PROXY + "/web/oms-actions.js",
      "./web/oms-actions.js",
      "./oms-actions.js",
      "/web/oms-actions.js"
    ];

    function load(list, i = 0) {
      if (i >= list.length) return;
      const s = document.createElement("script");
      s.defer = true;
      s.src = list[i] + bust;
      s.onerror = () => {
        console.warn("[OMS] actions load miss:", list[i]);
        load(list, i + 1);
      };
      document.head.appendChild(s);
    }

    load(ACTIONS);
  };

})();
