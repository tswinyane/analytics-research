const palette = ["#0d2240", "#b9975b", "#345b8c", "#8a6f43", "#60738b"];

const CHART_WIDTH = 760;
const CHART_HEIGHT = 340;
const MARGIN = { top: 20, right: 24, bottom: 64, left: 52 };
const INNER_WIDTH = CHART_WIDTH - MARGIN.left - MARGIN.right;
const INNER_HEIGHT = CHART_HEIGHT - MARGIN.top - MARGIN.bottom;
const GRID_LINES = 4;

const getX = (index, total) => {
  if (total <= 1) {
    return MARGIN.left + INNER_WIDTH / 2;
  }
  return MARGIN.left + (index / (total - 1)) * INNER_WIDTH;
};

const getY = (count, maxValue) => {
  if (maxValue <= 0) {
    return MARGIN.top + INNER_HEIGHT;
  }
  return MARGIN.top + INNER_HEIGHT - (count / maxValue) * INNER_HEIGHT;
};

const getYearMarkers = (years) => {
  if (years.length <= 4) {
    return years.map((year, index) => ({ year, index }));
  }

  const markerIndices = [0, Math.floor((years.length - 1) / 3), Math.floor(((years.length - 1) * 2) / 3), years.length - 1];
  return markerIndices
    .filter((index, markerIndex, values) => values.indexOf(index) === markerIndex)
    .map((index) => ({ year: years[index], index }));
};

const buildPath = (points, maxValue) =>
  points
    .map((point, index) => {
      const x = getX(index, points.length);
      const y = getY(point.count || 0, maxValue);
      return `${index === 0 ? "M" : "L"} ${x} ${y}`;
    })
    .join(" ");

export default function TopicTrendChart({ title, data }) {
  const series = data?.series ?? [];
  const years = data?.years ?? [];
  const yearMarkers = getYearMarkers(years);
  const maxValue = Math.max(
    0,
    ...series.flatMap((item) => item.points.map((point) => point.count || 0)),
  );

  return (
    <section className="panel">
      <div className="panel-header">
        <h2>{title}</h2>
      </div>
      {series.length === 0 || years.length === 0 ? (
        <p className="empty-state">No data available yet.</p>
      ) : (
        <div className="topic-trend-layout">
          <p className="topic-axis-note">
            X-axis: Publication year | Y-axis: Number of publications for each topic
          </p>
          <div className="topic-trend-canvas">
            <svg className="topic-trend-chart" viewBox={`0 0 ${CHART_WIDTH} ${CHART_HEIGHT}`} aria-label={title}>
              {Array.from({ length: GRID_LINES + 1 }, (_, index) => {
                const value = Math.round((maxValue / GRID_LINES) * (GRID_LINES - index));
                const y = MARGIN.top + (INNER_HEIGHT / GRID_LINES) * index;
                return (
                  <g key={`${title}-grid-${index}`}>
                    <line
                      x1={MARGIN.left}
                      y1={y}
                      x2={CHART_WIDTH - MARGIN.right}
                      y2={y}
                      className="topic-grid-line"
                    />
                    <text
                      x={MARGIN.left - 10}
                      y={y + 4}
                      textAnchor="end"
                      className="topic-axis-label"
                    >
                      {value}
                    </text>
                  </g>
                );
              })}

              <line
                x1={MARGIN.left}
                y1={MARGIN.top + INNER_HEIGHT}
                x2={CHART_WIDTH - MARGIN.right}
                y2={MARGIN.top + INNER_HEIGHT}
                className="topic-axis"
              />
              <line
                x1={MARGIN.left}
                y1={MARGIN.top}
                x2={MARGIN.left}
                y2={MARGIN.top + INNER_HEIGHT}
                className="topic-axis"
              />

              {series.map((item, index) => {
                const color = palette[index % palette.length];
                return (
                  <g key={`${title}-${item.topic}`}>
                    <path
                      d={buildPath(item.points, maxValue)}
                      fill="none"
                      stroke={color}
                      strokeWidth="4"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    />
                    {item.points.map((point, pointIndex) => (
                      <circle
                        key={`${item.topic}-${point.year}`}
                        cx={getX(pointIndex, item.points.length)}
                        cy={getY(point.count || 0, maxValue)}
                        r="4.5"
                        fill={color}
                      />
                    ))}
                  </g>
                );
              })}

              {yearMarkers.map((marker) => (
                <text
                  key={`${title}-year-${marker.year}`}
                  x={getX(marker.index, years.length)}
                  y={CHART_HEIGHT - 20}
                  textAnchor="middle"
                  className="topic-year-label"
                >
                  {marker.year}
                </text>
              ))}
            </svg>
          </div>

          <div className="topic-trend-legend">
            {series.map((item, index) => (
              <div className="topic-legend-row" key={`${title}-legend-${item.topic}`}>
                <span
                  className="legend-swatch"
                  style={{ backgroundColor: palette[index % palette.length] }}
                />
                <span className="legend-label">{item.topic}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </section>
  );
}
