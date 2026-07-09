"use client";

import { Suspense, useEffect, useMemo } from "react";
import { useSearchParams } from "next/navigation";
import { getPanelDef } from "@/components/panelRegistry";
import { usePopoutClient } from "@/hooks/usePopouts";

// Renders a single panel full-viewport in its own window (multi-monitor
// support). Opened via the pop-out button on a grid tile — see
// DashboardGrid.tsx + usePopouts().
//
// Static-export note: this is a single static page (no [panelId] dynamic
// route segment) — the panel id travels as a `?panel=` query string and is
// read client-side via useSearchParams(), never touching the Next.js router
// or generateStaticParams. That's deliberate: `apps/desktop` ships this app
// as a static export (see next.config.mjs / package.json build:export), and
// a static export can only pre-render dynamic segments it knows about ahead
// of time. Query strings aren't part of route matching, so one exported
// /popout/index.html works for every panel id — known or unknown — and the
// "Unknown panel" branch below stays reachable at runtime for ids that don't
// (or no longer) exist in PANEL_REGISTRY (e.g. a stale localStorage entry
// from a previous session), exactly like it did as a dynamic route in dev.
function PopoutContent() {
  const searchParams = useSearchParams();
  const panelId = searchParams.get("panel") ?? "";
  const panel = useMemo(() => getPanelDef(panelId), [panelId]);

  usePopoutClient(panel ? panelId : null);

  useEffect(() => {
    document.title = panel ? `${panel.title} — Market Terminal` : "Market Terminal";
  }, [panel]);

  if (!panel) {
    return (
      <main className="app popout-page">
        <div className="popout-error">
          <span>Unknown panel &ldquo;{panelId}&rdquo;.</span>
          <button className="expand-btn" onClick={() => window.close()}>
            Close window
          </button>
        </div>
      </main>
    );
  }

  return (
    <main className="app popout-page">
      <div className="popout-panel">{panel.render()}</div>
    </main>
  );
}

export default function PopoutPage() {
  // useSearchParams() opts the subtree into client-side rendering, which
  // requires a Suspense boundary (Next.js enforces this for both normal and
  // static-export builds).
  return (
    <Suspense fallback={null}>
      <PopoutContent />
    </Suspense>
  );
}
