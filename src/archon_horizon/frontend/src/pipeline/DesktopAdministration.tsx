import { useEffect, useRef, useState, type ReactNode } from "react";
import {
  Bot,
  Code2,
  Cpu,
  FileText,
  RefreshCw,
  Search,
  Server,
  Settings2,
  ShieldCheck,
  UserRound,
  X,
} from "lucide-react";
import {
  type Account,
  type Project,
  type Resources as ResourceData,
} from "./api";
import { usePagedRead, useRead } from "./queries";
import { ErrorNotice, RichText, Time } from "./shared";
import {
  HarnessEditor,
  MachineEditor,
  SaveActions,
  useSave,
  type Harness,
  type Machine,
} from "./Administration";
import "../components/agents.css";
import "../components/accounts.css";
import "./DesktopAdministration.css";
import InstructionCatalog from "./InstructionCatalog";

type Props = {
  accountId: string;
  admin: boolean;
  writable: boolean;
  projectId?: string;
};
type Reviewer = {
  id: string;
  revision: number;
  slug: string;
  enabled: boolean;
  instructions: string;
  invocation: string;
  functions: string[];
  harness_id: string | null;
  model_options: { model?: string; reasoning_effort?: string };
};

function Button({
  title,
  onClick,
  disabled,
  children,
}: {
  title: string;
  onClick: () => void;
  disabled?: boolean;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      className="platform-icon-button"
      title={title}
      aria-label={title}
      onClick={onClick}
      disabled={disabled}
    >
      {children}
    </button>
  );
}
function Status({ value }: { value: string }) {
  return (
    <span
      className={`platform-status ${["enabled", "online", "admin", "maintainer"].includes(value) ? "good" : ["unavailable", "storage_pressure"].includes(value) ? "bad" : "quiet"}`}
    >
      {value.replace(/_/g, " ")}
    </span>
  );
}
function Dialog({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    ref.current?.showModal();
  }, []);
  return (
    <dialog
      ref={ref}
      className="platform-modal desktop-admin-dialog"
      aria-label={title}
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
    >
      <div className="platform-modal-head">
        <h2>{title}</h2>
        <Button title="Close dialog" onClick={onClose}>
          <X size={17} />
        </Button>
      </div>
      {children}
    </dialog>
  );
}

