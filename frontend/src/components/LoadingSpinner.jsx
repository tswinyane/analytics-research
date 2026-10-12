export default function LoadingSpinner({ label = "Loading..." }) {
  return (
    <div className="loading-state" role="status" aria-live="polite">
      <span className="loading-spinner" />
      <span>{label}</span>
    </div>
  );
}