import {
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  Check,
  Cpu,
  Download,
  HardDrive,
  Plus,
  RotateCcw,
  Save,
  Server,
  X,
} from "lucide-react";
import {
  ApiError,
  request,
  type Project,
  type Resources as ResourceData,
  type Settings as SettingsData,
} from "./api";
import { usePagedRead, useRead } from "./queries";
import {
  Empty,
  ErrorNotice,
  External,
  IconButton,
  State,
  Time,
} from "./shared";
import type { ViewProps } from "./shared";
import "./Administration.css";

type AdminProps = ViewProps & { admin?: boolean };
type Revisioned = { id: string; revision: number };
type ProjectRecord = Project & Revisioned & { description: string };
export type Machine = Revisioned & {
  slug: string;
  display_name: string;
  mode: string;
  workspace_root: string;
  scratch_root: string;
};
export type Harness = Revisioned & {
  slug: string;
  adapter: string;
  provider_version: string;
  enabled: boolean;
  model_options: { model?: string; reasoning_effort?: string };
};
type HostHarness = {
  harness_id: string;
  harness_slug?: string;
  execution_slots: number;
  max_parallel_subagents: number;
  enabled: boolean;
  credential_configured: boolean;
};
type Integration = Revisioned & {
  kind: string;
  endpoint: string;
  enabled: boolean;
  credential_ref?: string;
};
type PendingSave = { id: string; path: string; method: string; body: string };

export function useSave(accountId: string, scope: string) {
  const client = useQueryClient();
  const storageKey = `horizon.pipeline.catalog-save.${accountId}.${scope}`;
  const pending = useRef<PendingSave | null>(null);
  const submitting = useRef(false);
  const [busy, setBusy] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const [error, setError] = useState<Error | null>(null);
  const [saved, setSaved] = useState(false);
  const [storageBlocked, setStorageBlocked] = useState(false);
  useEffect(() => {
    try {
      const raw = sessionStorage.getItem(storageKey);
      if (!raw) return;
      const operation = JSON.parse(raw) as PendingSave;
      if (
        typeof operation.id !== "string" ||
        typeof operation.path !== "string" ||
        !operation.path.startsWith("/") ||
        !["POST", "PATCH"].includes(operation.method) ||
        typeof operation.body !== "string"
      )
        throw new Error("Stored save cannot be read.");
      pending.current = operation;
      setUncertain(true);
      setError(
        new Error(
          "A previous save is unconfirmed. Retry to recover that same request.",
        ),
      );
    } catch (failure) {
      setStorageBlocked(true);
      setError(
        new Error(
          `Save recovery storage is unavailable: ${failure instanceof Error ? failure.message : String(failure)}`,
        ),
      );
    }
  }, [storageKey]);
  async function save<T>(
    path: string,
    body: unknown,
    method = "PATCH",
  ): Promise<T | undefined> {
    if (submitting.current || storageBlocked) return;
    const operation = pending.current || {
      id: crypto.randomUUID(),
      path,
      method,
      body: JSON.stringify(body),
    };
    try {
      sessionStorage.setItem(storageKey, JSON.stringify(operation));
    } catch {
      setStorageBlocked(true);
      setError(
        new Error("Save was not sent because recovery storage is unavailable."),
      );
      return;
    }
    pending.current = operation;
    submitting.current = true;
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      const result = await request<T>(operation.path, {
        method: operation.method,
        headers: { "Idempotency-Key": operation.id },
        body: operation.body,
      });
      sessionStorage.removeItem(storageKey);
      pending.current = null;
      setUncertain(false);
      setSaved(true);
      await client.invalidateQueries({ queryKey: ["pipeline", accountId] });
      return result;
    } catch (failure) {
      const definite =
        failure instanceof ApiError &&
        failure.status >= 400 &&
        failure.status < 500 &&
        ![401, 403, 408, 429].includes(failure.status);
      if (definite) {
        pending.current = null;
        sessionStorage.removeItem(storageKey);
      }
      setUncertain(!definite);
      setError(
        new Error(
          failure instanceof ApiError && failure.status === 409
            ? "This record changed elsewhere. Your edits are still here. Reload saved values to review the latest version before editing again."
            : `${!definite ? "Save unconfirmed. Retry to recover the same request. " : ""}${failure instanceof Error ? failure.message : String(failure)}`,
        ),
      );
      if (failure instanceof ApiError && failure.status === 409)
        await client.invalidateQueries({ queryKey: ["pipeline", accountId] });
    } finally {
      submitting.current = false;
      setBusy(false);
    }
  }
  return {
    save,
    busy,
    storageBlocked,
    uncertain,
    error,
    saved,
    reset: () => {
      setError(null);
      setSaved(false);
    },
  };
}

