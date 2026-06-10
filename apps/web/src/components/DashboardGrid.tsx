"use client";

import { Responsive, WidthProvider, type Layouts } from "react-grid-layout";
import { HeartbeatPanel } from "./panels/HeartbeatPanel";
import { MacroPanel } from "./panels/MacroPanel";
import { NewsPanel } from "./panels/NewsPanel";
import { PlaceholderPanel } from "./panels/PlaceholderPanel";

const Grid = WidthProvider(Responsive);

// The six target panels from PLAN.md §3 plus the Phase-0 system panel.
const PANELS: { i: string; el: React.ReactNode }[] = [
  { i: "system", el: <HeartbeatPanel /> },
  { i: "news", el: <NewsPanel /> },
  { i: "liquidity", el: <MacroPanel /> },
  { i: "watchlist", el: <PlaceholderPanel title="Custom Watchlist" phase="P3" note="Per-symbol quotes + badges" /> },
  { i: "multiasset", el: <PlaceholderPanel title="Multi-Asset Liquidity & Major Trades" phase="P4" note="Live crypto prints · metals · proxies" /> },
  { i: "retail", el: <PlaceholderPanel title="Retail Market Score" phase="P5" note="ApeWisdom · Bluesky · StockTwits" /> },
  { i: "cookbook", el: <PlaceholderPanel title="Correlation Cookbook" phase="P6" note="12 intermarket rule-cards" /> },
];

const layouts: Layouts = {
  lg: [
    { i: "system", x: 0, y: 0, w: 4, h: 6 },
    { i: "news", x: 4, y: 0, w: 4, h: 6 },
    { i: "retail", x: 8, y: 0, w: 4, h: 6 },
    { i: "liquidity", x: 0, y: 6, w: 4, h: 6 },
    { i: "watchlist", x: 4, y: 6, w: 4, h: 6 },
    { i: "multiasset", x: 8, y: 6, w: 4, h: 6 },
    { i: "cookbook", x: 0, y: 12, w: 12, h: 5 },
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
