import { useEffect, useMemo, useState } from "react";
import { api } from "../../api/client";
import type { TodayItem } from "../../api/types";
import PlantCard from "./components/PlantCard";
import SelectBar from "./components/SelectBar";
import SkeletonList from "./components/SkeletonList";

type LoadState = "loading" | "ready" | "error";
type Filter = "all" | "due" | "overdue";

function matchesFilter(item: TodayItem, filter: Filter): boolean {
  if (filter === "all") return true;
  if (filter === "due") return item.status === "due";
  if (filter === "overdue") return item.status === "overdue";
  return true;
}

export default function TodayPage() {
  const [loadState, setLoadState] = useState<LoadState>("loading");
  const [items, setItems] = useState<TodayItem[]>([]);
  const [error, setError] = useState<string>("");
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [filter, setFilter] = useState<Filter>("all");
  const [saving, setSaving] = useState(false);

  useEffect(() => { void reload(); }, []);

  async function reload() {
    setLoadState("loading");
    setError("");
    try {
      const data = await api.today();
      setItems(Array.isArray(data.items) ? data.items : []);
      setLoadState("ready");
    } catch (e: any) {
      setError(e?.message || "Failed to load");
      setLoadState("error");
    }
  }

  const visible = useMemo(() => items.filter((i) => matchesFilter(i, filter)), [items, filter]);

  function toggle(id: number) {
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  }

  function clearSelection() {
    setSelected(new Set());
  }

  function selectAllVisible() {
    setSelected((prev) => {
      const next = new Set(prev);
      for (const it of visible) next.add(it.id);
      return next;
    });
  }

  async function markWatered() {
    if (selected.size === 0 || saving) return;
    setSaving(true);
    setError("");
    try {
      await api.water({ plant_ids: Array.from(selected) });
      clearSelection();
      await reload();
    } catch (e: any) {
      setError(e?.message || "Failed to save");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="container">
      <div className="header">
        <div>
          <div className="h-title">PlantBuddy</div>
          <div className="h-sub">Сегодня</div>
        </div>
        <button className="btn" onClick={reload} disabled={loadState === "loading"}>
          Обновить
        </button>
      </div>

      <div className="filters">
        <button className={filter === "all" ? "pill pill--active" : "pill"} onClick={() => setFilter("all")}>
          Все
        </button>
        <button className={filter === "due" ? "pill pill--active" : "pill"} onClick={() => setFilter("due")}>
          Сегодня
        </button>
        <button className={filter === "overdue" ? "pill pill--active" : "pill"} onClick={() => setFilter("overdue")}>
          Просрочено
        </button>
      </div>

      {error ? <div className="alert">{error}</div> : null}

      {loadState === "loading" ? (
        <SkeletonList />
      ) : (
        <div className="list">
          {visible.map((it) => (
            <PlantCard
              key={it.id}
              item={it}
              checked={selected.has(it.id)}
              onToggle={() => toggle(it.id)}
            />
          ))}
          {loadState === "ready" && visible.length === 0 ? (
            <div className="empty">Пока тут пусто 🙂</div>
          ) : null}
        </div>
      )}

      <SelectBar
        count={selected.size}
        onSelectAll={selectAllVisible}
        onClear={clearSelection}
        onWater={markWatered}
        disabled={saving}
      />
    </div>
  );
}
