import LoadingSpinner from "./LoadingSpinner";

export default function DataTable({
  title,
  columns,
  rows,
  loading = false,
  headerAction = null,
  className = "",
}) {
  return (
    <section className={`panel ${className}`.trim()}>
      <div className="panel-header">
        <h2>{title}</h2>
        {headerAction}
      </div>
      {loading ? (
        <LoadingSpinner label="Loading..." />
      ) : rows.length === 0 ? (
        <p className="empty-state">No data available yet.</p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                {columns.map((column) => (
                  <th key={column.key}>{column.label}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, index) => (
                <tr key={`${title}-${index}`}>
                  {columns.map((column) => (
                    <td key={column.key}>{column.render ? column.render(row) : row[column.key]}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
