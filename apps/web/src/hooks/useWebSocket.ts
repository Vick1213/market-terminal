"use client";

import { useEffect, useRef, useState } from "react";
import type { WsMessage } from "@market/shared";
import { WS_URL } from "@/lib/api";

export type WsStatus = "connecting" | "open" | "closed";

/**
 * Subscribe to a backend WS topic with auto-reconnect (exponential backoff).
 * Returns connection status plus the most-recent `max` messages (newest first).
 */
export function useWebSocket(topic: string, max = 50) {
  const [status, setStatus] = useState<WsStatus>("connecting");
  const [messages, setMessages] = useState<WsMessage[]>([]);
  const retry = useRef(0);

  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let ws: WebSocket | undefined;

    const connect = () => {
      if (!active) return;
      setStatus("connecting");
      ws = new WebSocket(`${WS_URL}/ws/${topic}`);

      ws.onopen = () => {
        if (!active) return;
        retry.current = 0;
        setStatus("open");
      };
      ws.onmessage = (ev) => {
        if (!active) return;
        try {
          const data = JSON.parse(ev.data) as WsMessage;
          setMessages((prev) => [data, ...prev].slice(0, max));
        } catch {
          /* ignore non-JSON frames */
        }
      };
      ws.onclose = () => {
        if (!active) return;
        setStatus("closed");
        const delay = Math.min(1000 * 2 ** retry.current, 15000);
        retry.current += 1;
        timer = setTimeout(connect, delay);
      };
      ws.onerror = () => ws?.close();
    };

    connect();
    return () => {
      active = false;
      if (timer) clearTimeout(timer);
      ws?.close();
    };
  }, [topic, max]);

  return { status, messages, last: messages[0] };
}
