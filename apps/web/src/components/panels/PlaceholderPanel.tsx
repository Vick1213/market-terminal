export function PlaceholderPanel({
  title,
  phase,
  note,
}: {
  title: string;
  phase: string;
  note?: string;
}) {
  return (
    <div className="panel">
      <div className="panel-head">
        <span>{title}</span>
        <span className="badge soon">{phase}</span>
      </div>
      <div className="panel-body">
        <div className="placeholder">
          <span>Wired in {phase}</span>
          {note && <span style={{ maxWidth: 240 }}>{note}</span>}
        </div>
      </div>
    </div>
  );
}
