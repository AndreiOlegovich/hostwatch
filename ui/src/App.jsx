import { useCallback, useEffect, useMemo, useState } from "react";
import { CopyButton } from "./CopyButton.jsx";
import { CardsPage, DisksPage } from "./Devices.jsx";
import { DocsPage } from "./Docs.jsx";
import { dateFormat, getJSON, plural, sameDay, shortDateFormat, timeFormat } from "./lib.js";

const SOURCES = [
  { id: "claude", label: "Claude Code" },
  { id: "opencode", label: "OpenCode" },
];
const SORTS = {
  recent: { label: "Last active", value: (s) => Date.parse(s.ended || 0) },
  commits: { label: "Most commits", value: (s) => s.commits },
  active: { label: "Longest", value: (s) => s.active_seconds },
  prompts: { label: "Most prompts", value: (s) => s.prompts },
};
const VIEWS = [
  { id: "prompts", label: "Prompts" },
  { id: "conversation", label: "Conversation" },
  { id: "all", label: "With tools" },
];

const PAGES = [
  { id: "disks", label: "Disks" },
  { id: "cards", label: "USB cards" },
  { id: "docs", label: "Docs" },
];

function readRoute() {
  const page = window.location.hash.match(/^#\/(disks|cards|docs)$/);
  if (page) return { page: page[1], source: "claude", id: null };
  const match = window.location.hash.match(/^#\/(claude|opencode)(?:\/([A-Za-z0-9_-]+))?$/);
  return { page: "sessions", source: match ? match[1] : "claude", id: match?.[2] ?? null };
}

function useRoute() {
  const [route, setRoute] = useState(readRoute);
  useEffect(() => {
    const onChange = () => setRoute(readRoute());
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return route;
}

function dayLabel(iso) {
  if (!iso) return "Unknown date";
  const day = new Date(iso);
  const today = new Date();
  const yesterday = new Date(today);
  yesterday.setDate(today.getDate() - 1);
  if (sameDay(day, today)) return "Today";
  if (sameDay(day, yesterday)) return "Yesterday";
  return dateFormat.format(day);
}

function timeRange(started, ended) {
  if (!started || !ended) return "";
  const a = new Date(started);
  const b = new Date(ended);
  if (sameDay(a, b)) return `${timeFormat.format(a)}–${timeFormat.format(b)}`;
  return `${shortDateFormat.format(a)} → ${shortDateFormat.format(b)}`;
}

function duration(seconds) {
  if (seconds < 60) return "under a minute";
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.round((seconds % 3600) / 60);
  return hours ? `${hours} h ${minutes} min` : `${minutes} min`;
}

function compact(n) {
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}k`;
  return String(n);
}

function shortPath(path, home) {
  if (!path) return "";
  if (home && (path === home || path.startsWith(`${home}/`))) return `~${path.slice(home.length)}`;
  return path;
}

function relativeTo(file, directory) {
  return directory && file.startsWith(`${directory}/`) ? file.slice(directory.length + 1) : file;
}

function SessionCard({ session, selected, home }) {
  const extraTopics = session.topics.filter((topic) => topic !== session.title).slice(0, 2);
  return (
    <li className={`card${selected ? " selected" : ""}`}>
      <a className="card-link" href={`#/${session.source}/${session.id}`} aria-current={selected ? "page" : undefined}>
        {session.title}
      </a>
      <div className="card-meta">
        <span title={session.directory}>{shortPath(session.directory, home)}</span>
        <span>{timeRange(session.started, session.ended)}</span>
        <span>{duration(session.active_seconds)}</span>
      </div>
      {extraTopics.length > 0 && (
        <ul className="card-topics">
          {extraTopics.map((topic) => (
            <li key={topic}>{topic}</li>
          ))}
        </ul>
      )}
      <div className="card-stats">
        <span>{plural(session.prompts, "prompt")}</span>
        <span className={session.commits ? "has-commits" : ""}>{plural(session.commits, "commit")}</span>
        <span>{plural(session.files_changed, "file")}</span>
        {session.archived && <span className="tag">archived</span>}
        <CopyButton text={session.resume} label="Copy command" className="btn small copy" />
      </div>
    </li>
  );
}

function SessionList({ sessions, sort, selected, home }) {
  if (sort !== "recent") {
    return (
      <ul className="cards">
        {sessions.map((session) => (
          <SessionCard key={session.id} session={session} selected={session.id === selected} home={home} />
        ))}
      </ul>
    );
  }
  const groups = [];
  for (const session of sessions) {
    const label = dayLabel(session.ended);
    if (groups.at(-1)?.label !== label) groups.push({ label, sessions: [] });
    groups.at(-1).sessions.push(session);
  }
  return groups.map((group) => (
    <section key={group.label}>
      <h2 className="day">{group.label}</h2>
      <ul className="cards">
        {group.sessions.map((session) => (
          <SessionCard key={session.id} session={session} selected={session.id === selected} home={home} />
        ))}
      </ul>
    </section>
  ));
}

function LongText({ text, limit = 900 }) {
  const [open, setOpen] = useState(false);
  if (text.length <= limit) return <div className="text">{text}</div>;
  return (
    <div className="text">
      {open ? text : `${text.slice(0, limit)}…`}{" "}
      <button type="button" className="link" onClick={() => setOpen(!open)}>
        {open ? "Show less" : "Show all"}
      </button>
    </div>
  );
}

function TranscriptRow({ item }) {
  const time = item.time ? timeFormat.format(new Date(item.time)) : "";
  if (item.kind === "tool") {
    return (
      <li className="row tool">
        <span className="tool-name">{item.name}</span>
        <code title={item.text}>{item.text}</code>
      </li>
    );
  }
  if (item.kind === "event") return <li className="row event">{item.text}</li>;
  if (item.kind === "command") {
    return (
      <li className="row command">
        <code>{item.text}</code>
        <time>{time}</time>
      </li>
    );
  }
  return (
    <li className={`row ${item.kind}`}>
      <div className="row-head">
        <span className="who">{item.kind === "user" ? "You" : "Assistant"}</span>
        <time>{time}</time>
      </div>
      <LongText text={item.text} />
    </li>
  );
}

function Transcript({ items }) {
  const [view, setView] = useState("conversation");
  const shown = items.filter((item) => {
    if (view === "all") return true;
    if (view === "prompts") return item.kind === "user" || item.kind === "command";
    return item.kind !== "tool";
  });
  return (
    <section>
      <div className="section-head">
        <h2>Transcript</h2>
        <div className="segmented" role="group" aria-label="Transcript view">
          {VIEWS.map((option) => (
            <button key={option.id} type="button" aria-pressed={view === option.id} onClick={() => setView(option.id)}>
              {option.label}
            </button>
          ))}
        </div>
      </div>
      {shown.length === 0 ? (
        <p className="muted">Nothing to show in this view.</p>
      ) : (
        <ol className="transcript">
          {shown.map((item, index) => (
            <TranscriptRow key={index} item={item} />
          ))}
        </ol>
      )}
    </section>
  );
}

function CommitList({ commits, directory, graceMinutes }) {
  if (commits.length === 0) {
    return <p className="muted">No commits: none in the transcript, none in the repository while the session was active.</p>;
  }
  const repos = new Set(commits.map((commit) => commit.repo).filter(Boolean));
  return (
    <>
      <ul className="commits">
        {commits.map((commit) => (
          <li key={commit.hash}>
            <code className="hash" title={commit.hash}>
              {commit.short}
            </code>
            <span className="subject">{commit.subject}</span>
            <span className="tags">
              {commit.in_transcript && <span className="tag agent">in transcript</span>}
              {commit.in_window && !commit.in_transcript && <span className="tag">during session</span>}
              {!commit.in_history && <span className="tag gone">not in history</span>}
            </span>
            <span className="commit-meta">
              {commit.time && `${dayLabel(commit.time)} ${timeFormat.format(new Date(commit.time))}`}
              {commit.author && ` · ${commit.author}`}
              {repos.size > 1 && commit.repo && ` · ${relativeTo(commit.repo, directory)}`}
            </span>
          </li>
        ))}
      </ul>
      <p className="hint">
        “In transcript”: git printed the commit inside the session. “During session”: a commit on a local branch of a
        repository the session worked in, made while it was active or up to {graceMinutes} min after.
      </p>
    </>
  );
}

function SessionDetail({ source, id, home, graceMinutes }) {
  const [state, setState] = useState({ loading: true });
  const [allFiles, setAllFiles] = useState(false);
  useEffect(() => {
    let live = true;
    setState({ loading: true });
    getJSON(`/api/sessions/${source}/${id}`).then(
      (data) => live && setState({ data }),
      (error) => live && setState({ error: error.message }),
    );
    return () => {
      live = false;
    };
  }, [source, id]);

  const back = (
    <a className="back" href={`#/${source}`}>
      ← All sessions
    </a>
  );
  if (state.loading) return <div className="detail">{back}<p className="muted">Reading the session…</p></div>;
  if (state.error) return <div className="detail">{back}<p className="error">{state.error}</p></div>;

  const s = state.data;
  const tokensIn = (s.tokens.input || 0) + (s.tokens.cache_read || 0) + (s.tokens.cache_write || 0);
  const tokenTitle = Object.entries(s.tokens)
    .map(([key, value]) => `${key.replace("_", " ")}: ${value.toLocaleString()}`)
    .join("\n");
  const files = allFiles ? s.files : s.files.slice(0, 16);
  return (
    <article className="detail">
      {back}
      <header>
        <p className="eyebrow">
          {SOURCES.find((item) => item.id === source)?.label} · {dayLabel(s.started)} {timeRange(s.started, s.ended)}
          {s.archived && " · archived"}
        </p>
        <h1>{s.title}</h1>
        <p className="meta">
          <span title={s.directory}>{shortPath(s.directory, home)}</span>
          {s.branch && (
            <>
              {" "}
              on <code>{s.branch}</code>
            </>
          )}
          {" · "}active {duration(s.active_seconds)}
          {s.models.length > 0 && ` · ${s.models.join(", ")}`}
        </p>
      </header>

      <section className="resume" aria-label="Resume">
        <div className="command">
          <code>{s.resume}</code>
          <CopyButton text={s.resume} label="Copy" className="btn primary" />
        </div>
        <p className="hint">Paste it into a terminal on this machine to continue the session where it stopped.</p>
      </section>

      <dl className="stats">
        <div>
          <dt>Prompts</dt>
          <dd>{s.prompts}</dd>
        </div>
        <div>
          <dt>Commits</dt>
          <dd>{s.commits}</dd>
        </div>
        <div>
          <dt>Files changed</dt>
          <dd>{s.files_changed}</dd>
        </div>
        <div>
          <dt>Tool calls</dt>
          <dd>{s.tool_calls}</dd>
        </div>
        <div title={tokenTitle}>
          <dt>Tokens in / out</dt>
          <dd>
            {compact(tokensIn)} / {compact(s.tokens.output || 0)}
          </dd>
        </div>
        {s.cost !== null && (
          <div>
            <dt>Cost</dt>
            <dd>${s.cost.toFixed(2)}</dd>
          </div>
        )}
        {s.subagents > 0 && (
          <div>
            <dt>Subagents</dt>
            <dd>{s.subagents}</dd>
          </div>
        )}
      </dl>

      <section>
        <h2>Topics</h2>
        {s.topics.length ? (
          <ol className="topics">
            {s.topics.map((topic) => (
              <li key={topic}>{topic}</li>
            ))}
          </ol>
        ) : (
          <p className="muted">No prompts.</p>
        )}
      </section>

      <section>
        <h2>Commits</h2>
        <CommitList commits={s.commit_list} directory={s.directory} graceMinutes={graceMinutes} />
      </section>

      {s.files.length > 0 && (
        <section>
          <h2>Files changed</h2>
          <ul className="files">
            {files.map((file) => (
              <li key={file} title={file}>
                {relativeTo(file, s.directory)}
              </li>
            ))}
          </ul>
          {s.files.length > 16 && (
            <button type="button" className="link" onClick={() => setAllFiles(!allFiles)}>
              {allFiles ? "Show fewer" : `Show all ${s.files.length}`}
            </button>
          )}
        </section>
      )}

      {s.tools.length > 0 && (
        <section>
          <h2>Tools</h2>
          <ul className="chips">
            {s.tools.map(([name, count]) => (
              <li key={name} className="chip">
                {name} <span>{count}</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      <Transcript items={s.transcript} />
    </article>
  );
}

export default function App() {
  const route = useRoute();
  const [info, setInfo] = useState(null);
  const [lists, setLists] = useState({});
  const [query, setQuery] = useState("");
  const [project, setProject] = useState("");
  const [sort, setSort] = useState("recent");

  const load = useCallback((source) => {
    setLists((prev) => ({ ...prev, [source]: { sessions: [], ...prev[source], loading: true } }));
    getJSON(`/api/sessions/${source}`).then(
      (data) => setLists((prev) => ({ ...prev, [source]: { loading: false, error: data.error, sessions: data.sessions } })),
      (error) =>
        setLists((prev) => ({ ...prev, [source]: { ...prev[source], loading: false, error: error.message } })),
    );
  }, []);
  const loadAll = useCallback(() => SOURCES.forEach((source) => load(source.id)), [load]);

  useEffect(() => {
    getJSON("/api/info").then(setInfo, () => setInfo({ home: "", grace_minutes: 30 }));
    loadAll();
  }, [loadAll]);
  useEffect(() => setProject(""), [route.source]);

  const current = lists[route.source] || { loading: true, sessions: [] };
  const home = info?.home || "";
  const projects = useMemo(
    () => [...new Set(current.sessions.map((session) => session.directory))].sort(),
    [current.sessions],
  );
  const visible = useMemo(() => {
    const words = query.toLowerCase().split(/\s+/).filter(Boolean);
    const value = SORTS[sort].value;
    return current.sessions
      .filter((session) => !project || session.directory === project)
      .filter((session) => {
        if (!words.length) return true;
        const haystack = [session.title, session.directory, session.id, ...session.topics].join("\n").toLowerCase();
        return words.every((word) => haystack.includes(word));
      })
      .sort((a, b) => value(b) - value(a) || Date.parse(b.ended || 0) - Date.parse(a.ended || 0));
  }, [current.sessions, project, query, sort]);

  const sessions = route.page === "sessions";
  return (
    <div className={`app${route.id ? " has-detail" : ""}`}>
      <header className="topbar">
        <a className="brand" href={`#/${route.source}`}>
          Hostwatch
        </a>
        <nav className="tabs" aria-label="Sections">
          {SOURCES.map((source) => (
            <a
              key={source.id}
              href={`#/${source.id}`}
              aria-current={sessions && route.source === source.id ? "page" : undefined}
            >
              {source.label}
              <span className="count">{lists[source.id]?.sessions?.length ?? "…"}</span>
            </a>
          ))}
          <span className="tabs-gap" aria-hidden="true" />
          {PAGES.map((page) => (
            <a key={page.id} href={`#/${page.id}`} aria-current={route.page === page.id ? "page" : undefined}>
              {page.label}
            </a>
          ))}
        </nav>
        <span className="spacer" />
        {sessions && (
          <button type="button" className="btn" onClick={loadAll} disabled={current.loading}>
            {current.loading ? "Reading…" : "Refresh"}
          </button>
        )}
      </header>
      {route.page === "disks" && <DisksPage />}
      {route.page === "cards" && <CardsPage />}
      {route.page === "docs" && <DocsPage />}
      {sessions && <div className="layout">
        <aside className="list-pane" aria-label="Sessions">
          <div className="filters">
            <input
              type="search"
              placeholder="Search titles, prompts, folders"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
            <div className="filter-row">
              <select value={project} onChange={(event) => setProject(event.target.value)} aria-label="Folder">
                <option value="">All folders</option>
                {projects.map((path) => (
                  <option key={path} value={path}>
                    {shortPath(path, home)}
                  </option>
                ))}
              </select>
              <select value={sort} onChange={(event) => setSort(event.target.value)} aria-label="Sort">
                {Object.entries(SORTS).map(([key, option]) => (
                  <option key={key} value={key}>
                    {option.label}
                  </option>
                ))}
              </select>
            </div>
            <p className="muted small">
              {visible.length} of {plural(current.sessions.length, "session")}
            </p>
          </div>
          {current.error && <p className="error pad">{current.error}</p>}
          {current.loading && current.sessions.length === 0 && <p className="muted pad">Reading sessions…</p>}
          <SessionList sessions={visible} sort={sort} selected={route.id} home={home} />
        </aside>
        <main className="detail-pane">
          {route.id ? (
            <SessionDetail
              key={`${route.source}/${route.id}`}
              source={route.source}
              id={route.id}
              home={home}
              graceMinutes={info?.grace_minutes ?? 30}
            />
          ) : (
            <div className="empty">
              <p>Pick a session to see its topics, commits and transcript, and the command that reopens it.</p>
            </div>
          )}
        </main>
      </div>}
    </div>
  );
}
