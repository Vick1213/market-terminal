"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { DashboardGrid } from "@/components/DashboardGrid";
import { OnboardingWizard } from "@/components/OnboardingWizard";
import { SettingsModal } from "@/components/SettingsModal";
import { fetchSettings } from "@/lib/api";

export default function Page() {
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [wizardDismissed, setWizardDismissed] = useState(false);
  // M3: GET /api/settings also carries the onboarding flag — the same query
  // SettingsModal reads, so opening it right after the wizard finishes shows
  // fresh data with no extra request.
  const { data: settings } = useQuery({
    queryKey: ["settings"],
    queryFn: fetchSettings,
    staleTime: 30_000,
  });
  const showWizard = !!settings && !settings.onboarded && !wizardDismissed;

  return (
    <main className="app">
      <header className="app-header">
        <h1>Market Terminal</h1>
        <span className="tag">local · private · free data · Phase 0 skeleton</span>
        <button
          className="settings-gear"
          title="Settings"
          aria-label="Settings"
          onClick={() => setSettingsOpen(true)}
        >
          ⚙
        </button>
      </header>
      <DashboardGrid />
      {settingsOpen && <SettingsModal onClose={() => setSettingsOpen(false)} />}
      {showWizard && <OnboardingWizard onDone={() => setWizardDismissed(true)} />}
    </main>
  );
}
