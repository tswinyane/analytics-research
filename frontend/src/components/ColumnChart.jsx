export default function ColumnChart({
  title,
  rows,
  labelKey,
  valueKey,
  headerAction = null,
  // Optional: make the bars clickable. onSelect(label) is called with the bar's label;
  // selectedLabel highlights that bar and dims the others.
  onSelect = null,
  selectedLabel = null,
}) {
  const maxValue = rows.length ? Math.max(...rows.map((row) => row[valueKey] || 0)) : 0;
  const hasSelection = selectedLabel !== null && selectedLabel !== undefined && selectedLabel !== "";

  return (
    <section className="panel">
      <div className="panel-header">
        <h2>{title}</h2>
        {headerAction}
      </div>
      {rows.length === 0 ? (
        <p className="empty-state">No data available yet.</p>
      ) : (
        <div className={`column-chart ${onSelect ? "column-chart-interactive" : ""}`.trim()}>
          {rows.map((row) => {
            const height = maxValue ? `${(row[valueKey] / maxValue) * 100}%` : "0%";
            const label = row[labelKey];
            const isSelected = hasSelection && String(label) === String(selectedLabel);
            const className = [
              "column-item",
              isSelected ? "selected" : "",
              hasSelection && !isSelected ? "dimmed" : "",
            ]
              .filter(Boolean)
              .join(" ");
            const content = (
              <>
                <span className="column-value">{row[valueKey]}</span>
                <div className="column-track">
                  <div className="column-fill" style={{ height }} />
                </div>
                <span className="column-label">{label}</span>
              </>
            );
            return onSelect ? (
              <button
                type="button"
                className={className}
                key={`${title}-${label}`}
                aria-pressed={isSelected}
                title={isSelected ? `Show all years` : `Show publications from ${label}`}
                onClick={() => onSelect(label)}
              >
                {content}
              </button>
            ) : (
              <div className={className} key={`${title}-${label}`}>
                {content}
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}
