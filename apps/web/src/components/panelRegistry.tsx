"use client";

import type { ReactNode } from "react";
import { AlertsPanel } from "./panels/AlertsPanel";
import { BotPanel } from "./panels/BotPanel";
import { BriefPanel } from "./panels/BriefPanel";
import { CalendarPanel } from "./panels/CalendarPanel";
import { CorrelationPanel } from "./panels/CorrelationPanel";
import { DivergencePanel } from "./panels/DivergencePanel";
import { Edgar8KPanel } from "./panels/Edgar8KPanel";
import { Edgar13DPanel } from "./panels/Edgar13DPanel";
import { EnergyPanel } from "./panels/EnergyPanel";
import { FedSpeechesPanel } from "./panels/FedSpeechesPanel";
import { HeartbeatPanel } from "./panels/HeartbeatPanel";
import { HelpPanel } from "./panels/HelpPanel";
import { KalshiPanel } from "./panels/KalshiPanel";
import { MacroPanel } from "./panels/MacroPanel";
import { MultiAssetPanel } from "./panels/MultiAssetPanel";
import { NewsPanel } from "./panels/NewsPanel";
import { PolicyRiskPanel } from "./panels/PolicyRiskPanel";
import { PortfolioPanel } from "./panels/PortfolioPanel";
import { PositioningPanel } from "./panels/PositioningPanel";
import { RatesInflationPanel } from "./panels/RatesInflationPanel";
import { RetailPanel } from "./panels/RetailPanel";
import { RotationPanel } from "./panels/RotationPanel";
import { SourceHealthPanel } from "./panels/SourceHealthPanel";
import { StrategistPanel } from "./panels/StrategistPanel";
import { TffPositioningPanel } from "./panels/TffPositioningPanel";
import { TradebookPanel } from "./panels/TradebookPanel";
import { TreasurySupplyPanel } from "./panels/TreasurySupplyPanel";
import { VolOverlayPanel } from "./panels/VolOverlayPanel";
import { WatchlistPanel } from "./panels/WatchlistPanel";
import { WhalesPanel } from "./panels/WhalesPanel";

export interface PanelDef {
  /** Grid id — also the /popout `?panel=` query value and localStorage key. */
  id: string;
  /** Human title, used for the popout window's document.title and tooltips. */
  title: string;
  render: () => ReactNode;
}

// Single source of truth for "what panels exist" — DashboardGrid (the grid
// layout) and /popout (single-panel popout windows, id via `?panel=`) both
// read this.
// Titles are copied from each panel's own `.panel-head` text so a popout
// window's title bar matches what the panel calls itself in the grid.
export const PANEL_REGISTRY: PanelDef[] = [
  { id: "portfolio", title: "Portfolio", render: () => <PortfolioPanel /> },
  { id: "system", title: "System · Phase 0", render: () => <HeartbeatPanel /> },
  { id: "news", title: "News + Sentiment", render: () => <NewsPanel /> },
  { id: "liquidity", title: "Liquidity & Macro", render: () => <MacroPanel /> },
  { id: "watchlist", title: "Custom Watchlist", render: () => <WatchlistPanel /> },
  { id: "multiasset", title: "Multi-Asset Liquidity & Major Trades", render: () => <MultiAssetPanel /> },
  { id: "retail", title: "Retail Market Score", render: () => <RetailPanel /> },
  { id: "cookbook", title: "Correlation Cookbook", render: () => <CorrelationPanel /> },
  { id: "brief", title: "Morning Brief", render: () => <BriefPanel /> },
  { id: "alerts", title: "Alerts", render: () => <AlertsPanel /> },
  { id: "calendar", title: "Event Horizon", render: () => <CalendarPanel /> },
  { id: "rotation", title: "Sector Rotation (RRG)", render: () => <RotationPanel /> },
  { id: "positioning", title: "Smart Money — COT · Gamma · Insiders", render: () => <PositioningPanel /> },
  { id: "whales", title: "Whales — 13F Funds · Congress", render: () => <WhalesPanel /> },
  { id: "divergence", title: "Divergence — Narrative vs Money", render: () => <DivergencePanel /> },
  { id: "edgar8k", title: "8-K Catalysts — Corporate Events", render: () => <Edgar8KPanel /> },
  { id: "edgar13d", title: "13D / 13G — Activist & Passive Stakes", render: () => <Edgar13DPanel /> },
  { id: "tff", title: "Positioning — Real Money vs Hedge Funds (TFF)", render: () => <TffPositioningPanel /> },
  { id: "treasury", title: "Treasury Supply — Auction Demand", render: () => <TreasurySupplyPanel /> },
  { id: "kalshi", title: "Kalshi — Rate & CPI Odds", render: () => <KalshiPanel /> },
  { id: "nowcast", title: "Rates & Inflation — Cleveland Nowcast", render: () => <RatesInflationPanel /> },
  { id: "energy", title: "Energy Fundamentals — EIA Weekly", render: () => <EnergyPanel /> },
  { id: "policyrisk", title: "Policy-Risk Surface — GPR & TPU", render: () => <PolicyRiskPanel /> },
  { id: "fedspeak", title: "Fed Speeches — Hawk / Dove", render: () => <FedSpeechesPanel /> },
  { id: "strategist", title: "Strategist", render: () => <StrategistPanel /> },
  { id: "voloverlay", title: "Quant Overlay — Vol-Target + Factor Screen", render: () => <VolOverlayPanel /> },
  { id: "bot", title: "Bot", render: () => <BotPanel /> },
  { id: "tradebook", title: "Tradebook", render: () => <TradebookPanel /> },
  { id: "sources", title: "Data Sources", render: () => <SourceHealthPanel /> },
  { id: "help", title: "Field Manual", render: () => <HelpPanel /> },
];

export function getPanelDef(id: string): PanelDef | undefined {
  return PANEL_REGISTRY.find((p) => p.id === id);
}
