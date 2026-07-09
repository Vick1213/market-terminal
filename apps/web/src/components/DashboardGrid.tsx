"use client";

import { Responsive, WidthProvider, type Layouts } from "react-grid-layout";
import { PANEL_REGISTRY } from "./panelRegistry";
import { usePopouts } from "@/hooks/usePopouts";

const Grid = WidthProvider(Responsive);

const layouts: Layouts = {
  lg: [
    // Phase 14: the big portfolio value panel leads — full-width, attribution-aware.
    { i: "portfolio", x: 0, y: 0, w: 12, h: 9 },
    // The daily-driver row: brief + alerts + event horizon.
    { i: "brief", x: 0, y: 9, w: 4, h: 6 },
    { i: "alerts", x: 4, y: 9, w: 4, h: 6 },
    { i: "calendar", x: 8, y: 9, w: 4, h: 6 },
    { i: "news", x: 0, y: 15, w: 4, h: 6 },
    { i: "retail", x: 4, y: 15, w: 4, h: 6 },
    { i: "liquidity", x: 8, y: 15, w: 4, h: 6 },
    { i: "watchlist", x: 0, y: 21, w: 4, h: 6 },
    { i: "multiasset", x: 4, y: 21, w: 4, h: 6 },
    { i: "positioning", x: 8, y: 21, w: 4, h: 6 },
    { i: "rotation", x: 0, y: 27, w: 6, h: 8 },
    { i: "cookbook", x: 6, y: 27, w: 6, h: 8 },
    { i: "whales", x: 0, y: 35, w: 4, h: 8 },
    { i: "strategist", x: 4, y: 35, w: 4, h: 8 },
    // §12 follow-through: vol-target overlay sits beside the strategist it feeds.
    { i: "voloverlay", x: 8, y: 35, w: 4, h: 8 },
    // Phase 15: narrative-vs-money divergence + Polymarket front-running.
    { i: "divergence", x: 0, y: 43, w: 12, h: 10 },
    // Phase 16 §11 ranks 12+13: the two EDGAR corporate-event scanners, side by side.
    { i: "edgar8k", x: 0, y: 53, w: 6, h: 10 },
    { i: "edgar13d", x: 6, y: 53, w: 6, h: 10 },
    // Phase 16 §11 remaining frontend: macro/positioning surface.
    // rank 6 (TFF) + rank 7 (Treasury supply).
    { i: "tff", x: 0, y: 63, w: 6, h: 9 },
    { i: "treasury", x: 6, y: 63, w: 6, h: 9 },
    // rank 8 (Kalshi odds) + rank 9 (Rates & Inflation nowcast).
    { i: "kalshi", x: 0, y: 72, w: 6, h: 9 },
    { i: "nowcast", x: 6, y: 72, w: 6, h: 9 },
    // rank 10 (Energy fundamentals) — full-width 3-section.
    { i: "energy", x: 0, y: 81, w: 12, h: 8 },
    // rank 11 (Policy-risk surface + Fed-speech hawk/dove).
    { i: "policyrisk", x: 0, y: 89, w: 6, h: 8 },
    { i: "fedspeak", x: 6, y: 89, w: 6, h: 9 },
    // Phase 12/13: paper bots — full-width (optimizer split + swing proposals + day sleeve).
    { i: "bot", x: 0, y: 98, w: 12, h: 12 },
    // Tradebook: paired entry/exit trades by sleeve + the learning loop.
    { i: "tradebook", x: 0, y: 110, w: 12, h: 11 },
    { i: "system", x: 0, y: 121, w: 4, h: 6 },
    { i: "sources", x: 4, y: 121, w: 4, h: 6 },
    { i: "help", x: 8, y: 121, w: 4, h: 6 },
  ],
};

export function DashboardGrid() {
  const { isPoppedOut, openPopout, bringBack } = usePopouts();

  return (
    <Grid
      className="layout"
      layouts={layouts}
      breakpoints={{ lg: 1200, md: 996, sm: 768, xs: 480, xxs: 0 }}
      cols={{ lg: 12, md: 8, sm: 4, xs: 2, xxs: 1 }}
      rowHeight={48}
      margin={[10, 10]}
      draggableHandle=".panel-head"
      draggableCancel="button, a, select, input"
    >
      {PANEL_REGISTRY.map((p) => (
        <div key={p.id} className="grid-tile">
          {isPoppedOut(p.id) ? (
            <div className="panel">
              <div className="panel-head">
                <span>{p.title}</span>
              </div>
              <div className="panel-body placeholder">
                <span>Popped out</span>
                <button className="expand-btn" onClick={() => bringBack(p.id)}>
                  Bring back
                </button>
              </div>
            </div>
          ) : (
            <>
              <button
                className="popout-btn"
                title={`Pop out ${p.title}`}
                aria-label={`Pop out ${p.title}`}
                onMouseDown={(e) => e.stopPropagation()}
                onTouchStart={(e) => e.stopPropagation()}
                onClick={() => openPopout(p.id)}
              >
                ⧉
              </button>
              {p.render()}
            </>
          )}
        </div>
      ))}
    </Grid>
  );
}
