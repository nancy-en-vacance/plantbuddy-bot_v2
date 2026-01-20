export default function SelectBar(props: {
  count: number;
  disabled: boolean;
  onSelectAll: () => void;
  onClear: () => void;
  onWater: () => void;
}) {
  const { count, disabled, onSelectAll, onClear, onWater } = props;

  return (
    <div className="selectbar">
      <div className="selectbar-left">
        <div className="selectbar-count">Выбрано: {count}</div>
        <button className="link" onClick={onSelectAll} disabled={disabled}>
          Выбрать все
        </button>
        <button className="link" onClick={onClear} disabled={disabled || count === 0}>
          Сбросить
        </button>
      </div>
      <button className="btn btn--primary" onClick={onWater} disabled={disabled || count === 0}>
        Отметить полив
      </button>
    </div>
  );
}
