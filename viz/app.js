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
  const NEON = DATA.neon;               // {window: [{lat, lon, detected}]}
  const FRONTIER = DATA.frontier;       // [{window, corNorthernLimit, ...}]
  const FLINES = DATA.frontierLines || {}; // {window: [[lon, lat], ...]}
  const WINDOWS = META.windows;
  const [DOM_LO, DOM_HI] = META.colorDomain;
  const STOPS = META.colorStops;        // [[r,g,b], ...]
  const HEX_ALPHA = Math.round((META.hexOpacity || 0.78) * 255);

  // ---- application state ------------------------------------------------- //
  const state = {
    windowIndex: Math.max(0, WINDOWS.indexOf(META.defaultWindow)),
    surface: META.defaultSurface === "raw" ? "raw" : "shrunk",
    showNeon: false,
    showFrontier: false,
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

    const layers = [hexLayer];

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

  function tooltip({ object, layer }) {
    if (!object) return null;
    if (layer && layer.id === "neon") {
      return {
        html: object.detected
          ? "<b>NEON</b><br/>lone star detected (systematic sampling)"
          : "<b>NEON</b><br/>sampled here; no lone star found",
      };
    }
    const raw = (object.r * 100).toFixed(0);
    const cor = (object.s * 100).toFixed(0);
    return {
      html:
        `<b>${object.h}</b><br/>` +
        `raw share: ${raw}%<br/>` +
        `corrected share: ${cor}%`,
    };
  }

  function render() {
    deckOverlay.setProps({ layers: buildLayers(), getTooltip: tooltip });
    syncUI();
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

    document.getElementById("neon-legend").classList.toggle("hidden", !state.showNeon);
    document
      .getElementById("frontier-legend")
      .classList.toggle("hidden", !state.showFrontier);

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
      } else if (e.key.toLowerCase() === "b") {
        document.getElementById("view-band").click();
      }
    });
  }

  buildLegendBar();
  initControls();
  syncUI();
})();
