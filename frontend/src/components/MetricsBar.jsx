export function MetricsBar({ metrics }) {
  return (
    <section className="metrics">
      {metrics.map((metric) => (
        <div className="metric" key={metric.label}>
          <span>{metric.label}</span>
          <strong>{metric.value}</strong>
          {metric.label === "complete" && (
            <div className="meter" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={metric.raw || 0}>
              <i style={{ width: `${metric.raw || 0}%` }} />
            </div>
          )}
        </div>
      ))}
    </section>
  );
}
