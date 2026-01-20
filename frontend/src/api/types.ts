export type PlantStatus = "unknown" | "ok" | "due" | "overdue";

export type TodayItem = {
  id: number;
  name: string;
  norm_days: number | null;
  last_watered_at: string | null;
  days_since_last_watering: number | null;
  due_in_days: number | null;
  status: PlantStatus;
};

export type TodayResponse = { items: TodayItem[] };

export type WaterRequest = { plant_ids: number[] };

export type WaterResponse = { ok: boolean; updated: number };