export function SaveActions({
  action,
  disabled,
  reload,
}: {
  action: ReturnType<typeof useSave>;
  disabled: boolean;
  reload?: () => void;
}) {
  return (
    <>
      <ErrorNotice error={action.error} stale={false} />
      <div className="pl-admin-actions">
        <button
          type="submit"
          className="pl-admin-primary"
          disabled={disabled || action.busy || action.storageBlocked}
        >
          <Save size={15} />
          {action.busy
            ? "Saving..."
            : action.uncertain
              ? "Retry save"
              : "Save changes"}
        </button>
        {reload && (
          <button
            type="button"
            disabled={action.busy || action.uncertain}
            onClick={reload}
          >
            <RotateCcw size={14} />
            Reload saved values
          </button>
        )}
        {action.saved && (
          <span className="pl-admin-saved" role="status">
            <Check size={15} />
            Saved
          </span>
        )}
      </div>
    </>
  );
}

export function ProjectCreate({
  accountId,
  writable,
  onCreated,
  onClose,
}: {
  accountId: string;
  writable: boolean;
  onCreated: (project: Project) => void;
  onClose: () => void;
}) {
  const action = useSave(accountId, "new-project");
  const dialog = useRef<HTMLDialogElement>(null);
  const [title, setTitle] = useState("");
  const [slug, setSlug] = useState("");
  const [description, setDescription] = useState("");
  const [slugEdited, setSlugEdited] = useState(false);
  useEffect(() => {
    dialog.current?.showModal();
  }, []);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!writable) return;
    const project = await action.save<Project>(
      "/records/project",
      { title: title.trim(), slug, description },
      "POST",
    );
    if (project) onCreated(project);
  };
  return (
    <dialog
      ref={dialog}
      className="pl-admin-dialog"
      aria-labelledby="pl-project-create-title"
      onCancel={(event) => {
        if (action.busy || action.uncertain) event.preventDefault();
        else onClose();
      }}
    >
      <div className="pl-section-heading">
        <h2 id="pl-project-create-title">New project</h2>
        <IconButton
          title="Close new project"
          disabled={action.busy || action.uncertain}
          onClick={onClose}
        >
          <X size={18} />
        </IconButton>
      </div>
      <form onSubmit={(event) => void submit(event)}>
        <fieldset
          disabled={!writable || action.busy || action.uncertain}
          className="pl-admin-fields"
        >
          <label>
            Project name
            <input
              required
              autoFocus
              maxLength={500}
              value={title}
              onChange={(event) => {
                setTitle(event.target.value);
                if (!slugEdited)
                  setSlug(
                    event.target.value
                      .toLowerCase()
                      .replace(/[^a-z0-9]+/g, "-")
                      .replace(/^[^a-z]+|[-]+$/g, "")
                      .slice(0, 64),
                  );
              }}
            />
          </label>
          <label>
            Project identifier
            <input
              required
              pattern="[a-z][a-z0-9_-]{0,63}"
              maxLength={64}
              value={slug}
              onChange={(event) => {
                setSlugEdited(true);
                setSlug(event.target.value);
              }}
            />
          </label>
          <label>
            Description
            <textarea
              aria-label="Description"
              rows={4}
              value={description}
              onChange={(event) => setDescription(event.target.value)}
            />
          </label>
        </fieldset>
        <ErrorNotice error={action.error} stale={false} />
        <div className="pl-admin-actions">
          <button
            className="pl-admin-primary"
            type="submit"
            disabled={!writable || action.busy || action.storageBlocked}
          >
            <Plus size={16} />
            {action.busy
              ? "Creating..."
              : action.uncertain
                ? "Retry creation"
                : "Create project"}
          </button>
          <button
            type="button"
            disabled={action.busy || action.uncertain}
            onClick={onClose}
          >
            Cancel
          </button>
        </div>
      </form>
    </dialog>
  );
}

