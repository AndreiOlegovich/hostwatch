import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getJSON, plural, postJSON, sameDay, shortDateFormat, timeFormat } from "./lib.js";

const clockFormat = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
const SEVERITY = { high: "Serious", medium: "Warning", low: "Notice" };
const ISSUES_SHOWN = 20;

function formatBytes(bytes) {
  if (bytes == null) return "";
  const units = ["B", "kB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1000 && unit < units.length - 1) {
    value /= 1000;
    unit += 1;
  }
  return `${value >= 100 || unit === 0 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`;
}

function when(iso) {
  if (!iso) return "";
  const date = new Date(iso);
  const time = timeFormat.format(date);
  return sameDay(date, new Date()) ? `today ${time}` : `${shortDateFormat.format(date)}, ${time}`;
}

function cardTitle(card) {
  return card.labels?.[0] || card.name || card.serial;
}

// Re-reads `url` every `interval` ms while the tab is visible, so plugging a card in shows up by itself.
function usePolling(url, interval) {
  const [state, setState] = useState({ data: null, error: null, updated: null });
  const inFlight = useRef(false);
  const reload = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    try {
      const data = await getJSON(url);
      setState({ data, error: null, updated: new Date() });
    } catch (error) {
      setState((prev) => ({ ...prev, error: error.message }));
    } finally {
      inFlight.current = false;
    }
  }, [url]);
  useEffect(() => {
    reload();
    const timer = setInterval(() => document.hidden || reload(), interval);
    return () => clearInterval(timer);
  }, [reload, interval]);
  return { ...state, reload };
}

function useActions(reload) {
  const [busy, setBusy] = useState(null);
  const [notice, setNotice] = useState(null);
  const run = async (key, url, body) => {
    setBusy(key);
    setNotice(null);
    try {
      const result = await postJSON(url, body);
      setNotice({ key, text: result.message });
    } catch (error) {
      setNotice({ key, text: error.message, error: true });
    } finally {
      setBusy(null);
      reload();
    }
  };
  return { busy, notice, run };
}

function Notice({ notice, keys }) {
  if (!notice || !keys.includes(notice.key)) return null;
  return <p className={`notice${notice.error ? " error" : ""}`}>{notice.text}</p>;
}

function PageHead({ title, updated, children }) {
  return (
    <header className="page-head">
      <h1>{title}</h1>
      <p className="meta">{children}</p>
      {updated && <p className="hint">Updated {clockFormat.format(updated)}, refreshes by itself</p>}
    </header>
  );
}

function Usage({ volume }) {
  if (!volume.fssize || !volume.mountpoints.length) return null;
  const used = Math.min(100, Math.round((volume.fsused / volume.fssize) * 100));
  return (
    <div className="usage" title={`${used}% used`}>
      <div className="usage-bar">
        <span className={used >= 90 ? "full" : ""} style={{ width: `${used}%` }} />
      </div>
      <span className="muted">
        {formatBytes(volume.fsavail)} free of {formatBytes(volume.fssize)}
      </span>
    </div>
  );
}

function MountState({ volume }) {
  if (volume.fstype === "swap") return <span className="tag">swap</span>;
  if (!volume.mountpoints.length) return <span className="tag">not mounted</span>;
  if (volume.readonly) return <span className="tag ro">read-only</span>;
  return <span className="tag rw">writable</span>;
}

function VolumeRow({ volume, actions }) {
  const { busy, notice, run } = actions;
  const mountKey = `mount:${volume.device}`;
  const unmountKey = `unmount:${volume.device}`;
  const mounted = volume.mountpoints.length > 0 && volume.fstype !== "swap";
  return (
    <li className="volume">
      <div className="volume-name">
        <strong>{volume.label || volume.name}</strong>
        <span className="muted">
          <code>{volume.device}</code>
          {volume.kind && ` · ${volume.kind}`}
        </span>
      </div>
      <div className="volume-fs muted">
        {volume.fstype || "no file system"} · {formatBytes(volume.size)}
      </div>
      <div className="volume-where">
        {mounted ? (
          <code>{volume.mountpoints.join(", ")}</code>
        ) : (
          <span className="muted">{volume.fstype === "swap" && volume.mountpoints.length ? "In use" : "Not mounted"}</span>
        )}
        <Usage volume={volume} />
      </div>
      <div className="volume-state">
        <MountState volume={volume} />
      </div>
      <div className="volume-action">
        {volume.can_mount && (
          <button
            type="button"
            className="btn"
            disabled={!!busy}
            onClick={() => run(mountKey, "/api/mount", { device: volume.device })}
          >
            {busy === mountKey ? "Mounting…" : "Mount"}
          </button>
        )}
        {volume.can_unmount && (
          <button
            type="button"
            className="btn"
            disabled={!!busy}
            onClick={() => run(unmountKey, "/api/unmount", { device: volume.device })}
          >
            {busy === unmountKey ? "Unmounting…" : "Unmount"}
          </button>
        )}
        {volume.system && <span className="muted">System</span>}
      </div>
      <Notice notice={notice} keys={[mountKey, unmountKey]} />
    </li>
  );
}

