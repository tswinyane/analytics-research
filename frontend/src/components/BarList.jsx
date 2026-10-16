import LoadingSpinner from "./LoadingSpinner";

export default function BarList({
  title,
  rows,
  labelKey,
  valueKey,
  loading = false,
  headerAction = null,
  className = "",
  labelHeading = "",
  valueHeading = "",
}) {
  const maxValue = rows.length ? Math.max(...rows.map((row) => row[valueKey] || 0)) : 0;

  return (
    <section className={`panel ${className}`.trim()}>
      <div className="panel-header">
        <h2>{title}</h2>
        {headerAction}
      </div>
      <div className="bar-list">
        {loading ? (
          <LoadingSpinner label="Loading..." />
        ) : rows.length === 0 ? (
          <p className="empty-state">No data available yet.</p>
        ) : (
          <>
          {labelHeading || valueHeading ? (
            <div className="bar-copy bar-list-heading" aria-hidden="true">
              <span>{labelHeading}</span>
              <span>{valueHeading}</span>
            </div>
          ) : null}
          {rows.map((row) => {
            const width = maxValue ? `${(row[valueKey] / maxValue) * 100}%` : "0%";
            return (
              <div className="bar-row" key={`${title}-${row[labelKey]}`}>
                <div className="bar-copy">
                  <span>{row[labelKey]}</span>
                  <strong>{row[valueKey]}</strong>
                </div>
                <div className="bar-track">
                  <div className="bar-fill" style={{ width }} />
                </div>
              </div>
            );
          })}
          </>
        )}
      </div>
    </section>
  );
}