export function DesktopHosts({ accountId, admin, writable }: Props) {
  const resource = useRead<ResourceData>(
    accountId,
    "resources",
    "global",
    "/resources",
    admin,
  );
  const machines = usePagedRead<Machine>(
    accountId,
    "settings",
    "global",
    "/records/host?limit=100",
    admin,
  );
  const [editing, setEditing] = useState("");
  const hosts = machines.data?.pages.flatMap((page) => page.items) || [];
  const selected = hosts.find((host) => host.id === editing);
  const data = resource.data;
  const occupied =
    data?.hosts.reduce((sum, host) => sum + Number(host.occupied_slots), 0) ||
    0;
  const total =
    data?.hosts.reduce((sum, host) => sum + Number(host.slots), 0) || 0;
  if (!admin)
    return (
      <div className="platform-empty">
        Execution host administration requires an administrator account.
      </div>
    );
  return (
    <section className="desktop-administration" aria-label="Execution hosts">
      <div className="platform-toolbar">
        <div className="platform-hosts-overview">
          <span>
            {data?.hosts.length || 0} hosts{" "}
            <span className="platform-text-divider">/</span> {occupied} active
            sessions
          </span>
          <div className="platform-global-capacity">
            <span>Global capacity</span>
            <span className="platform-global-quota">
              <span>Sessions</span>
              <b>{total}</b>
            </span>
          </div>
        </div>
        <Button
          title="Refresh hosts"
          onClick={() => {
            void resource.refetch();
            void machines.refetch();
          }}
          disabled={resource.isFetching}
        >
          <RefreshCw size={15} />
        </Button>
      </div>
      <ErrorNotice error={resource.error || machines.error} stale={!!data} />
      <div className="platform-hosts">
        {data?.hosts.map((host) => (
          <section className="platform-machine" key={host.id}>
            <div className="platform-machine-head">
              <Server size={18} />
              <div className="platform-machine-name">
                <h2>{host.name}</h2>
                <small>
                  {hosts.find((item) => item.id === host.id)?.slug || "Worker"}
                </small>
              </div>
              <div className="platform-machine-stats desktop-host-stats">
                <div className="platform-machine-stat">
                  <div className="platform-machine-stat-copy">
                    <span>Sessions</span>
                    <b>
                      {host.occupied_slots} / {host.slots}
                    </b>
                  </div>
                  <span className="platform-resource-meter">
                    <span
                      style={{
                        width: `${Math.min(100, (100 * host.occupied_slots) / Math.max(host.slots, 1))}%`,
                      }}
                    />
                  </span>
                </div>
                <div className="platform-machine-stat">
                  <span className="platform-machine-stat-copy">Heartbeat</span>
                  <Time value={host.heartbeat_at} />
                </div>
              </div>
              <Status
                value={host.status === "enabled" ? "online" : host.status}
              />
              <div className="platform-host-actions">
                <Button
                  title={`Edit ${host.name}`}
                  onClick={() => setEditing(host.id)}
                  disabled={
                    !writable || !hosts.some((item) => item.id === host.id)
                  }
                >
                  <Settings2 size={16} />
                </Button>
              </div>
            </div>
            <div className="platform-machine-slots">
              <span className="platform-global-quota">
                <span>Scheduler slots</span>
                <b>{host.slots}</b>
              </span>
              <div className="platform-host-slot-group">
                <span className="platform-host-slot-label">Sessions</span>
                <div
                  className="platform-host-slots"
                  aria-label={`${host.name}: ${host.occupied_slots} active sessions`}
                >
                  {Array.from(
                    { length: Math.min(host.slots, 32) },
                    (_, index) => (
                      <span
                        key={index}
                        className={`platform-slot ${index < host.occupied_slots ? "busy" : ""}`}
                        title={
                          index < host.occupied_slots
                            ? "Occupied slot"
                            : "Unoccupied slot"
                        }
                      />
                    ),
                  )}
                </div>
              </div>
              <span className="platform-host-sampled">
                {host.detail ||
                  `${Math.max(0, host.slots - host.occupied_slots)} slots unoccupied`}
              </span>
            </div>
          </section>
        ))}
      </div>
      {resource.isPending ? (
        <div className="platform-empty">Loading execution hosts...</div>
      ) : (
        !data?.hosts.length && (
          <div className="platform-empty">
            {resource.error
              ? "Execution hosts are unavailable."
              : "No execution hosts registered"}
          </div>
        )
      )}
      {machines.hasNextPage && (
        <button
          className="platform-text-button"
          onClick={() => void machines.fetchNextPage()}
        >
          Load more host configurations
        </button>
      )}
      {!!data?.limits?.length && (
        <div className="platform-section-head">
          <div className="platform-global-capacity">
            <span>Shared quotas</span>
            {data.limits.map((limit) => (
              <span key={limit.id} className="platform-global-quota">
                <span>{limit.slug}</span>
                <b>
                  {limit.occupied} / {limit.max_concurrent}
                </b>
              </span>
            ))}
          </div>
        </div>
      )}
      {selected && (
        <Dialog
          title={`Edit execution host: ${selected.display_name}`}
          onClose={() => setEditing("")}
        >
          <MachineEditor
            host={selected}
            accountId={accountId}
            writable={writable}
          />
        </Dialog>
      )}
    </section>
  );
}