export function DisksPage() {
  const { data, error, updated, reload } = usePolling("/api/disks", 5000);
  const actions = useActions(reload);
  return (
    <main className="page">
      <div className="page-body">
        <PageHead title="Disks" updated={updated}>
          The disks inside this computer and their partitions. Mount opens a partition under /media, the same as
          clicking it in Files; it stays mounted until you unmount it or restart.
        </PageHead>
        {error && <p className="error">{error}</p>}
        {!data && !error && <p className="muted">Reading disks…</p>}
        {data?.disks.map((disk) => (
          <section key={disk.device} className="device">
            <header className="device-head">
              <h2>{disk.model || disk.name}</h2>
              <span className="muted">
                {formatBytes(disk.size)}
                {disk.tran && ` · ${disk.tran.toUpperCase()}`} · <code>{disk.device}</code>
              </span>
            </header>
            <ul className="volumes">
              {disk.volumes.map((volume) => (
                <VolumeRow key={volume.device} volume={volume} actions={actions} />
              ))}
            </ul>
          </section>
        ))}
      </div>
    </main>
  );
}

function Switch({ checked, disabled, label, onChange }) {
  return (
    <label className={`switch${disabled ? " disabled" : ""}`}>
      <span>{label}</span>
      <button type="button" role="switch" aria-checked={checked} disabled={disabled} onClick={() => onChange(!checked)}>
        <span className="knob" />
      </button>
    </label>
  );
}

function PresentCard({ card, actions }) {
  const { busy, notice, run } = actions;
  const readonlyKey = `readonly:${card.serial}`;
  const ejectKey = `eject:${card.serial}`;
  const locked = card.write_protected;
  const checked = locked || (card.readonly_pref ?? card.state === "read-only");
  let hint = "Writable: files on it can be changed or deleted.";
  if (locked) {
    hint = card.protect_switch
      ? "The card itself reports write protection, so nothing can change what is on it. Worn-out cards do this."
      : "The card is read-only at the device level, so it can't be made writable from here.";
  } else if (card.readonly_pref) {
    hint = "Kept read-only: Hostwatch mounts it read-only again whenever it is plugged in or comes back by itself.";
  } else if (checked) {
    hint = "Mounted read-only for now. Switch on to keep it read-only every time it is plugged in.";
  }
  return (
    <article className="card-tile">
      <header className="tile-head">
        <div>
          <h3>{cardTitle(card)}</h3>
          <p className="muted">
            {[
              card.labels.length > 0 && card.name,
              formatBytes(card.size),
              card.device,
              card.speed && `USB ${card.speed}`,
              card.connected && `plugged in ${when(card.connected)}`,
            ]
              .filter(Boolean)
              .join(" · ")}
          </p>
        </div>
        <Switch
          checked={checked}
          disabled={locked || !!busy}
          label={busy === readonlyKey ? "Switching…" : "Read-only"}
          onChange={(value) => run(readonlyKey, "/api/cards/readonly", { serial: card.serial, readonly: value })}
        />
      </header>
      <p className="hint">{hint}</p>
      {card.keep_error && <p className="error">Could not make it read-only again: {card.keep_error}</p>}
      <Notice notice={notice} keys={[readonlyKey]} />
      <ul className="volumes">
        {card.volumes.map((volume) => (
          <VolumeRow key={volume.device} volume={volume} actions={actions} />
        ))}
      </ul>
      <footer className="tile-foot">
        <span className="muted">
          Serial <code>{card.serial}</code>
          {card.issues > 0 && ` · ${plural(card.issues, "issue")} on record`}
        </span>
        <button
          type="button"
          className="btn"
          disabled={!!busy}
          onClick={() => run(ejectKey, "/api/cards/eject", { serial: card.serial })}
        >
          {busy === ejectKey ? "Ejecting…" : "Eject safely"}
        </button>
      </footer>
      <Notice notice={notice} keys={[ejectKey]} />
    </article>
  );
}

