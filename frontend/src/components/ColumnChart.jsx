export default function ColumnChart({ title, rows, labelKey, valueKey, headerAction = null }) {
  const maxValue = rows.length ? Math.max(...rows.map((row) => row[valueKey] || 0)) : 0;

  return (
    <section className="panel">
      <div className="panel-header">
        <h2>{title}</h2>
        {headerAction}
      </div>
      {rows.length === 0 ? (
        <p className="empty-state">No data available yet.</p>
      ) : (
        <div className="column-chart">
          {rows.map((row) => {
            const height = maxValue ? `${(row[valueKey] / maxValue) * 100}%` : "0%";
            return (
              <div className="column-item" key={`${title}-${row[labelKey]}`}>
                <span className="column-value">{row[valueKey]}</span>
                <div className="column-track">
                  <div className="column-fill" style={{ height }} />
                </div>
                <span className="column-label">{row[labelKey]}</span>
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}