export function DesktopAgents({
  accountId,
  admin,
  writable,
  projectId = "",
}: Props) {
  const [kind, setKind] = useState<"harnesses" | "reviewers" | "skills" | "descriptors">("harnesses");
  const [selected, setSelected] = useState("");
  const [search, setSearch] = useState("");
  const [chosenProject, setChosenProject] = useState(projectId);
  const [reviewerDirty, setReviewerDirty] = useState(false);
  const [reviewerBusy, setReviewerBusy] = useState(false);
  const projects = usePagedRead<Project>(
    accountId,
    "projects",
    "global",
    "/projects?limit=100",
    kind === "reviewers",
  );
  const availableProjects =
    projects.data?.pages.flatMap((page) => page.items) || [];
  const reviewerProject =
    chosenProject || projectId || availableProjects[0]?.id || "";
  const discardReviewer = () =>
    !reviewerBusy &&
    (!reviewerDirty || window.confirm("Discard unsaved reviewer changes?"));
  const harnesses = usePagedRead<Harness>(
    accountId,
    "settings",
    "global",
    "/records/harness?limit=100",
    admin,
  );
  const reviewers = usePagedRead<Reviewer>(
    accountId,
    "settings",
    reviewerProject,
    `/records/reviewer_descriptor?project_id=${encodeURIComponent(reviewerProject)}&limit=100`,
    !!reviewerProject && kind === "reviewers",
  );
  const allHarnesses =
    harnesses.data?.pages.flatMap((page) => page.items) || [];
  const allReviewers =
    reviewers.data?.pages.flatMap((page) => page.items) || [];
  const items = (kind === "harnesses" ? allHarnesses : allReviewers).filter(
    (item) => item.slug.toLowerCase().includes(search.toLowerCase()),
  );
  const active = items.find((item) => item.id === selected) || items[0];
  const harness =
    kind === "harnesses"
      ? allHarnesses.find((item) => item.id === active?.id)
      : undefined;
  const reviewer =
    kind === "reviewers"
      ? allReviewers.find((item) => item.id === active?.id)
      : undefined;
  const query = kind === "harnesses" ? harnesses : reviewers;
  return (
    <section
      className="horizon-capabilities desktop-administration"
      aria-label="Agent capabilities"
    >
      <div
        className="capability-tabs"
        role="tablist"
        aria-label="Capability categories"
      >
        <button
          role="tab"
          aria-selected={kind === "harnesses"}
          disabled={reviewerBusy}
          onClick={() => {
            if (!discardReviewer()) return;
            setKind("harnesses");
            setSelected("");
            setSearch("");
          }}
        >
          <Cpu size={16} />
          Harnesses
        </button>
        <button
          role="tab"
          aria-selected={kind === "reviewers"}
          onClick={() => {
            setKind("reviewers");
            setSelected("");
            setSearch("");
          }}
        >
          <Bot size={16} />
          Reviewers
        </button>
        {admin && (["skills", "descriptors"] as const).map(value => <button key={value} role="tab" aria-selected={kind === value} disabled={reviewerBusy}
          onClick={() => {if (discardReviewer()) {setKind(value); setSelected(""); setSearch("");}}}>
          {value === "skills" ? <FileText size={16}/> : <Bot size={16}/>}{value === "skills" ? "Skills" : "Subagent library"}
        </button>)}
      </div>
      {kind === "skills" || kind === "descriptors" ? <InstructionCatalog key={kind} accountId={accountId} mode={kind}/> : <>
      <div className="capability-toolbar">
        {kind === "reviewers" && (
          <label className="desktop-reviewer-project">
            <span>Project</span>
            <select
              aria-label="Reviewer project"
              value={reviewerProject}
              disabled={reviewerBusy || !availableProjects.length}
              onChange={(event) => {
                if (!discardReviewer()) return;
                setChosenProject(event.target.value);
                setSelected("");
                setSearch("");
              }}
            >
              {!availableProjects.length && (
                <option value="">
                  {projects.isPending
                    ? "Loading projects..."
                    : "No accessible projects"}
                </option>
              )}
              {reviewerProject &&
                !availableProjects.some(
                  (project) => project.id === reviewerProject,
                ) && <option value={reviewerProject}>Current project</option>}
              {availableProjects.map((project) => (
                <option key={project.id} value={project.id}>
                  {project.title}
                </option>
              ))}
            </select>
          </label>
        )}
        {kind === "reviewers" && projects.hasNextPage && (
          <button
            className="capability-text-button"
            onClick={() => void projects.fetchNextPage()}
            disabled={projects.isFetchingNextPage}
          >
            More projects
          </button>
        )}
        <label className="capability-search">
          <Search size={15} />
          <input
            type="search"
            aria-label="Search agent configurations"
            placeholder={`Search ${kind}`}
            value={search}
            disabled={reviewerBusy || reviewerDirty}
            onChange={(event) => setSearch(event.target.value)}
          />
        </label>
        <span className="capability-library-location">
          {kind === "reviewers" ? "Project reviewers" : "Installed harnesses"}
        </span>
        <Button
          title="Refresh agent configurations"
          onClick={() => void query.refetch()}
          disabled={query.isFetching}
        >
          <RefreshCw size={15} />
        </Button>
      </div>
      <ErrorNotice error={query.error} stale={!!query.data} />
      {kind === "reviewers" && (
        <ErrorNotice error={projects.error} stale={!!projects.data} />
      )}
      <div className="capability-workspace">
        <aside
          className="capability-directory"
          aria-label={kind === "harnesses" ? "Harnesses" : "Reviewers"}
        >
          <ul>
            {items.map((item) => (
              <li key={item.id}>
                <button
                  className={active?.id === item.id ? "selected" : ""}
                  aria-current={active?.id === item.id ? "true" : undefined}
                  disabled={reviewerBusy}
                  onClick={() => {
                    if (discardReviewer()) setSelected(item.id);
                  }}
                >
                  <span className="capability-entry-name">
                    {kind === "harnesses" ? (
                      <Cpu size={15} />
                    ) : (
                      <Bot size={15} />
                    )}
                    <strong>{item.slug}</strong>
                  </span>
                  <span
                    className={`capability-state ${item.enabled ? "enabled" : "disabled"}`}
                  >
                    {item.enabled ? "Enabled" : "Disabled"}
                  </span>
                </button>
              </li>
            ))}
          </ul>
          {query.hasNextPage && (
            <button
              className="capability-text-button"
              onClick={() => void query.fetchNextPage()}
            >
              Load more
            </button>
          )}
        </aside>
        <article className="capability-detail">
          {harness ? (
            <HarnessEditor
              key={harness.id}
              harness={harness}
              accountId={accountId}
              writable={admin && writable}
            />
          ) : reviewer ? (
            <ReviewerEditor
              key={reviewer.id}
              {...{ reviewer, accountId }}
              harnesses={allHarnesses}
              writable={admin && writable}
              onDirtyChange={setReviewerDirty}
              onBusyChange={setReviewerBusy}
            />
          ) : (
            <div className="capability-empty">
              {kind === "harnesses" && !admin
                ? "Harness administration requires an administrator account."
                : kind === "reviewers" && !reviewerProject
                  ? projects.isPending
                    ? "Loading projects..."
                    : "No accessible projects."
                  : query.isPending
                    ? "Loading configurations..."
                    : query.error
                      ? "Configurations are unavailable."
                      : search
                        ? "No matching configurations"
                        : kind === "reviewers"
                          ? "No reviewers configured for this project."
                          : "No harnesses configured."}
            </div>
          )}
        </article>
      </div>
      </>}
    </section>
  );
}