function IssueList({ issues, names }) {
  const [all, setAll] = useState(false);
  if (!issues.length) return <p className="muted">The kernel log has no problems with USB cards.</p>;
  const shown = all ? issues : issues.slice(0, ISSUES_SHOWN);
  return (
    <>
      <ul className="issues">
        {shown.map((issue) => (
          <li key={issue.id} className={`issue ${issue.severity}`}>
            <div className="issue-head">
              <span className={`tag sev-${issue.severity}`}>{SEVERITY[issue.severity]}</span>
              <strong>{issue.title}</strong>
              <span className="muted issue-when">
                {names[issue.serial] || issue.card} · {when(issue.started)}
              </span>
            </div>
            {issue.details.length > 0 && <p className="issue-details">{issue.details.join(" · ")}</p>}
            {issue.lines.length > 0 && (
              <details>
                <summary>Kernel log</summary>
                <pre>
                  {issue.lines.join("\n")}
                  {issue.more_lines > 0 && `\n… and ${issue.more_lines} more lines`}
                </pre>
              </details>
            )}
          </li>
        ))}
      </ul>
      {issues.length > ISSUES_SHOWN && (
        <button type="button" className="link" onClick={() => setAll(!all)}>
          {all ? "Show fewer" : `Show all ${issues.length}`}
        </button>
      )}
    </>
  );
}

function KnownList({ cards }) {
  if (!cards.length) return <p className="muted">No USB card in the kernel log yet.</p>;
  return (
    <ul className="known">
      {cards.map((card) => (
        <li key={card.serial}>
          <details>
            <summary>
              <span className="known-name">
                <strong>{cardTitle(card)}</strong>
                {card.labels.length > 0 && card.name && <span className="muted"> {card.name}</span>}
              </span>
              <span className="muted">{formatBytes(card.size)}</span>
              <span className="muted">{plural(card.insertions, "connection")}</span>
              <span className={card.issues ? "issue-count" : "muted"}>{plural(card.issues, "issue")}</span>
              <span>
                {card.present ? (
                  <span className="tag rw">plugged in</span>
                ) : (
                  <span className="muted">last {when(card.last_seen)}</span>
                )}
              </span>
            </summary>
            <div className="known-body">
              <p className="muted">
                Serial <code>{card.serial}</code> · first seen {when(card.first_seen)}
                {card.write_protected && " · reported write protection"}
                {card.readonly_pref && " · kept read-only"}
              </p>
              {card.connections.length > 0 && (
                <ol className="connections">
                  {card.connections.map((conn) => (
                    <li key={conn.connected}>
                      {when(conn.connected)} →{" "}
                      {conn.disconnected
                        ? when(conn.disconnected)
                        : conn.boot_ended
                          ? `${when(conn.boot_ended)} (computer shut down)`
                          : "now"}
                      {conn.device && <span className="muted"> · {conn.device}</span>}
                    </li>
                  ))}
                </ol>
              )}
            </div>
          </details>
        </li>
      ))}
    </ul>
  );
}

export function CardsPage() {
  const { data, error, updated, reload } = usePolling("/api/cards", 3000);
  const actions = useActions(reload);
  const names = useMemo(
    () => Object.fromEntries((data?.known || []).map((card) => [card.serial, cardTitle(card)])),
    [data],
  );
  return (
    <main className="page">
      <div className="page-body">
        <PageHead title="USB cards" updated={updated}>
          USB flash drives plugged in now, every one the kernel log remembers, and the problems it reported with them.
        </PageHead>
        {error && <p className="error">{error}</p>}
        {data?.log_error && <p className="error">{data.log_error}</p>}
        {!data && !error && <p className="muted">Reading cards and the kernel log…</p>}
        {data && (
          <>
            <section>
              <h2>Inserted now</h2>
              {data.present.length ? (
                data.present.map((card) => <PresentCard key={card.serial} card={card} actions={actions} />)
              ) : (
                <p className="muted">No USB card is plugged in.</p>
              )}
            </section>
            <section>
              <h2>
                Issues <span className="count">{data.issues.length}</span>
              </h2>
              {data.history_loading && (
                <p className="hint">Reading older boots from the kernel log; the first start takes about a minute.</p>
              )}
              <p className="hint">
                From the kernel log. Counts are what the kernel wrote down: after about ten of the same error it stops
                logging each one.
              </p>
              <IssueList issues={data.issues} names={names} />
            </section>
            <section>
              <h2>
                All cards seen <span className="count">{data.known.length}</span>
              </h2>
              <KnownList cards={data.known} />
            </section>
          </>
        )}
      </div>
    </main>
  );
}