export function ProjectSettings({
  accountId,
  projectId,
  writable,
}: Pick<ViewProps, "accountId" | "projectId" | "writable">) {
  const query = useRead<ProjectRecord>(
    accountId,
    "settings",
    projectId,
    `/records/project/${encodeURIComponent(projectId)}`,
    !!projectId,
  );
  return (
    <>
      <ErrorNotice error={query.error} stale={!!query.data} />
      {query.data ? (
        <ProjectEditor
          key={query.data.id}
          project={query.data}
          {...{ accountId, writable }}
        />
      ) : (
        <Empty>
          {projectId
            ? "Loading project..."
            : "Select a project to edit its settings."}
        </Empty>
      )}
    </>
  );
}

function ProjectEditor({
  project,
  accountId,
  writable,
}: {
  project: ProjectRecord;
  accountId: string;
  writable: boolean;
}) {
  const [draft, setDraft] = useState(project);
  const action = useSave(accountId, `project.${project.id}`);
  return (
    <form
      className="pl-admin-form"
      onSubmit={(event) => {
        event.preventDefault();
        if (writable)
          void action
            .save<ProjectRecord>(`/records/project/${project.id}`, {
              expected_revision: draft.revision,
              changes: { title: draft.title, description: draft.description,
                ...(draft.workflow !== project.workflow ? {workflow: draft.workflow} : {}) },
            })
            .then((result) => {
              if (result) setDraft(result);
            });
      }}
    >
      <h3>Project details</h3>
      <fieldset
        className="pl-admin-fields"
        disabled={!writable || action.busy || action.uncertain}
      >
        <label>
          Name
          <input
            required
            value={draft.title}
            onChange={(event) => {
              action.reset();
              setDraft({ ...draft, title: event.target.value });
            }}
          />
        </label>
        <label>
          Identifier
          <input readOnly value={project.slug} />
        </label>
        <label className="pl-admin-wide">
          Description
          <textarea
            aria-label="Description"
            rows={4}
            value={draft.description}
            onChange={(event) => {
              action.reset();
              setDraft({ ...draft, description: event.target.value });
            }}
          />
        </label>
        <label className="pl-admin-wide">Workflow
          <select value={draft.workflow || "legacy"} disabled={project.workflow === "milestones"}
            onChange={event => setDraft({...draft, workflow: event.target.value as "legacy" | "milestones"})}>
            <option value="legacy">Existing roadmap workflow</option>
            <option value="milestones">Reviewed Lean milestones</option>
          </select>
        </label>
      </fieldset>
      <SaveActions
        action={action}
        disabled={!writable}
        reload={() => {
          setDraft(project);
          action.reset();
        }}
      />
    </form>
  );
}