function ReviewerEditor({
  reviewer,
  accountId,
  harnesses,
  writable,
  onDirtyChange,
  onBusyChange,
}: {
  reviewer: Reviewer;
  accountId: string;
  harnesses: Harness[];
  writable: boolean;
  onDirtyChange: (value: boolean) => void;
  onBusyChange: (value: boolean) => void;
}) {
  const [draft, setDraft] = useState(reviewer);
  const [baseline, setBaseline] = useState(JSON.stringify(reviewer));
  const [view, setView] = useState("preview");
  const action = useSave(accountId, `reviewer.${reviewer.id}`);
  const dirty = JSON.stringify(draft) !== baseline;
  useEffect(() => {
    onDirtyChange(dirty);
    return () => onDirtyChange(false);
  }, [dirty, onDirtyChange]);
  useEffect(() => {
    onBusyChange(action.busy || action.uncertain);
    return () => onBusyChange(false);
  }, [action.busy, action.uncertain, onBusyChange]);
  const update = (changes: Partial<Reviewer>) => {
    action.reset();
    setDraft((previous) => ({ ...previous, ...changes }));
  };
  const reload = () => {
    setDraft(reviewer);
    setBaseline(JSON.stringify(reviewer));
    action.reset();
  };
  return (
    <>
      <header className="capability-detail-heading">
        <div>
          <span className="capability-kind">Reviewer</span>
          <h2>{reviewer.slug}</h2>
        </div>
        <Status value={reviewer.enabled ? "enabled" : "disabled"} />
      </header>
      <div className="capability-file-metadata">
        <span>
          {reviewer.invocation === "subrequest"
            ? "Maintainer subagent"
            : "Queued assignment"}
        </span>
        {reviewer.functions.map((value) => (
          <span key={value}>{value}</span>
        ))}
        {dirty && <span className="capability-unsaved">Unsaved changes</span>}
      </div>
      <div className="capability-editor-toolbar">
        <div role="tablist" aria-label="Reviewer definition">
          <button
            type="button"
            role="tab"
            aria-selected={view === "preview"}
            onClick={() => setView("preview")}
          >
            <FileText size={15} />
            Preview
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={view === "configuration"}
            onClick={() => setView("configuration")}
          >
            <Code2 size={15} />
            {writable ? "Edit" : "Configuration"}
          </button>
        </div>
      </div>
      {view === "preview" ? (
        <div className="capability-preview">
          <RichText>{draft.instructions}</RichText>
        </div>
      ) : (
        <form
          className="pl-admin-form desktop-reviewer-form"
          onSubmit={(event) => {
            event.preventDefault();
            if (!writable) return;
            void action
              .save<Reviewer>(`/records/reviewer_descriptor/${reviewer.id}`, {
                expected_revision: draft.revision,
                changes: {
                  instructions: draft.instructions,
                  enabled: draft.enabled,
                  invocation: draft.invocation,
                  harness_id: draft.harness_id,
                  model_options: Object.fromEntries(
                    Object.entries(draft.model_options || {}).filter(
                      ([, value]) => value?.trim(),
                    ),
                  ),
                },
              })
              .then((result) => {
                if (result) {
                  setDraft(result);
                  setBaseline(JSON.stringify(result));
                }
              });
          }}
        >
          <fieldset
            className="pl-admin-fields"
            disabled={!writable || action.busy || action.uncertain}
          >
            <label className="pl-admin-check pl-admin-wide">
              <input
                type="checkbox"
                checked={draft.enabled}
                onChange={(event) => update({ enabled: event.target.checked })}
              />
              Enabled
            </label>
            <label>
              Invocation
              <select
                aria-label="Reviewer invocation"
                value={draft.invocation}
                onChange={(event) => update({ invocation: event.target.value })}
              >
                <option value="subrequest">Maintainer subagent</option>
                <option value="assignment">Queued assignment</option>
              </select>
            </label>
            <label>
              Harness
              <select
                aria-label="Reviewer harness"
                value={draft.harness_id || ""}
                onChange={(event) =>
                  update({ harness_id: event.target.value || null })
                }
              >
                <option value="">Inherit from session</option>
                {draft.harness_id &&
                  !harnesses.some(
                    (harness) => harness.id === draft.harness_id,
                  ) && (
                    <option value={draft.harness_id}>Current harness</option>
                  )}
                {harnesses.map((harness) => (
                  <option value={harness.id} key={harness.id}>
                    {harness.slug}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Default model
              <input
                aria-label="Reviewer model"
                placeholder="Inherit from session"
                value={draft.model_options?.model || ""}
                onChange={(event) =>
                  update({
                    model_options: {
                      ...draft.model_options,
                      model: event.target.value,
                    },
                  })
                }
              />
            </label>
            <label>
              Reasoning effort
              <input
                aria-label="Reviewer reasoning effort"
                placeholder="Inherit from session"
                value={draft.model_options?.reasoning_effort || ""}
                onChange={(event) =>
                  update({
                    model_options: {
                      ...draft.model_options,
                      reasoning_effort: event.target.value,
                    },
                  })
                }
              />
            </label>
            <label className="pl-admin-wide">
              Instructions
              <textarea
                aria-label="Reviewer instructions"
                rows={18}
                required
                spellCheck={false}
                value={draft.instructions}
                onChange={(event) =>
                  update({ instructions: event.target.value })
                }
              />
            </label>
          </fieldset>
          <SaveActions action={action} disabled={!writable} reload={reload} />
        </form>
      )}
    </>
  );
}

export function DesktopAccounts({
  accountId,
  account,
}: Props & { account?: Account }) {
  const query = useRead<Account>(
    accountId,
    "account",
    "global",
    "/auth/me",
    !account,
  );
  const current = account || query.data;
  return (
    <section className="platform-accounts desktop-administration">
      <div className="platform-accounts-head">
        <div>
          <strong>Current account</strong>
          <span>Dashboard access</span>
        </div>
      </div>
      <ErrorNotice error={query.error} stale={!!current} />
      {current ? (
        <>
          <div
            className="platform-account-columns desktop-account-columns"
            aria-hidden="true"
          >
            <span>Identity</span>
            <span>Access</span>
          </div>
          <div className="platform-account-list">
            <article className="platform-account-row desktop-account-row">
              <div className="platform-account-identity">
                <span className="platform-account-avatar">
                  <UserRound size={17} />
                </span>
                <span>
                  <strong>{current.username}</strong>
                  <code>Signed in</code>
                </span>
              </div>
              <div className="platform-account-access">
                <span
                  className={`platform-status ${current.permissions?.admin ? "good" : "quiet"}`}
                >
                  <ShieldCheck size={12} />
                  {current.role}
                </span>
                <span className="platform-muted">
                  {current.permissions?.admin
                    ? "Administrator"
                    : current.permissions?.write
                      ? "Project write access"
                      : "Read-only access"}
                </span>
              </div>
            </article>
          </div>
        </>
      ) : (
        <div className="platform-empty">
          {query.error
            ? "Account details are unavailable."
            : "Loading account..."}
        </div>
      )}
    </section>
  );
}
