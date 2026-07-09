"use client";

import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { SettingsFieldView, SettingsTestProvider, SettingsUpdate } from "@market/shared";
import { fetchSettings, testSettingsProvider, updateSettings } from "@/lib/api";

interface ProviderSection {
  /** POST /api/settings/test/{key} provider name, or null when this
   * provider has no cheap test call wired up (e.g. IBKR — reads-only, no
   * simple "ping" endpoint). */
  key: SettingsTestProvider | null;
  title: string;
  fields: string[];
  signup?: string;
}

interface GroupSection {
  group: string;
  groupLabel: string;
  providers: ProviderSection[];
}

// Mirrors app/settings_store.py's FIELD_SPEC groups, but split further by
// PROVIDER (one Test button per provider, not per field — Alpaca has 5
// fields and one account to test).
const SECTIONS: GroupSection[] = [
  {
    group: "data",
    groupLabel: "Data",
    providers: [
      {
        key: "fred",
        title: "FRED",
        fields: ["fred_api_key"],
        signup: "https://fred.stlouisfed.org/docs/api/api_key.html",
      },
      {
        key: "tiingo",
        title: "Tiingo",
        fields: ["tiingo_api_key"],
        signup: "https://www.tiingo.com",
      },
      {
        key: "fmp",
        title: "FMP",
        fields: ["fmp_api_key"],
        signup: "https://financialmodelingprep.com",
      },
      {
        key: "finnhub",
        title: "Finnhub",
        fields: ["finnhub_api_key"],
        signup: "https://finnhub.io",
      },
    ],
  },
  {
    group: "broker",
    groupLabel: "Broker",
    providers: [
      {
        key: "alpaca",
        title: "Alpaca (paper trading)",
        fields: [
          "alpaca_key_id",
          "alpaca_secret_key",
          "alpaca_paper_key_id",
          "alpaca_paper_secret_key",
          "alpaca_trading_base_url",
        ],
        signup: "https://app.alpaca.markets/signup",
      },
      {
        key: null,
        title: "Interactive Brokers",
        fields: ["broker_backend", "ibkr_base_url", "ibkr_account_id"],
      },
    ],
  },
  {
    group: "ai",
    groupLabel: "AI",
    providers: [
      {
        key: "llm",
        title: "LLM narratives (brief + strategist)",
        fields: [
          "llm_provider",
          "llm_api_key",
          "llm_model",
          "llm_base_url",
          "ollama_url",
          "ollama_model",
        ],
      },
    ],
  },
  {
    group: "alerts",
    groupLabel: "Alerts",
    providers: [
      {
        key: "ntfy",
        title: "ntfy push",
        fields: ["ntfy_topic", "ntfy_server"],
        signup: "https://ntfy.sh",
      },
    ],
  },
];

const SELECT_OPTIONS: Record<string, string[]> = {
  broker_backend: ["alpaca", "ibkr"],
  llm_provider: ["ollama", "openai", "deepseek", "anthropic"],
};

function provLabel(p: string): string {
  if (p === "store") return "saved";
  if (p === "env") return "from env";
  return "default";
}

type TestState = { status: "idle" | "pending" | "ok" | "fail"; detail?: string };