export function MachineEditor({
  host,
  accountId,
  writable,
}: {
  host: Machine;
  accountId: string;
  writable: boolean;
}) {
  const [draft, setDraft] = useState(host);
  const action = useSave(accountId, `host.${host.id}`);
  const query = useRead<{ items: HostHarness[]; host_revision: number }>(
    accountId,
    "resources",
    "global",
    `/hosts/${host.id}/harnesses`,
  );
  return (
    <div className="pl-admin-machine">
      <form
        className="pl-admin-form"
        onSubmit={(event) => {
          event.preventDefault();
          if (writable)
            void action
              .save<Machine>(`/records/host/${host.id}`, {
                expected_revision: draft.revision,
                changes: { display_name: draft.display_name, mode: draft.mode },
              })
              .then((result) => {
                if (result) setDraft(result);
              });
        }}
      >
        <fieldset
          className="pl-admin-fields"
          disabled={!writable || action.busy || action.uncertain}
        >
          <label>
            Machine name
            <input
              required
              value={draft.display_name}
              onChange={(event) => {
                action.reset();
                setDraft({ ...draft, display_name: event.target.value });
              }}
            />
          </label>
          <label>
            Scheduling
            <select
              value={draft.mode}
              onChange={(event) => {
                action.reset();
                setDraft({ ...draft, mode: event.target.value });
              }}
            >
              <option value="enabled">Accept new sessions</option>
              <option value="draining">Finish active sessions only</option>
              <option value="disabled">Disabled</option>
            </select>
          </label>
        </fieldset>
        <SaveActions
          action={action}
          disabled={!writable}
          reload={() => {
            setDraft(host);
            action.reset();
          }}
        />
      </form>
      <ErrorNotice error={query.error} stale={!!query.data} />
      {query.data?.items.map((item) => (
        <CapacityEditor
          key={item.harness_id}
          {...{ accountId, writable, host, item }}
          revision={query.data!.host_revision}
        />
      ))}
      <details className="pl-admin-paths">
        <summary>Workspace locations</summary>
        <dl>
          <dt>Workspaces</dt>
          <dd>{host.workspace_root}</dd>
          <dt>Scratch</dt>
          <dd>{host.scratch_root}</dd>
        </dl>
      </details>
    </div>
  );
}

function CapacityEditor({
  host,
  item,
  revision,
  accountId,
  writable,
}: {
  host: Machine;
  item: HostHarness;
  revision: number;
  accountId: string;
  writable: boolean;
}) {
  const [slots, setSlots] = useState(item.execution_slots);
  const [enabled, setEnabled] = useState(item.enabled);
  const [baseRevision, setBaseRevision] = useState(revision);
  const [dirty, setDirty] = useState(false);
  const action = useSave(accountId, `capacity.${host.id}.${item.harness_id}`);
  useEffect(() => {
    if (!dirty && !action.uncertain) {
      setSlots(item.execution_slots);
      setEnabled(item.enabled);
      setBaseRevision(revision);
    }
  }, [revision, item.execution_slots, item.enabled, dirty, action.uncertain]);
  return (
    <form
      className="pl-admin-capacity"
      onSubmit={(event) => {
        event.preventDefault();
        if (writable)
          void action
            .save<{ host_revision: number }>(
              `/hosts/${host.id}/harnesses/${item.harness_id}`,
              {
                expected_revision: baseRevision,
                changes: { execution_slots: slots, enabled },
              },
            )
            .then((result) => {
              if (result) {
                setBaseRevision(result.host_revision);
                setDirty(false);
              }
            });
      }}
    >
      <h4>{item.harness_slug || "Harness capacity"}</h4>
      <fieldset
        className="pl-admin-fields"
        disabled={!writable || action.busy || action.uncertain}
      >
        <label>
          Scheduling slot limit
          <input
            title="New sessions are also limited by the worker daemon's local slot configuration."
            type="number"
            required
            min={1}
            step={1}
            value={slots}
            onChange={(event) => {
              action.reset();
              setDirty(true);
              setSlots(event.target.valueAsNumber);
            }}
          />
        </label>
        <label className="pl-admin-check">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(event) => {
              action.reset();
              setDirty(true);
              setEnabled(event.target.checked);
            }}
          />
          Enable this harness on this machine
        </label>
      </fieldset>
      <SaveActions
        action={action}
        disabled={!writable}
        reload={() => {
          setSlots(item.execution_slots);
          setEnabled(item.enabled);
          setBaseRevision(revision);
          setDirty(false);
          action.reset();
        }}
      />
    </form>
  );
}

