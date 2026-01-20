import type { TodayResponse, WaterRequest, WaterResponse } from "./types";

function getInitData(): string {
  const tg = (window as any).Telegram?.WebApp;
  return typeof tg?.initData === "string" ? tg.initData : "";
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers || {});
  headers.set("Content-Type", "application/json");
  headers.set("X-Telegram-InitData", getInitData());

  const res = await fetch(path, { ...options, headers });

  const text = await res.text();
  let data: any = null;
  try { data = text ? JSON.parse(text) : null; } catch {}

  if (!res.ok) {
    const msg = (data && (data.detail || data.message)) || `HTTP ${res.status}`;
    throw new Error(msg);
  }
  return data as T;
}

export const api = {
  today(): Promise<TodayResponse> {
    return request<TodayResponse>("/api/today");
  },
  water(payload: WaterRequest): Promise<WaterResponse> {
    return request<WaterResponse>("/api/water", {
      method: "POST",
      body: JSON.stringify(payload)
    });
  }
};
