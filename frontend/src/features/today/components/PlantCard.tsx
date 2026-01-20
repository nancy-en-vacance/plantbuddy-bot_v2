import type { TodayItem } from "../../../api/types";

function badgeClass(status: TodayItem["status"]) {
  if (status === "overdue") return "badge badge--bad";
  if (status === "due") return "badge badge--warn";
  if (status === "ok") return "badge badge--ok";
  return "badge";
}

function badgeText(item: TodayItem) {
  if (item.status === "overdue") return "Просрочено";
  if (item.status === "due") return "Сегодня";
  if (item.status === "ok") return item.due_in_days != null ? `Через ${item.due_in_days}д` : "Ок";
  return "—";
}

export default function PlantCard(props: { item: TodayItem; checked: boolean; onToggle: () => void }) {
  const { item, checked, onToggle } = props;

  const meta =
    item.days_since_last_watering == null
      ? "Полив: нет данных"
      : `Полив: ${item.days_since_last_watering}д назад`;

  const norm = item.norm_days == null ? "норма: —" : `норма: ${item.norm_days}д`;

  return (
    <button className={checked ? "card card--checked" : "card"} onClick={onToggle}>
      <div className="card-top">
        <div className="card-name">{item.name}</div>
        <div className={badgeClass(item.status)}>{badgeText(item)}</div>
      </div>
      <div className="card-meta">
        <span>{meta}</span>
        <span className="dot">•</span>
        <span>{norm}</span>
      </div>
      <div className="card-check">{checked ? "✓" : ""}</div>
    </button>
  );
}