export function HarnessEditor({
  harness,
  accountId,
  writable,
}: {
  harness: Harness;
  accountId: string;
  writable: boolean;
}) {
  const [draft, setDraft] = useState(harness);
  const action = useSave(accountId, `harness.${harness.id}`);
  return (
    <form
      className="pl-admin-form"
      onSubmit={(event) => {
        event.preventDefault();
        if (writable)
          void action
            .save<Harness>(`/records/harness/${harness.id}`, {
              expected_revision: draft.revision,
              changes: {
                enabled: draft.enabled,
                model_options: Object.fromEntries(
                  Object.entries(draft.model_options).filter(([, value]) =>
                    value?.trim(),
                  ),
                ),
              },
            })
            .then((result) => {
              if (result) setDraft(result);
            });
      }}
    >
      <div className="pl-section-heading">
        <h3>{harness.slug}</h3>
        <span className="pl-muted">
          {harness.adapter.replace(/_/g, " ")} / {harness.provider_version}
        </span>
      </div>
      <fieldset
        className="pl-admin-fields"
        disabled={!writable || action.busy || action.uncertain}
      >
        <label>
          Default model
          <input
            value={draft.model_options.model || ""}
            placeholder="Provider default"
            onChange={(event) => {
              action.reset();
              setDraft({
                ...draft,
                model_options: {
                  ...draft.model_options,
                  model: event.target.value,
                },
              });
            }}
          />
        </label>
        <label>
          Reasoning effort
          <input
            list={`pl-reasoning-${harness.id}`}
            value={draft.model_options.reasoning_effort || ""}
            placeholder="Provider default"
            onChange={(event) => {
              action.reset();
              setDraft({
                ...draft,
                model_options: {
                  ...draft.model_options,
                  reasoning_effort: event.target.value,
                },
              });
            }}
          />
          <datalist id={`pl-reasoning-${harness.id}`}>
            {["low", "medium", "high", "xhigh", "max"].map((value) => (
              <option key={value} value={value} />
            ))}
          </datalist>
        </label>
        <label className="pl-admin-check pl-admin-wide">
          <input
            type="checkbox"
            checked={draft.enabled}
            onChange={(event) => {
              action.reset();
              setDraft({ ...draft, enabled: event.target.checked });
            }}
          />
          Available for new sessions
        </label>
      </fieldset>
      <SaveActions
        action={action}
        disabled={!writable}
        reload={() => {
          setDraft(harness);
          action.reset();
        }}
      />
    </form>
  );
}

function CatalogList<T>({
  accountId,
  kind,
  children,
}: {
  accountId: string;
  kind: string;
  children: (item: T) => ReactNode;
}) {
  const query = usePagedRead<T>(
    accountId,
    "settings",
    "global",
    `/records/${kind}?limit=50`,
  );
  const items = query.data?.pages.flatMap((page) => page.items) || [];
  return (
    <>
      <ErrorNotice error={query.error} stale={!!query.data} />
      {query.isPending ? (
        <Empty>Loading...</Empty>
      ) : !items.length ? (
        <Empty>
          No {kind === "host" ? "machines" : `${kind}s`} configured.
        </Empty>
      ) : (
        items.map(children)
      )}
      {query.hasNextPage && (
        <button
          disabled={query.isFetchingNextPage}
          onClick={() => void query.fetchNextPage()}
        >
          Load more
        </button>
      )}
    </>
  );
}

function Installation({ accountId }: { accountId: string }) {
  const query = useRead<SettingsData>(
    accountId,
    "settings",
    "global",
    "/settings",
  );
  const data = query.data?.configuration;
  const exportConfiguration = () => {
    if (!data) return;
    const url = URL.createObjectURL(
      new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }),
    );
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "horizon-configuration.json";
    anchor.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  return (
    <>
      <div className="pl-section-heading">
        <h3>Installation</h3>
        <IconButton
          title="Export redacted installation configuration"
          disabled={!data}
          onClick={exportConfiguration}
        >
          <Download size={16} />
        </IconButton>
      </div>
      <ErrorNotice error={query.error} stale={!!data} />
      {data && (
        <dl className="pl-admin-facts">
          {[
            ["Public address", data.public_url],
            ["State directory", data.state_root],
            ["Search", data.search_enabled ? "Enabled" : "Disabled"],
          ].map(([label, value]) => (
            <div key={String(label)}>
              <dt>{String(label)}</dt>
              <dd>{String(value ?? "Not configured")}</dd>
            </div>
          ))}
        </dl>
      )}
    </>
  );
}

