"use client";

import { Responsive, WidthProvider, type Layouts } from "react-grid-layout";
import { AlertsPanel } from "./panels/AlertsPanel";
import { BriefPanel } from "./panels/BriefPanel";
import { CalendarPanel } from "./panels/CalendarPanel";
import { CorrelationPanel } from "./panels/CorrelationPanel";
import { HeartbeatPanel } from "./panels/HeartbeatPanel";
import { MacroPanel } from "./panels/MacroPanel";
import { MultiAssetPanel } from "./panels/MultiAssetPanel";
import { NewsPanel } from "./panels/NewsPanel";
import { PositioningPanel } from "./panels/PositioningPanel";
import { RetailPanel } from "./panels/RetailPanel";
import { RotationPanel } from "./panels/RotationPanel";
import { WatchlistPanel } from "./panels/WatchlistPanel";

const Grid = WidthProvider(Responsive);

// The six target panels from PLAN.md §3, the Phase-0 system panel, and the
// Phase-7 edge layer (brief, alerts, calendar, rotation, positioning).
const PANELS: { i: string; el: React.ReactNode }[] = [
  { i: "system", el: <HeartbeatPanel /> },
  { i: "news", el: <NewsPanel /> },
  { i: "liquidity", el: <MacroPanel /> },
  { i: "watchlist", el: <WatchlistPanel /> },
  { i: "multiasset", el: <MultiAssetPanel /> },
  { i: "retail", el: <RetailPanel /> },
  { i: "cookbook", el: <CorrelationPanel /> },
  { i: "brief", el: <BriefPanel /> },
  { i: "alerts", el: <AlertsPanel /> },
  { i: "calendar", el: <CalendarPanel /> },
  { i: "rotation", el: <RotationPanel /> },
  { i: "positioning", el: <PositioningPanel /> },
];

const layouts: Layouts = {
  lg: [
    // The daily-driver row first: brief + alerts + event horizon.
    { i: "brief", x: 0, y: 0, w: 4, h: 6 },
    { i: "alerts", x: 4, y: 0, w: 4, h: 6 },
    { i: "calendar", x: 8, y: 0, w: 4, h: 6 },
    { i: "news", x: 0, y: 6, w: 4, h: 6 },
    { i: "retail", x: 4, y: 6, w: 4, h: 6 },
    { i: "liquidity", x: 8, y: 6, w: 4, h: 6 },
    { i: "watchlist", x: 0, y: 12, w: 4, h: 6 },
    { i: "multiasset", x: 4, y: 12, w: 4, h: 6 },
    { i: "positioning", x: 8, y: 12, w: 4, h: 6 },
    { i: "rotation", x: 0, y: 18, w: 6, h: 8 },
    { i: "cookbook", x: 6, y: 18, w: 6, h: 8 },
    { i: "system", x: 0, y: 26, w: 4, h: 6 },
  ],
};

export function DashboardGrid() {
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
      {PANELS.map((p) => (
        <div key={p.i}>{p.el}</div>
      ))}
    </Grid>
  );
}
