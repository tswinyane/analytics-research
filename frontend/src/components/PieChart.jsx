const polarToCartesian = (centerX, centerY, radius, angleInDegrees) => {
  const angleInRadians = ((angleInDegrees - 90) * Math.PI) / 180;
  return {
    x: centerX + radius * Math.cos(angleInRadians),
    y: centerY + radius * Math.sin(angleInRadians),
  };
};

const describeArc = (x, y, radius, startAngle, endAngle) => {
  const start = polarToCartesian(x, y, radius, endAngle);
  const end = polarToCartesian(x, y, radius, startAngle);
  const largeArcFlag = endAngle - startAngle <= 180 ? "0" : "1";
  return [
    "M",
    x,
    y,
    "L",
    start.x,
    start.y,
    "A",
    radius,
    radius,
    0,
    largeArcFlag,
    0,
    end.x,
    end.y,
    "Z",
  ].join(" ");
};

const palette = ["#0f766e", "#c4913b", "#14532d", "#2563eb", "#b45309", "#64748b"];

export default function PieChart({
  title,
  rows,
  labelKey,
  valueKey,
  showLegendValues = true,
  showTotal = true,
  centerLegend = false,
  showChart = true,
}) {
  const total = rows.reduce((sum, row) => sum + (row[valueKey] || 0), 0);
  let startAngle = 0;

  return (
    <section className="panel">
      <div className="panel-header">
        <h2>{title}</h2>
      </div>
      {rows.length === 0 || total === 0 ? (
        <p className="empty-state">No data available yet.</p>
      ) : (
        <div className="pie-layout">
          {showChart ? (
            <svg className="pie-chart" viewBox="0 0 220 220" aria-label={title}>
              {rows.map((row, index) => {
                const value = row[valueKey] || 0;
                const angle = (value / total) * 360;
                const path = describeArc(110, 110, 92, startAngle, startAngle + angle);
                const fill = palette[index % palette.length];
                startAngle += angle;
                return <path key={`${title}-${row[labelKey]}`} d={path} fill={fill} />;
              })}
              <circle cx="110" cy="110" r="48" fill="white" />
              {showTotal ? (
                <>
                  <text x="110" y="104" textAnchor="middle" className="pie-total-label">
                    Total
                  </text>
                  <text x="110" y="126" textAnchor="middle" className="pie-total-value">
                    {total}
                  </text>
                </>
              ) : null}
            </svg>
          ) : null}
          <div className={centerLegend ? "pie-legend pie-legend-center" : "pie-legend"}>
            {rows.map((row, index) => (
              <div
                className={centerLegend ? "legend-row legend-row-center" : "legend-row"}
                key={`${title}-legend-${row[labelKey]}`}
              >
                <span
                  className="legend-swatch"
                  style={{ backgroundColor: palette[index % palette.length] }}
                />
                <span className="legend-label">{row[labelKey]}</span>
                {showLegendValues ? <strong>{row[valueKey]}</strong> : null}
              </div>
            ))}
          </div>
        </div>
      )}
    </section>
  );
}