function Connections({
  accountId,
  projectId,
}: {
  accountId: string;
  projectId: string;
}) {
  const query = useRead<{ items: { id: string; public_url: string | null }[] }>(
    accountId,
    "integrations",
    projectId,
    `/projects/${encodeURIComponent(projectId)}/integrations`,
    !!projectId,
  );
  return (
    <>
      <h3>Connected services</h3>
      <ErrorNotice error={query.error} stale={!!query.data} />
      <CatalogList<Integration> {...{ accountId }} kind="integration">
        {(item) => {
          const publicUrl = query.data?.items.find(
            (integration) => integration.id === item.id,
          )?.public_url;
          return (
            <div className="pl-admin-connection" key={item.id}>
              <strong>
                {item.kind === "forge"
                  ? "Forge"
                  : item.kind === "zulip"
                    ? "Zulip"
                    : item.kind}
              </strong>
              {publicUrl ? (
                <External url={publicUrl}>{publicUrl}</External>
              ) : (
                <span className="pl-muted">Private connection</span>
              )}
              <State value={item.enabled ? "enabled" : "disabled"} />
              <span>
                {item.credential_ref
                  ? "Credential reference configured"
                  : "No credential reference"}
              </span>
            </div>
          );
        }}
      </CatalogList>
      <Installation {...{ accountId }} />
    </>
  );
}

export function Settings({
  accountId,
  projectId,
  writable,
  admin = false,
}: AdminProps) {
  const [tab, setTab] = useState("project");
  const tabs = admin
    ? [
        ["project", "Project"],
        ["machines", "Machines"],
        ["harnesses", "Harnesses"],
        ["connections", "Connections"],
      ]
    : [["project", "Project"]];
  return (
    <section className="pl-admin">
      <div className="pl-section-heading">
        <h2>Settings</h2>
      </div>
      <div
        className="pl-admin-tabs"
        role="tablist"
        aria-label="Settings categories"
      >
        {tabs.map(([id, label]) => (
          <button
            key={id}
            role="tab"
            aria-selected={tab === id}
            onClick={() => setTab(id)}
          >
            {label}
          </button>
        ))}
      </div>
      {tab === "project" && (
        <ProjectSettings
          key={projectId}
          {...{ accountId, projectId, writable }}
        />
      )}
      {admin && tab === "machines" && (
        <CatalogList<Machine> {...{ accountId }} kind="host">
          {(host) => (
            <MachineEditor key={host.id} {...{ host, accountId, writable }} />
          )}
        </CatalogList>
      )}
      {admin && tab === "harnesses" && (
        <CatalogList<Harness> {...{ accountId }} kind="harness">
          {(harness) => (
            <HarnessEditor
              key={harness.id}
              {...{ harness, accountId, writable }}
            />
          )}
        </CatalogList>
      )}
      {admin && tab === "connections" && (
        <Connections {...{ accountId, projectId }} />
      )}
    </section>
  );
}

const bytes = (value: number) =>
  `${new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 }).format(value / 1024 ** 3)} GiB`;