export function SettingsModal({ onClose }: { onClose: () => void }) {
  const queryClient = useQueryClient();
  const { data, isLoading } = useQuery({ queryKey: ["settings"], queryFn: fetchSettings });
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [tests, setTests] = useState<Record<string, TestState>>({});

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = "";
    };
  }, [onClose]);

  const save = useMutation({
    mutationFn: (u: SettingsUpdate) => updateSettings(u),
    onSuccess: (resp) => {
      queryClient.setQueryData(["settings"], resp);
      setEdits({});
    },
  });

  const runTest = async (provider: SettingsTestProvider) => {
    setTests((t) => ({ ...t, [provider]: { status: "pending" } }));
    try {
      const res = await testSettingsProvider(provider);
      setTests((t) => ({ ...t, [provider]: { status: res.ok ? "ok" : "fail", detail: res.detail } }));
    } catch (e) {
      setTests((t) => ({ ...t, [provider]: { status: "fail", detail: (e as Error).message } }));
    }
  };

  const dirty = Object.keys(edits).length > 0;

  const modal = (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span>Settings — BYO API keys &amp; broker config</span>
          <button className="expand-btn" title="Close (Esc)" onClick={onClose}>
            ✕
          </button>
        </div>
        <div className="modal-body">
          {isLoading || !data ? (
            <div style={{ color: "var(--text-dim)", fontSize: 12 }}>loading…</div>
          ) : (
            SECTIONS.map((section) => (
              <div className="settings-group" key={section.group}>
                <div className="settings-group-title">{section.groupLabel}</div>
                {section.providers.map((p) => {
                  const testState = p.key ? tests[p.key] : undefined;
                  return (
                    <div className="settings-provider" key={p.title}>
                      <div className="settings-provider-head">
                        <span className="settings-provider-title">
                          {p.title}
                          {p.signup && (
                            <a
                              className="settings-signup-link"
                              href={p.signup}
                              target="_blank"
                              rel="noreferrer"
                            >
                              get a key ↗
                            </a>
                          )}
                        </span>
                        {p.key && (
                          <span>
                            <button
                              className="settings-test-btn"
                              disabled={testState?.status === "pending"}
                              onClick={() => runTest(p.key as SettingsTestProvider)}
                            >
                              {testState?.status === "pending" ? "testing…" : "Test"}
                            </button>
                            {testState && testState.status !== "pending" && testState.status !== "idle" && (
                              <span className={`settings-test-result ${testState.status}`}>
                                {testState.status === "ok" ? "✓" : "✕"} {testState.detail}
                              </span>
                            )}
                          </span>
                        )}
                      </div>
                      {p.fields.map((f) => {
                        const view: SettingsFieldView | undefined = data.fields[f];
                        if (!view) return null;
                        const options = SELECT_OPTIONS[f];
                        const editVal = edits[f];
                        return (
                          <div className="settings-field-row" key={f}>
                            <span className="settings-field-label">{view.label}</span>
                            {options ? (
                              <select
                                className="settings-field-input"
                                value={editVal ?? view.value ?? options[0]}
                                onChange={(e) => setEdits((s) => ({ ...s, [f]: e.target.value }))}
                              >
                                {options.map((o) => (
                                  <option key={o} value={o}>
                                    {o}
                                  </option>
                                ))}
                              </select>
                            ) : (
                              <input
                                className="settings-field-input"
                                type={view.secret ? "password" : "text"}
                                value={editVal ?? ""}
                                placeholder={view.value ?? "not set"}
                                onChange={(e) => setEdits((s) => ({ ...s, [f]: e.target.value }))}
                                spellCheck={false}
                              />
                            )}
                            <span className={`settings-prov-tag ${view.provenance}`}>
                              {provLabel(view.provenance)}
                            </span>
                            {view.value && (
                              <button
                                className="wl-remove"
                                title={`Clear ${view.label}`}
                                onClick={() => setEdits((s) => ({ ...s, [f]: "" }))}
                              >
                                ✕
                              </button>
                            )}
                          </div>
                        );
                      })}
                    </div>
                  );
                })}
              </div>
            ))
          )}
        </div>
        <div className="settings-footer">
          <span className="settings-footer-note">
            stored locally in apps/api/data/settings.json — never sent anywhere except the
            provider you Test or the broker/LLM the bots actually call
          </span>
          {save.isError && (
            <span style={{ color: "var(--red)", fontSize: 11 }}>{(save.error as Error).message}</span>
          )}
          <button
            className="settings-save-btn"
            disabled={!dirty || save.isPending}
            onClick={() => save.mutate(edits)}
          >
            {save.isPending ? "saving…" : "Save changes"}
          </button>
        </div>
      </div>
    </div>
  );
  return createPortal(modal, document.body);
}
