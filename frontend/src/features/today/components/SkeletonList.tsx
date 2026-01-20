export default function SkeletonList() {
  return (
    <div className="list">
      {Array.from({ length: 6 }).map((_, i) => (
        <div className="skeleton" key={i} />
      ))}
    </div>
  );
}
