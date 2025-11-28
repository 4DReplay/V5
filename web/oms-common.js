/* oms-common.js
 * Shared utilities for 4DReplay OMS pages.
 * Loads common config, injects titles, favicon, and shared styles.
 */

(function () {

  window.OMS = window.OMS || {};
  OMS.config = null;

  window.UI_STATE_TITLE = {
    0 : "Unknown",          // unknown                       | 🟠"chip-orange",
    // SYSTEM
    10: "Check System",     // system/check                  | 🟠"chip-orange",
    11: "Need Restart",     // not on everything             | 🔵"chip-blue",
    12: "Restarting...",       // on restarting system       | 🔵"chip-blue", Text Aninmation
    13: "Need Connect",     // on everything + not connect   | 🟡"chip-yellow",
    14: "Connecting...",    // on connecting system          | 🟡"chip-yellow", Text Aninmation
    15: "Ready",            // ready (on+connected)          | 🟢"chip-green",
    // CAMERA
    20: "Check Camera",     // camera/check                  | 🟠"chip-orange",
    21: "Need Restart",     // not on everything             | 🔵"chip-blue",
    22: "Restarting...",       // on restarting camera       | 🔵"chip-blue", Text Aninmation
    23: "Need Connect",     // on everything + not connect   | 🟡"chip-yellow",   
    24: "Connecting...",    // on connecting camera          | 🟡"chip-yellow",   Text Aninmation 
    25: "Ready",            // ready (on+connected)          | 🟢"chip-green",  = 31
    26: "Recording",        // on recording                  | 🟢"chip-green", Text Aninmation
    27: "Recording Error",  // on recording                  | 🟠"chip-orange",Text Aninmation  
    // PRODUCTION
    30: "Check Camera",     // recording/check               | 🟠"chip-orange",
    31: "Need Recording",   // not on everything             | 🟡"chip-yellow",   = 25
    32: "Preparing...",     // on recording camera           | 🟡"chip-yellow",     
    33: "Product Ready",    // on everything + not connect   | 🟢"chip-green",  = 26
    34: "Producing...",     // on connecting camera          | 🔴"chip-red", Text Aninmation
    35: "Creating...",      // waiting until finish jon      | 🔴"chip-red",
  };

  window.UI_STATE_COLOR = {
    0 :"chip-orange",
    // SYSTEM
    10:"chip-orange",
    11:"chip-blue",
    12:"chip-blue",
    13:"chip-yellow",
    14:"chip-yellow",
    15:"chip-green",
    // CAMERA
    20:"chip-orange",
    21:"chip-blue",
    22:"chip-blue",
    23:"chip-yellow",   
    24:"chip-yellow",   
    25:"chip-green",  //= 31
    26:"chip-green",  //= 33
    27:"chip-orange",  
    // PRODUCTION
    30:"chip-orange",
    31:"chip-yellow",   //= 25
    32:"chip-yellow",    
    33:"chip-green",  //= 26
    34:"chip-red",
    35:"chip-red",
  };

  // === inside OMS.initAssets() === 
  OMS.initAssets = function () {
    const head = document.head;
    const cssList = [
      "/web/css/base.css",
    ];

    cssList.forEach(href => {
      const link = document.createElement("link");
      link.rel = "stylesheet";
      link.href = href + "?v=" + (window.__CACHE_VERSION__ || "1");
      head.appendChild(link);
    });
  };

  window.applyStateAndMessage = function (opts) {
    const { state, message, stateEl, messageEl } = opts;
    if (!stateEl || !messageEl) return;

    // state ship
    const label = UI_STATE_TITLE[state] || "Unknown";
    const color = UI_STATE_COLOR[state] || "chip-blue";

    stateEl.className = "chip " + color;
    stateEl.textContent = label;

    // message chip
    messageEl.textContent = message || "";
    messageEl.style.visibility = message ? "visible" : "hidden";
  };

  window.applyStateStyle = function(el, state) {
      if (!el) return;

      // --- 상태 텍스트 기본 클래스 초기화 ---
      el.classList.remove(
          "status-ready",
          "status-warn",
          "status-init",
          "status-danger",
          "status-work",
          "state-anim"
      );

      // --- 상태 텍스트 내용 적용 ---
      el.textContent = window.UI_STATE_TITLE[state] || "Unknown";
      const colorClass = window.UI_STATE_COLOR[state];

      // 상태 색상 class 적용
      if (colorClass === "chip-green")    el.classList.add("status-ready");
      if (colorClass === "chip-blue")     el.classList.add("status-warn");
      if (colorClass === "chip-yellow")   el.classList.add("status-init");
      if (colorClass === "chip-orange")   el.classList.add("status-danger");
      if (colorClass === "chip-red")      el.classList.add("status-work");
      

      // --- Text Aninmation
      if (
        state === 12 ||  // system restarting          | "chip-blue",    // blink
        state === 14 ||  // system connecting          | "chip-yellow",  // blink
        state === 22 ||  // camera restarting          | "chip-blue",    // blink
        state === 24 ||  // camera connecting          | "chip-yellow",  // blink
        state === 26 ||  // camera recording           | "chip-green",   // blink
        state === 27 ||  // camera recording error     | "chip-orange",  // blink
        state === 34     // "Producing...",            | "chip-red",     // blink
      )
      {
          el.classList.add("state-anim");
      }

      // --- 메시지 칩 선택 ---
      const msgEl =
          (el.id === "sysReady") ? $("sysMessageChip") :
          (el.id === "camReady") ? $("camMessageChip") :
          null;

      if (!msgEl) return;

      // ----------------------------------------------------
      //  메시지 칩은 state와 상관없이 항상 동일한 색 유지
      //
      //  → message-chip 관련 class 조작 제거 (추가/삭제 모두 X)
      // ----------------------------------------------------

      // 아무 것도 하지 않음. = 항상 기본 스타일 유지
  };

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
    const baseName = titleCfg.Name || "4DReplay";
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
      PROXY + "/web/css/base.css",
      "./base.css",
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
