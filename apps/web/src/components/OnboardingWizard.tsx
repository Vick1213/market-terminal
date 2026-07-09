"use client";

import { useState } from "react";
import { createPortal } from "react-dom";
import { useMutation } from "@tanstack/react-query";
import { updateSettings } from "@/lib/api";

type Step = "welcome" | "fred" | "tiingo" | "broker" | "done";
const STEPS: Step[] = ["welcome", "fred", "tiingo", "broker", "done"];

/** First-run wizard: shown once, when GET /api/settings returns
 * onboarded=false. Every step is optional/skippable — "Skip setup" and the
 * final "Finish" both just PUT {onboarded: true}. Per-step key entry is
 * saved best-effort as the user moves forward, so a key already typed isn't
 * lost even if they skip out partway through. */
export function OnboardingWizard({ onDone }: { onDone: () => void }) {
  const [step, setStep] = useState<Step>("welcome");
  const [fredKey, setFredKey] = useState("");
  const [tiingoKey, setTiingoKey] = useState("");
  const [alpacaKeyId, setAlpacaKeyId] = useState("");
  const [alpacaSecret, setAlpacaSecret] = useState("");

  const finish = useMutation({
    mutationFn: (fields: Record<string, string | boolean>) => updateSettings(fields),
    onSuccess: onDone,
  });

  const skip = () => finish.mutate({ onboarded: true });

  const idx = STEPS.indexOf(step);

  const next = () => {
    if (step === "welcome") {
      setStep("fred");
      return;
    }
    if (step === "fred") {
      if (fredKey.trim()) updateSettings({ fred_api_key: fredKey.trim() }).catch(() => {});
      setStep("tiingo");
      return;
    }
    if (step === "tiingo") {
      if (tiingoKey.trim()) updateSettings({ tiingo_api_key: tiingoKey.trim() }).catch(() => {});
      setStep("broker");
      return;
    }
    if (step === "broker") {
      const fields: Record<string, string> = {};
      if (alpacaKeyId.trim()) fields.alpaca_key_id = alpacaKeyId.trim();
      if (alpacaSecret.trim()) fields.alpaca_secret_key = alpacaSecret.trim();
      if (Object.keys(fields).length) updateSettings(fields).catch(() => {});
      setStep("done");
      return;
    }
  };

  const back = () => idx > 0 && setStep(STEPS[idx - 1]);

  const modal = (
    <div className="modal-backdrop">
      <div className="modal wizard-modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <span>Welcome to Market Terminal</span>
          <button className="expand-btn" onClick={skip} disabled={finish.isPending}>
            Skip setup
          </button>
        </div>
        <div className="wizard-steps">
          {STEPS.map((s, i) => (
            <span
              key={s}
              className={`wizard-step-dot ${i === idx ? "active" : i < idx ? "done" : ""}`}
            />
          ))}
        </div>
        <div className="wizard-body">
          {step === "welcome" && (
            <>
              <div className="wizard-title">A local-first market terminal</div>
              <div className="wizard-copy">
                Market Terminal runs entirely on your machine and uses your own free/BYO API
                keys — nothing is sent to us. This quick setup adds the two keys that unlock the
                most panels. Everything here is optional and can be changed anytime from the gear
                icon.
              </div>
            </>
          )}
          {step === "fred" && (
            <>
              <div className="wizard-title">FRED API key (recommended)</div>
              <div className="wizard-copy">
                Free, instant signup at{" "}
                <a href="https://fred.stlouisfed.org/docs/api/api_key.html" target="_blank" rel="noreferrer">
                  fred.stlouisfed.org
                </a>
                . Powers the macro composite, yields/spreads, and the CPI/NFP/PPI/GDP release
                calendar.
              </div>
              <input
                className="settings-field-input"
                style={{ width: "100%" }}
                type="password"
                placeholder="FRED API key (optional — skip if you don't have one yet)"
                value={fredKey}
                onChange={(e) => setFredKey(e.target.value)}
                spellCheck={false}
              />
            </>
          )}
          {step === "tiingo" && (
            <>
              <div className="wizard-title">Tiingo API key (recommended)</div>
              <div className="wizard-copy">
                Free-tier signup at{" "}
                <a href="https://www.tiingo.com" target="_blank" rel="noreferrer">
                  tiingo.com
                </a>
                . Daily price bars for your watchlist and every chart.
              </div>
              <input
                className="settings-field-input"
                style={{ width: "100%" }}
                type="password"
                placeholder="Tiingo API key (optional — skip if you don't have one yet)"
                value={tiingoKey}
                onChange={(e) => setTiingoKey(e.target.value)}
                spellCheck={false}
              />
            </>
          )}
          {step === "broker" && (
            <>
              <div className="wizard-title">Broker keys (optional)</div>
              <div className="wizard-copy">
                Only needed for the paper-trading bots. Free paper keys from{" "}
                <a href="https://app.alpaca.markets/signup" target="_blank" rel="noreferrer">
                  app.alpaca.markets
                </a>
                . Skip this if you just want the dashboard.
              </div>
              <input
                className="settings-field-input"
                style={{ width: "100%", marginBottom: 8 }}
                placeholder="Alpaca key ID (optional)"
                value={alpacaKeyId}
                onChange={(e) => setAlpacaKeyId(e.target.value)}
                spellCheck={false}
              />
              <input
                className="settings-field-input"
                style={{ width: "100%" }}
                type="password"
                placeholder="Alpaca secret key (optional)"
                value={alpacaSecret}
                onChange={(e) => setAlpacaSecret(e.target.value)}
                spellCheck={false}
              />
            </>
          )}
          {step === "done" && (
            <>
              <div className="wizard-title">You&apos;re all set</div>
              <div className="wizard-copy">
                Add, change, or test any key later from the gear icon in the top-right corner.
              </div>
            </>
          )}
        </div>
        <div className="wizard-nav">
          <button className="expand-btn" onClick={back} disabled={idx === 0}>
            Back
          </button>
          {step === "done" ? (
            <button className="settings-save-btn" onClick={skip} disabled={finish.isPending}>
              {finish.isPending ? "finishing…" : "Finish"}
            </button>
          ) : (
            <button className="settings-save-btn" onClick={next}>
              Next
            </button>
          )}
        </div>
      </div>
    </div>
  );
  return createPortal(modal, document.body);
}
