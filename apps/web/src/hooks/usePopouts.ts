"use client";

import { useCallback, useEffect, useRef, useState } from "react";

// Cross-window "which panels are popped out" state. Two channels back each
// other up: localStorage (persisted, survives reload) and BroadcastChannel
// (instant push to every same-origin window, including ones opened via
// window.open). Either one alone would miss cases the other handles.
const STORAGE_KEY = "market:popouts";
const CHANNEL_NAME = "market:popouts";

type PopoutMessage =
  | { type: "add"; id: string }
  | { type: "remove"; id: string }
  | { type: "ping" }
  | { type: "pong"; id: string };

function readStoredIds(): string[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.filter((x): x is string => typeof x === "string") : [];
  } catch {
    return [];
  }
}

function writeStoredIds(ids: string[]) {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(ids));
  } catch {
    /* storage disabled/full — live state via BroadcastChannel still works */
  }
}

function openChannel(): BroadcastChannel | null {
  if (typeof window === "undefined" || typeof BroadcastChannel === "undefined") return null;
  return new BroadcastChannel(CHANNEL_NAME);
}

// `window.open` doesn't create real OS windows inside a Tauri webview (it's a
// no-op or opens in-place depending on platform), so popouts need a native
// path when running desktop. "__TAURI_INTERNALS__" is injected onto `window`
// by every Tauri webview at load time — cheaper and more reliable than
// feature-detecting individual APIs, and it's the same check the
// `@tauri-apps/api` package itself uses internally.
function isTauri(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

// `@tauri-apps/api` is imported dynamically (never at module scope) so the
// plain web build never bundles or evaluates Tauri's IPC glue when it isn't
// running inside Tauri — `pnpm --filter @market/web build` must stay clean
// with no Tauri runtime present.
//
// Same-origin note re: BroadcastChannel/localStorage sync (see usePopoutClient
// below and the module doc comment above): in dev, tauri.conf.json's
// `devUrl` points the whole app — main window AND any WebviewWindow opened
// with a relative `url` — at the Next.js dev server (http://localhost:3000).
// A relative `url` like `/popout?panel=<id>` resolves against that same
// devUrl, so every native popout window loads from the exact same origin as
// the main window, same as a window.open'd browser tab. That means
// BroadcastChannel and localStorage require no extra bridging to sync across
// native Tauri windows — they're plain same-origin web platform APIs from
// the webview's point of view. (Reasoning only; multi-window runtime
// verification under `tauri dev` is left to a follow-up per the task notes.)
// In production, frontendDist (../web/out, a static export — see
// apps/web/next.config.mjs) serves the exact same single `/popout` page for
// every id; only the query string differs, so this holds there too.
async function openOrFocusTauriWindow(label: string, path: string): Promise<void> {
  try {
    const { WebviewWindow } = await import("@tauri-apps/api/webviewWindow");
    const existing = await WebviewWindow.getByLabel(label);
    if (existing) {
      await existing.setFocus();
      return;
    }
    const win = new WebviewWindow(label, {
      url: path,
      title: "Market Terminal",
      width: 720,
      height: 560,
      resizable: true,
      focus: true,
    });
    win.once("tauri://error", (event) => {
      // eslint-disable-next-line no-console
      console.error(`[popout] failed to create Tauri window "${label}"`, event);
    });
  } catch (err) {
    // eslint-disable-next-line no-console
    console.error(`[popout] Tauri window open failed for "${label}"`, err);
  }
}

/** Returns true if the native window exists and was focused. */
async function focusTauriWindowIfAlive(label: string): Promise<boolean> {
  try {
    const { WebviewWindow } = await import("@tauri-apps/api/webviewWindow");
    const existing = await WebviewWindow.getByLabel(label);
    if (!existing) return false;
    await existing.setFocus();
    return true;
  } catch (err) {
    // eslint-disable-next-line no-console
    console.error(`[popout] Tauri window focus failed for "${label}"`, err);
    return false;
  }
}

/** Broadcast a ping and collect the ids that pong back within `windowMs`. */
function pingAndCollect(channel: BroadcastChannel | null, windowMs: number): Promise<Set<string>> {
  return new Promise((resolve) => {
    if (!channel) {
      resolve(new Set());
      return;
    }
    const alive = new Set<string>();
    const onMessage = (ev: MessageEvent<PopoutMessage>) => {
      if (ev.data.type === "pong") alive.add(ev.data.id);
    };
    channel.addEventListener("message", onMessage);
    channel.postMessage({ type: "ping" } satisfies PopoutMessage);
    window.setTimeout(() => {
      channel.removeEventListener("message", onMessage);
      resolve(alive);
    }, windowMs);
  });
}

/**
 * Main-window side: tracks which panel ids currently live in their own
 * popout window, and lets the grid open/reclaim them.
 *
 * Liveness: on mount and on window focus we ping every window on the
 * channel; a popout that doesn't reply within ~1s (force-closed, crashed,
 * or left over from a previous session) gets pruned from state + storage.
 */
export function usePopouts() {
  const [ids, setIds] = useState<string[]>(() => readStoredIds());
  const channelRef = useRef<BroadcastChannel | null>(null);

  const prune = useCallback(async () => {
    const alive = await pingAndCollect(channelRef.current, 1000);
    setIds((prev) => {
      const next = prev.filter((id) => alive.has(id));
      if (next.length !== prev.length) writeStoredIds(next);
      return next.length === prev.length ? prev : next;
    });
  }, []);

  useEffect(() => {
    const channel = openChannel();
    channelRef.current = channel;

    const addId = (id: string) => setIds((prev) => (prev.includes(id) ? prev : [...prev, id]));
    const removeId = (id: string) => setIds((prev) => prev.filter((x) => x !== id));

    const onMessage = (ev: MessageEvent<PopoutMessage>) => {
      if (ev.data.type === "add") addId(ev.data.id);
      else if (ev.data.type === "remove") removeId(ev.data.id);
    };
    channel?.addEventListener("message", onMessage);

    // Fallback for browsers without BroadcastChannel, and belt-and-suspenders
    // for the normal case: localStorage writes in other windows fire here.
    const onStorage = (ev: StorageEvent) => {
      if (ev.key === STORAGE_KEY) setIds(readStoredIds());
    };
    window.addEventListener("storage", onStorage);
    window.addEventListener("focus", prune);
    prune();

    return () => {
      channel?.removeEventListener("message", onMessage);
      channel?.close();
      window.removeEventListener("storage", onStorage);
      window.removeEventListener("focus", prune);
    };
  }, [prune]);

  useEffect(() => {
    writeStoredIds(ids);
  }, [ids]);

  const isPoppedOut = useCallback((id: string) => ids.includes(id), [ids]);

  const openPopout = useCallback((id: string) => {
    const name = `popout-${id}`;
    setIds((prev) => (prev.includes(id) ? prev : [...prev, id]));
    channelRef.current?.postMessage({ type: "add", id } satisfies PopoutMessage);

    if (isTauri()) {
      // Native path: create (or focus, if already open) a real OS window
      // instead of window.open, which doesn't produce a window inside a
      // Tauri webview.
      void openOrFocusTauriWindow(name, `/popout?panel=${encodeURIComponent(id)}`);
      return;
    }

    const features = "width=720,height=560,menubar=no,toolbar=no,location=no,status=no";
    window.open(`/popout?panel=${encodeURIComponent(id)}`, name, features)?.focus();
  }, []);

  const bringBack = useCallback(async (id: string) => {
    const name = `popout-${id}`;

    if (isTauri()) {
      const focused = await focusTauriWindowIfAlive(name);
      if (focused) return;
      // Stale entry (window was force-closed) — just drop it so the panel
      // re-renders in the grid.
      setIds((prev) => prev.filter((x) => x !== id));
      channelRef.current?.postMessage({ type: "remove", id } satisfies PopoutMessage);
      return;
    }

    const alive = await pingAndCollect(channelRef.current, 400);
    if (alive.has(id)) {
      // Empty-url window.open on an existing named window reuses it
      // in-place without navigating it away.
      window.open("", name)?.focus();
      return;
    }
    // Stale entry (window was force-closed) — just drop it so the panel
    // re-renders in the grid.
    setIds((prev) => prev.filter((x) => x !== id));
    channelRef.current?.postMessage({ type: "remove", id } satisfies PopoutMessage);
  }, []);

  return { poppedOut: ids, isPoppedOut, openPopout, bringBack };
}

/**
 * Popout-window side: announces itself, answers liveness pings, and
 * unregisters on close. `id` is null while the panel id hasn't resolved yet
 * (or is unknown), in which case this is a no-op.
 */
export function usePopoutClient(id: string | null) {
  useEffect(() => {
    if (typeof window === "undefined" || !id) return;
    const panelId = id;
    const channel = openChannel();

    const announce = () => {
      const current = readStoredIds();
      if (!current.includes(panelId)) writeStoredIds([...current, panelId]);
      channel?.postMessage({ type: "add", id: panelId } satisfies PopoutMessage);
    };
    const onMessage = (ev: MessageEvent<PopoutMessage>) => {
      if (ev.data.type === "ping") channel?.postMessage({ type: "pong", id: panelId } satisfies PopoutMessage);
    };
    const unregister = () => {
      writeStoredIds(readStoredIds().filter((x) => x !== panelId));
      channel?.postMessage({ type: "remove", id: panelId } satisfies PopoutMessage);
    };

    channel?.addEventListener("message", onMessage);
    announce();
    window.addEventListener("pagehide", unregister);
    window.addEventListener("beforeunload", unregister);

    return () => {
      channel?.removeEventListener("message", onMessage);
      channel?.close();
      window.removeEventListener("pagehide", unregister);
      window.removeEventListener("beforeunload", unregister);
    };
  }, [id]);
}