export function Resources({ accountId, admin = false }: AdminProps) {
  const query = useRead<ResourceData>(
    accountId,
    "resources",
    "global",
    "/resources",
    admin,
  );
  const data = query.data;
  const busy =
    data?.hosts.reduce((sum, host) => sum + host.occupied_slots, 0) || 0;
  const slots = data?.hosts.reduce((sum, host) => sum + host.slots, 0) || 0;
  return (
    <section className="pl-admin">
      <div className="pl-section-heading">
        <h2>Machines</h2>
        {data && <Time value={data.observed_at} />}
      </div>
      <ErrorNotice error={query.error} stale={!!data} />
      {!admin ? (
        <Empty>Machine administration requires an administrator account.</Empty>
      ) : !data ? (
        <Empty>
          {query.isPending
            ? "Loading resources..."
            : "Resources are unavailable."}
        </Empty>
      ) : (
        <>
          <div className="pl-admin-metrics">
            <div>
              <Server size={19} />
              <span>
                Machines
                <strong>
                  {
                    data.hosts.filter((host) =>
                      [
                        "enabled",
                        "draining",
                        "disabled",
                        "active",
                        "online",
                      ].includes(host.status),
                    ).length
                  }{" "}
                  / {data.hosts.length} online
                </strong>
              </span>
            </div>
            <div>
              <Cpu size={19} />
              <span>
                Session capacity
                <strong>
                  {busy} running / {slots} configured
                </strong>
              </span>
            </div>
            <div>
              <HardDrive size={19} />
              <span>
                Control-plane disk free
                <strong>
                  {data.free_bytes === null
                    ? "Unknown"
                    : bytes(data.free_bytes)}
                </strong>
              </span>
            </div>
          </div>
          <h3>Worker machines</h3>
          <div className="pl-table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Machine</th>
                  <th>Connection</th>
                  <th>Sessions</th>
                  <th>Last heartbeat</th>
                </tr>
              </thead>
              <tbody>
                {data.hosts.map((host) => (
                  <tr key={host.id}>
                    <td>
                      <strong>{host.name}</strong>
                      {host.detail && <small>{host.detail}</small>}
                    </td>
                    <td>
                      <State
                        value={
                          host.status === "enabled" ? "online" : host.status
                        }
                      />
                    </td>
                    <td>
                      <div className="pl-admin-usage">
                        <progress
                          max={Math.max(host.slots, host.occupied_slots, 1)}
                          value={host.occupied_slots}
                          aria-label={`${host.name}: ${host.occupied_slots} of ${host.slots} sessions`}
                        />
                        <span>
                          {host.occupied_slots} / {host.slots}
                        </span>
                      </div>
                    </td>
                    <td>
                      <Time value={host.heartbeat_at} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {!data.hosts.length && <Empty>No machines enrolled.</Empty>}
          {!!data.limits?.length && (
            <>
              <h3>Shared capacity</h3>
              <div className="pl-table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Pool</th>
                      <th>Type</th>
                      <th>Sessions</th>
                      <th>Retry after</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.limits.map((pool) => (
                      <tr key={pool.id}>
                        <td>{pool.slug}</td>
                        <td>{pool.kind.replace(/_/g, " ")}</td>
                        <td>
                          {pool.occupied} / {pool.max_concurrent}
                        </td>
                        <td>
                          {pool.cooldown_until ? (
                            <Time value={pool.cooldown_until} />
                          ) : (
                            "Available"
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
          <h3>Storage tracked by Horizon</h3>
          <div className="pl-table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Content</th>
                  <th>Size</th>
                  <th>Protected</th>
                  <th>Reclaimable</th>
                </tr>
              </thead>
              <tbody>
                {data.storage.map((item) => (
                  <tr key={item.category}>
                    <td>{item.category}</td>
                    <td>{bytes(item.bytes)}</td>
                    <td>{bytes(item.protected_bytes)}</td>
                    <td>{bytes(item.reclaimable_bytes)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <dl className="pl-admin-facts">
            <div>
              <dt>Provider capacity</dt>
              <dd>{data.provider_status}</dd>
            </div>
            <div>
              <dt>Backup evidence</dt>
              <dd>{data.backup_status}</dd>
            </div>
            <div>
              <dt>Oldest pending delivery</dt>
              <dd>
                {data.oldest_pending_delivery ? (
                  <Time value={data.oldest_pending_delivery} />
                ) : (
                  "No pending deliveries"
                )}
              </dd>
            </div>
          </dl>
        </>
      )}
    </section>
  );
}
