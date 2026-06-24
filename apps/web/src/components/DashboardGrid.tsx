"use client";

import { Responsive, WidthProvider, type Layouts } from "react-grid-layout";
import { AlertsPanel } from "./panels/AlertsPanel";
import { BotPanel } from "./panels/BotPanel";
import { BriefPanel } from "./panels/BriefPanel";
import { CalendarPanel } from "./panels/CalendarPanel";
import { CorrelationPanel } from "./panels/CorrelationPanel";
import { HeartbeatPanel } from "./panels/HeartbeatPanel";
import { HelpPanel } from "./panels/HelpPanel";
import { MacroPanel } from "./panels/MacroPanel";
import { MultiAssetPanel } from "./panels/MultiAssetPanel";
import { NewsPanel } from "./panels/NewsPanel";
import { PositioningPanel } from "./panels/PositioningPanel";
import { RetailPanel } from "./panels/RetailPanel";
import { RotationPanel } from "./panels/RotationPanel";
import { SourceHealthPanel } from "./panels/SourceHealthPanel";
import { StrategistPanel } from "./panels/StrategistPanel";
import { WatchlistPanel } from "./panels/WatchlistPanel";
import { WhalesPanel } from "./panels/WhalesPanel";

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
  { i: "whales", el: <WhalesPanel /> },
  { i: "strategist", el: <StrategistPanel /> },
  { i: "bot", el: <BotPanel /> },
  { i: "sources", el: <SourceHealthPanel /> },
  { i: "help", el: <HelpPanel /> },
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
    { i: "whales", x: 0, y: 26, w: 6, h: 8 },
    { i: "strategist", x: 6, y: 26, w: 6, h: 8 },
    // Phase 12/13: paper bots — full-width (optimizer split + swing proposals + day sleeve).
    { i: "bot", x: 0, y: 34, w: 12, h: 12 },
    { i: "system", x: 0, y: 46, w: 4, h: 6 },
    { i: "sources", x: 4, y: 46, w: 4, h: 6 },
    { i: "help", x: 8, y: 46, w: 4, h: 6 },
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
