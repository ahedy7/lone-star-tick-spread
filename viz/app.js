/* Lone star tick -- interactive effort-corrected frontier map.
 *
 * Renders the Stage 4 web export (window.LST_DATA, from data/bundle.js) as a
 * deck.gl H3HexagonLayer choropleth over a CARTO Positron basemap (MapLibre),
 * with: a play/animate sweep over the rolling windows, the RAW <-> CORRECTED
 * surface toggle (the headline interaction), and an optional NEON overlay.
 *
 * No build step, no server: open index.html directly. The colour ramp stops
 * are exported from matplotlib so the interactive map and the static figures
 * use a pixel-identical scale.
 */
(function () {
  "use strict";

  const DATA = window.LST_DATA;
  if (!DATA) {
    document.getElementById("loading").textContent =
      "Could not load data/bundle.js. Re-run: python src/stage4.py --only web";
    return;
  }

  const META = DATA.cells.meta;
  const CELLS = DATA.cells.windows;     // {window: [{h, r, s}]}
  // Data-vintage stamp. The bundle always carries it (works from file://); when
  // served over http we also try the canonical data/meta.json (which the monthly
  // workflow rewrites) and let it override, so the deployed stamp is never stale.
  let VINTAGE = META.vintage || null;
  const NEON = DATA.neon;               // {window: [{lat, lon, detected}]}
  const FRONTIER = DATA.frontier;       // [{window, corNorthernLimit, ...}]
  const FLINES = DATA.frontierLines || {}; // {window: [[lon, lat], ...]}

  // Stage 5 CDC validation layer (optional; null if validation.js not present).
  // A county FeatureCollection tagged with category/established/detected, plus a
  // meta.categoryColors palette exported straight from config.py.
  const VALID = window.LST_VALIDATION || null;
  const VAL_COLORS = (VALID && VALID.meta && VALID.meta.categoryColors) || {};
  // CDC-established counties (confirmed + blind spots) for the footprint shading,
  // and our leading-edge counties (detected, not CDC-established) for the
  // highlight. Filtered once up front.
  const CDC_FEATURES = VALID
    ? { type: "FeatureCollection",
        features: VALID.features.filter((f) => f.properties.established) }
    : null;
  const LEADING_FEATURES = VALID
    ? { type: "FeatureCollection",
        features: VALID.features.filter(
          (f) =>
            f.properties.category === "leading_edge_no_records" ||
            f.properties.category === "leading_edge_reported"
        ) }
    : null;

  function hexToRgb(hex) {
    const h = (hex || "#888888").replace("#", "");
    return [
      parseInt(h.slice(0, 2), 16),
      parseInt(h.slice(2, 4), 16),
      parseInt(h.slice(4, 6), 16),
    ];
  }
  // Steel-blue wash for the official footprint (sits UNDER the magma hexes).
  const CDC_FILL = [70, 130, 180];
  const WINDOWS = META.windows;
  const [DOM_LO, DOM_HI] = META.colorDomain;
  const STOPS = META.colorStops;        // [[r,g,b], ...]
  const HEX_ALPHA = Math.round((META.hexOpacity || 0.78) * 255);
  // Prior-bleed treatment for the CORRECTED surface: a cell with no lone star
  // signal (k === 0) and too few total tick sightings (n < MIN_OBS) shows the
  // shrinkage floor, not real ticks, so it is washed neutral grey instead of a
  // faint color. Cells with any lone star signal are always colored.
  const MIN_OBS = META.minObsForCorrected || 5;
  const INSUFF_RGB = hexToRgb(META.insufficientColor || "#cfcfcf");
  const INSUFF_ALPHA = Math.round(
    (META.insufficientOpacity != null ? META.insufficientOpacity : 0.45) * 255
  );

  // ---- application state ------------------------------------------------- //
  const state = {
    windowIndex: Math.max(0, WINDOWS.indexOf(META.defaultWindow)),
    surface: META.defaultSurface === "raw" ? "raw" : "shrunk",
    showNeon: false,
    showFrontier: false,
    showCdc: false,
    showLeading: false,
    playing: false,
  };
  const frontierByWindow = Object.fromEntries(
    FRONTIER.map((r) => [r.window, r])
  );

  // ---- colour ramp (matches matplotlib stops exactly) -------------------- //
  function rampColor(v) {
    let t = (v - DOM_LO) / (DOM_HI - DOM_LO);
    t = t < 0 ? 0 : t > 1 ? 1 : t;
    const n = STOPS.length - 1;
    const f = t * n;
    const i = Math.floor(f);
    const j = Math.min(i + 1, n);
    const frac = f - i;
    const a = STOPS[i];
    const b = STOPS[j];
    return [
      Math.round(a[0] + (b[0] - a[0]) * frac),
      Math.round(a[1] + (b[1] - a[1]) * frac),
      Math.round(a[2] + (b[2] - a[2]) * frac),
    ];
  }

  // ---- data-vintage stamp ------------------------------------------------ //
  function paintVintage() {
    const v = VINTAGE || {};
    const valEl = document.getElementById("vintage-value");
    const subEl = document.getElementById("vintage-sub");
    if (valEl) valEl.textContent = v.dataVintage || "—";
    if (subEl) {
      const last = v.lastUpdated ? ` · updated ${v.lastUpdated}` : "";
      subEl.textContent = `citizen-science frontier, refreshed monthly${last}`;
    }
  }
  function loadVintage() {
    paintVintage(); // immediate, from the embedded bundle copy (no flash)
    // Relative path so it resolves under the GitHub Pages subpath; fails
    // silently from file:// (browsers block fetch of siblings there).
    fetch("data/meta.json", { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : null))
      .then((m) => {
        if (m && m.dataVintage) {
          VINTAGE = m;
          paintVintage();
        }
      })
      .catch(() => {});
  }

  function buildLegendBar() {
    const stripes = STOPS.map((c, i) => {
      const pct = Math.round((i / (STOPS.length - 1)) * 100);
      return `rgb(${c[0]},${c[1]},${c[2]}) ${pct}%`;
    });
    document.getElementById("legend-bar").style.background =
      `linear-gradient(to right, ${stripes.join(",")})`;
    document.getElementById("legend-title").textContent = META.rateLabel;
  }

  // ---- deck.gl over MapLibre -------------------------------------------- //
  const map = new maplibregl.Map({
    container: "map",
    style: META.basemapStyleUrl,
    center: [META.initialViewState.longitude, META.initialViewState.latitude],
    zoom: META.initialViewState.zoom,
    attributionControl: true,
  });
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "bottom-right");

  const deckOverlay = new deck.MapboxOverlay({ interleaved: false, layers: [] });
  map.addControl(deckOverlay);

  map.on("load", () => {
    const el = document.getElementById("loading");
    if (el) el.remove();
    render();
  });

  // ---- layer construction ----------------------------------------------- //
  function buildLayers() {
    const win = WINDOWS[state.windowIndex];
    const records = CELLS[win] || [];
    const valueOf = state.surface === "raw" ? (d) => d.r : (d) => d.s;

    const hexLayer = new deck.H3HexagonLayer({
      id: "hex",
      data: records,
      getHexagon: (d) => d.h,
      getFillColor: (d) => {
        if (state.surface !== "raw" && d.k === 0 && d.n < MIN_OBS) {
          return [INSUFF_RGB[0], INSUFF_RGB[1], INSUFF_RGB[2], INSUFF_ALPHA];
        }
        const c = rampColor(valueOf(d));
        return [c[0], c[1], c[2], HEX_ALPHA];
      },
      extruded: false,
      filled: true,
      stroked: false,
      pickable: true,
      highPrecision: true,
      updateTriggers: { getFillColor: [state.surface, win] },
      transitions: { getFillColor: 250 },
    });

    // CDC established footprint sits UNDER the hexes (a wash, not a cover), so
    // the corrected surface still reads on top of the official footprint.
    const layers = [];
    if (state.showCdc && CDC_FEATURES) {
      layers.push(
        new deck.GeoJsonLayer({
          id: "cdc-established",
          data: CDC_FEATURES,
          stroked: true,
          filled: true,
          getFillColor: [CDC_FILL[0], CDC_FILL[1], CDC_FILL[2], 70],
          getLineColor: [CDC_FILL[0], CDC_FILL[1], CDC_FILL[2], 150],
          lineWidthMinPixels: 0.4,
          pickable: true,
        })
      );
    }

    layers.push(hexLayer);

    // Our leading edge sits ABOVE the hexes so frontier counties pop against the
    // official footprint -- coloured by CDC sub-status (no-records vs reported).
    if (state.showLeading && LEADING_FEATURES) {
      layers.push(
        new deck.GeoJsonLayer({
          id: "leading-edge",
          data: LEADING_FEATURES,
          stroked: true,
          filled: true,
          getFillColor: (f) => {
            const c = hexToRgb(VAL_COLORS[f.properties.category]);
            return [c[0], c[1], c[2], 70];
          },
          getLineColor: (f) => {
            const c = hexToRgb(VAL_COLORS[f.properties.category]);
            return [c[0], c[1], c[2], 255];
          },
          lineWidthMinPixels: 1.6,
          pickable: true,
        })
      );
    }

    if (state.showFrontier) {
      const pts = (FLINES[win] || []).map((p) => [p[0], p[1]]);
      if (pts.length > 1) {
        const lineData = [{ path: pts }];
        // White halo under a crimson edge, so the line reads on dark cells.
        layers.push(
          new deck.PathLayer({
            id: "frontier-halo",
            data: lineData,
            getPath: (d) => d.path,
            getColor: [255, 255, 255, 220],
            getWidth: 5,
            widthUnits: "pixels",
            widthMinPixels: 5,
            jointRounded: true,
            capRounded: true,
            parameters: { depthTest: false },
            updateTriggers: { getPath: [win] },
          })
        );
        layers.push(
          new deck.PathLayer({
            id: "frontier-line",
            data: lineData,
            getPath: (d) => d.path,
            getColor: [179, 18, 63, 255],
            getWidth: 2.6,
            widthUnits: "pixels",
            widthMinPixels: 2.6,
            jointRounded: true,
            capRounded: true,
            parameters: { depthTest: false },
            updateTriggers: { getPath: [win] },
          })
        );
      }
    }

    if (state.showNeon) {
      const neonRecs = NEON[win] || [];
      layers.push(
        new deck.ScatterplotLayer({
          id: "neon",
          data: neonRecs,
          getPosition: (d) => [d.lon, d.lat],
          getRadius: (d) => (d.detected ? 7000 : 4500),
          radiusMinPixels: 2.5,
          radiusMaxPixels: 9,
          stroked: true,
          filled: true,
          lineWidthMinPixels: 1.4,
          getFillColor: (d) =>
            d.detected ? [214, 39, 40, 230] : [255, 255, 255, 30],
          getLineColor: (d) =>
            d.detected ? [120, 10, 10, 255] : [110, 110, 110, 220],
          pickable: true,
          updateTriggers: { getRadius: [win], getFillColor: [win] },
        })
      );
    }
    return layers;
  }

  function vintageFooter() {
    const v = VINTAGE || {};
    return v.dataVintage
      ? `<div class="tip-meta">data vintage ${v.dataVintage}</div>`
      : "";
  }

  function tooltip({ object, layer }) {
    if (!object) return null;

    if (layer && (layer.id === "cdc-established" || layer.id === "leading-edge")) {
      const p = object.properties || {};
      const det = p.detected
        ? `detected by us (peak ${p.peak_window_obs} obs; last ${p.last_detected_window || "—"})`
        : "not detected by us";
      return {
        html:
          `<div class="lst-tip">` +
          `<div class="tip-title">${p.county || ""}, ${p.state || ""}</div>` +
          `<div class="tip-row">${p.category_label || p.category || ""}</div>` +
          `<div class="tip-row">CDC status: <span class="tip-val">${p.cdc_status}</span></div>` +
          `<div class="tip-row">${det}</div>` +
          `<div class="tip-meta">CDC establishment is an annual vintage (${
            (VINTAGE && VINTAGE.cdcVintage) || "—"
          }), not live</div>` +
          `</div>`,
      };
    }

    if (layer && layer.id === "neon") {
      return {
        html:
          `<div class="lst-tip">` +
          `<div class="tip-title">NEON · systematic sampling</div>` +
          `<div class="tip-row">${
            object.detected
              ? 'lone star <span class="tip-accent">detected</span> here'
              : "sampled here; no lone star found"
          }</div>` +
          `<div class="tip-meta">independent structured anchor (drag-cloth survey)</div>` +
          `</div>`,
      };
    }

    // Hex cell: lead with the plain-language reading of the active surface.
    const win = WINDOWS[state.windowIndex];
    const isRaw = state.surface === "raw";
    const active = isRaw ? object.r : object.s;
    const activeName = isRaw ? "raw" : "effort-corrected";
    return {
      html:
        `<div class="lst-tip">` +
        `<div class="tip-title">Share that were lone star</div>` +
        `<div class="tip-row">${activeName}: ` +
        `<span class="tip-val tip-accent">${active.toFixed(2)}</span> ` +
        `of tick observations here</div>` +
        `<div class="tip-row">window <span class="tip-val">${win}</span></div>` +
        `<div class="tip-row" style="opacity:.8">raw ${object.r.toFixed(
          2
        )} · corrected ${object.s.toFixed(2)}</div>` +
        vintageFooter() +
        `</div>`,
    };
  }

  function render() {
    deckOverlay.setProps({ layers: buildLayers(), getTooltip: tooltip });
    syncUI();
    // Graceful empty state: a window with no cells should never read as a broken
    // map (this also covers a guarded-out refresh that shipped a thin payload).
    const hasCells = (CELLS[WINDOWS[state.windowIndex]] || []).length > 0;
    const empty = document.getElementById("empty-state");
    if (empty) empty.classList.toggle("hidden", hasCells);
  }

  // ---- UI sync ----------------------------------------------------------- //
  function syncUI() {
    const win = WINDOWS[state.windowIndex];
    document.getElementById("window-label").textContent = win;
    document.getElementById("slider").value = String(state.windowIndex);

    document.getElementById("btn-raw").classList.toggle("active", state.surface === "raw");
    document
      .getElementById("btn-shrunk")
      .classList.toggle("active", state.surface === "shrunk");

    // The "too few sightings" wash only exists on the corrected surface.
    document
      .getElementById("insufficient-legend")
      .classList.toggle("hidden", state.surface === "raw");

    document.getElementById("neon-legend").classList.toggle("hidden", !state.showNeon);
    document
      .getElementById("frontier-legend")
      .classList.toggle("hidden", !state.showFrontier);
    document.getElementById("cdc-legend").classList.toggle("hidden", !state.showCdc);
    document
      .getElementById("leading-legend")
      .classList.toggle("hidden", !state.showLeading);

    const f = frontierByWindow[win];
    if (f) {
      const nl = state.surface === "raw" ? f.rawNorthernLimit : f.corNorthernLimit;
      const occ = state.surface === "raw" ? f.rawOccupied : f.corOccupied;
      const surfaceName = state.surface === "raw" ? "raw" : "corrected";
      document.getElementById("stat-readout").textContent =
        `${surfaceName}: northern limit ${nl.toFixed(2)}\u00B0N \u00B7 ${occ} cells`;
    }

    const playBtn = document.getElementById("play");
    playBtn.innerHTML = state.playing ? "&#10073;&#10073;" : "&#9658;";
  }

  // ---- animation --------------------------------------------------------- //
  let timer = null;
  function play() {
    if (state.playing) return;
    state.playing = true;
    syncUI();
    timer = setInterval(() => {
      state.windowIndex = (state.windowIndex + 1) % WINDOWS.length;
      render();
    }, META.animMsPerWindow || 900);
  }
  function pause() {
    state.playing = false;
    if (timer) clearInterval(timer);
    timer = null;
    syncUI();
  }
  function togglePlay() {
    state.playing ? pause() : play();
  }

  // ---- wire controls ----------------------------------------------------- //
  function initControls() {
    const slider = document.getElementById("slider");
    slider.max = String(WINDOWS.length - 1);
    slider.value = String(state.windowIndex);
    slider.addEventListener("input", (e) => {
      pause();
      state.windowIndex = Number(e.target.value);
      render();
    });

    document.getElementById("play").addEventListener("click", togglePlay);

    document.querySelectorAll(".seg-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.surface = btn.dataset.surface;
        render();
      });
    });

    document.getElementById("neon-toggle").addEventListener("change", (e) => {
      state.showNeon = e.target.checked;
      render();
    });

    document.getElementById("frontier-toggle").addEventListener("change", (e) => {
      state.showFrontier = e.target.checked;
      render();
    });

    const cdcToggle = document.getElementById("cdc-toggle");
    const leadingToggle = document.getElementById("leading-toggle");
    // Disable the validation toggles if validation.js was not loaded.
    if (!VALID) {
      [cdcToggle, leadingToggle].forEach((el) => {
        el.disabled = true;
        el.closest(".chk").title = "Run: python src/stage5.py";
      });
    }
    cdcToggle.addEventListener("change", (e) => {
      state.showCdc = e.target.checked;
      render();
    });
    leadingToggle.addEventListener("change", (e) => {
      state.showLeading = e.target.checked;
      render();
    });

    const band = META.frontierBand;
    document.getElementById("view-band").addEventListener("click", () => {
      if (!band) return;
      map.fitBounds(
        [
          [band.lonMin, band.latMin],
          [band.lonMax, band.latMax],
        ],
        { padding: 50, duration: 1200 }
      );
    });
    document.getElementById("view-usa").addEventListener("click", () => {
      map.flyTo({
        center: [META.initialViewState.longitude, META.initialViewState.latitude],
        zoom: META.initialViewState.zoom,
        duration: 1200,
      });
    });

    document.addEventListener("keydown", (e) => {
      if (e.key === " ") {
        e.preventDefault();
        togglePlay();
      } else if (e.key === "ArrowRight") {
        pause();
        state.windowIndex = Math.min(state.windowIndex + 1, WINDOWS.length - 1);
        render();
      } else if (e.key === "ArrowLeft") {
        pause();
        state.windowIndex = Math.max(state.windowIndex - 1, 0);
        render();
      } else if (e.key.toLowerCase() === "r") {
        state.surface = state.surface === "raw" ? "shrunk" : "raw";
        render();
      } else if (e.key.toLowerCase() === "f") {
        state.showFrontier = !state.showFrontier;
        document.getElementById("frontier-toggle").checked = state.showFrontier;
        render();
      } else if (e.key.toLowerCase() === "c" && VALID) {
        state.showCdc = !state.showCdc;
        document.getElementById("cdc-toggle").checked = state.showCdc;
        render();
      } else if (e.key.toLowerCase() === "e" && VALID) {
        state.showLeading = !state.showLeading;
        document.getElementById("leading-toggle").checked = state.showLeading;
        render();
      } else if (e.key.toLowerCase() === "b") {
        document.getElementById("view-band").click();
      }
    });
  }

  buildLegendBar();
  loadVintage();
  initControls();
  syncUI();
})();
