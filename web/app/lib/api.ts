import type {
  AgentId, AgentInvocation, AgentLayout, AgentPosition, Connection, ConnectionChanges, CostRow, LogPage,
  McpCatalogDetail, McpCatalogEntry, McpCheckResult, McpServerConfig, NewConnection, NewProject,
  PipelineProfile, ProjectChanges, ProjectDetail, ProjectMcpList, ProjectSummary,
  RunDetail, StartedRun,
} from './types';

const TOKEN_KEY = 'orches-admin-token';

// '' keeps same-origin '/api' (Nuxt dev proxy); configureApiBaseUrl switches to
// a validated remote origin for the direct browser->backend deployment.
let apiBase = '';
let onUnauthorized: (() => void) | null = null;

const LOOPBACK_HOSTS: Record<string, true> = { '127.0.0.1': true, localhost: true, '[::1]': true, '::1': true };

/** Accept a bare https origin, or an http loopback origin for local development. */
export function configureApiBaseUrl(raw: string): void {
  if (!raw) {
    apiBase = '';
    return;
  }
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    throw new Error(`Invalid API base URL: ${raw}`);
  }
  const loopback = LOOPBACK_HOSTS[url.hostname] === true;
  const bare = !url.username && !url.password && (url.pathname === '/' || url.pathname === '') && !url.search && !url.hash;
  if (!bare || (url.protocol !== 'https:' && !(url.protocol === 'http:' && loopback))) {
    throw new Error('API base URL must be a bare https origin (or an http loopback origin for local development)');
  }
  apiBase = url.origin;
}

/** Called when a protected request answers 401; the app redirects to login. */
export function setUnauthorizedHandler(handler: (() => void) | null): void {
  onUnauthorized = handler;
}

const storage = (): Storage | null => (typeof sessionStorage === 'undefined' ? null : sessionStorage);
export const getToken = (): string | null => storage()?.getItem(TOKEN_KEY) ?? null;
export const clearToken = (): void => storage()?.removeItem(TOKEN_KEY);

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = {
    Accept: 'application/json',
    ...(init?.headers as Record<string, string> | undefined),
  };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  const response = await fetch(`${apiBase}/api${path}`, {
    ...init,
    headers,
    cache: 'no-store',
  });
  if (response.status === 401 && !path.startsWith('/auth/login')) {
    // Expired or revoked session: drop the stale token and return to login.
    clearToken();
    onUnauthorized?.();
  }
  if (!response.ok) {
    let detail = 'Request failed';
    try {
      const body: { detail?: unknown } = await response.json();
      if (typeof body.detail === 'string') detail = body.detail;
      else if (body.detail !== undefined) detail = JSON.stringify(body.detail);
    } catch { /* A non-JSON error still includes its HTTP status. */ }
    throw new Error(`HTTP ${response.status}: ${detail}`);
  }
  return response.json() as Promise<T>;
}

const jsonMutation = (method: 'POST' | 'PUT' | 'PATCH', body: object): RequestInit => ({
  method,
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
});


// Authentication
export interface LoginResult { access_token: string; token_type: 'bearer'; expires_in: number }
export const login = async (password: string): Promise<void> => {
  const result = await request<LoginResult>('/auth/login', jsonMutation('POST', { password }));
  storage()?.setItem(TOKEN_KEY, result.access_token);
};
export const getAuthSession = () => request<{ authenticated: boolean }>('/auth/session');
export const logout = async (): Promise<void> => {
  try {
    await request<{ logged_out: boolean }>('/auth/logout', { method: 'POST' });
  } finally {
    clearToken();
  }
};
export const getHealth = () => request<{ status: string }>('/health');
export const getProjects = () => request<{ projects: ProjectSummary[] }>('/projects');
/** Lifetime project × model usage rows; sum them client-side for project and model totals. */
export const getCosts = () => request<{ rows: CostRow[] }>('/costs');
export const getPipelineProfiles = () => request<{ profiles: PipelineProfile[] }>('/pipeline-profiles');
export const createPipelineProfile = (name: string) =>
  request<PipelineProfile>('/pipeline-profiles', jsonMutation('POST', { name }));
export const getPipelineProfile = (id: string) =>
  request<PipelineProfile>(`/pipeline-profiles/${encodeURIComponent(id)}`);
export const updatePipelineProfile = (id: string, profile: PipelineProfile) =>
  request<PipelineProfile>(
    `/pipeline-profiles/${encodeURIComponent(id)}`,
    jsonMutation('PUT', {
      name: profile.name,
      steps: profile.steps,
      root_prompt: profile.root_prompt || '',
      root_model: profile.root_model || '',
      root_thinking: profile.root_thinking || '',
      pipeline_timeout_seconds: profile.pipeline_timeout_seconds || 0,
      revision: profile.revision,
    }),
  );
export const deletePipelineProfile = (id: string) =>
  request<{ deleted: boolean }>(`/pipeline-profiles/${encodeURIComponent(id)}`, { method: 'DELETE' });
export interface OmpModel { selector: string; name: string; provider: string; }
export const getOmpModels = () => request<{ models: OmpModel[] }>('/omp-models');
export const createProject = (project: NewProject) => request<ProjectSummary>('/projects', jsonMutation('POST', project));
export const getProject = (id: string) => request<ProjectDetail>(`/projects/${encodeURIComponent(id)}`);
export const updateProject = (id: string, changes: ProjectChanges) => request<ProjectSummary>(`/projects/${encodeURIComponent(id)}`, jsonMutation('PATCH', changes));
export const deleteProject = (id: string) => request<{ deleted: boolean }>(`/projects/${encodeURIComponent(id)}`, { method: 'DELETE' });
export const startProjectRun = (id: string) => request<StartedRun>(`/projects/${encodeURIComponent(id)}/runs`, jsonMutation('POST', {}));
export const saveAgentPosition = (projectId: string, role: AgentId, position: AgentPosition) =>
  request<{ agent_layout: AgentLayout }>(
    `/projects/${encodeURIComponent(projectId)}/agent-layout`,
    jsonMutation('PATCH', { role, x: position.x, y: position.y }),
  );
export const getRun = (id: string) => request<RunDetail>(`/runs/${encodeURIComponent(id)}`);
export const getAgents = (id: string) => request<{ agents: AgentInvocation[] }>(`/runs/${encodeURIComponent(id)}/agents`);
export const sendAgentMessage = (runId: string, invocationId: number, text: string) =>
  request<{ status: 'accepted'; run_id: string; invocation_id: number }>(
    `/runs/${encodeURIComponent(runId)}/agents/${invocationId}/messages`, jsonMutation('POST', { text }),
  );
export const getLogs = (runId: string, invocationId: number, stream: 'stdout' | 'stderr', offset = 0, limit = 200) =>
  request<LogPage>(`/runs/${encodeURIComponent(runId)}/agents/${invocationId}/logs?stream=${stream}&offset=${Math.max(0, offset)}&limit=${limit}`);
export const cancelRun = (id: string) =>
  request<{ run_id: string; status: 'cancelling' }>(`/runs/${encodeURIComponent(id)}/cancel`, { method: 'POST' });
export interface ResumedRun { run_id: string; project_id: string; status: 'running'; resumed_from: string; reused: string[] }
/** New run of the same snapshot; completed workers are reused unless listed in `rerun` (plus their dependents). */
export const resumeRun = (id: string, rerun: string[] = []) =>
  request<ResumedRun>(`/runs/${encodeURIComponent(id)}/resume`, jsonMutation('POST', rerun.length ? { rerun } : {}));
export const getConnections = () => request<{ connections: Connection[] }>('/connections');
export const createConnection = (connection: NewConnection) =>
  request<Connection>('/connections', jsonMutation('POST', connection));
export const updateConnection = (id: string, changes: ConnectionChanges) =>
  request<Connection>(`/connections/${encodeURIComponent(id)}`, jsonMutation('PATCH', changes));
export const deleteConnection = (id: string) =>
  request<{ deleted: boolean }>(`/connections/${encodeURIComponent(id)}`, { method: 'DELETE' });
export const revealConnectionCredential = (id: string, signal?: AbortSignal) =>
  request<{ cred: string | null }>(`/connections/${encodeURIComponent(id)}/cred`, { signal });

// MCP catalog
export const getMcpServers = () => request<{ servers: McpCatalogEntry[] }>('/mcp-servers');
export const createMcpServer = (name: string, config: McpServerConfig) =>
  request<McpCatalogDetail>('/mcp-servers', jsonMutation('POST', { name, config }));
export const getMcpServer = (name: string) =>
  request<McpCatalogDetail>(`/mcp-servers/${encodeURIComponent(name)}`);
export const updateMcpServer = (name: string, config: McpServerConfig, revision: number) =>
  request<McpCatalogDetail>(`/mcp-servers/${encodeURIComponent(name)}`, jsonMutation('PUT', { config, revision }));
export const deleteMcpServer = (name: string) =>
  request<{ deleted: boolean }>(`/mcp-servers/${encodeURIComponent(name)}`, { method: 'DELETE' });

// Project MCP
export const getProjectMcpServers = (projectId: string) =>
  request<ProjectMcpList>(`/projects/${encodeURIComponent(projectId)}/mcp-servers`);
export const addProjectMcpServer = (projectId: string, body: { source: string; name: string; config?: McpServerConfig; if_revision: string }) =>
  request<ProjectMcpList>(`/projects/${encodeURIComponent(projectId)}/mcp-servers`, jsonMutation('POST', body));
export const editProjectMcpServer = (projectId: string, name: string, config: McpServerConfig, if_revision: string) =>
  request<ProjectMcpList>(`/projects/${encodeURIComponent(projectId)}/mcp-servers/${encodeURIComponent(name)}`, jsonMutation('PUT', { config, if_revision }));
export const removeProjectMcpServer = (projectId: string, name: string, if_revision: string) =>
  request<ProjectMcpList>(`/projects/${encodeURIComponent(projectId)}/mcp-servers/${encodeURIComponent(name)}`, { method: 'DELETE', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ if_revision }) });
export const refreshProjectMcpServer = (projectId: string, name: string, if_revision: string) =>
  request<ProjectMcpList>(`/projects/${encodeURIComponent(projectId)}/mcp-servers/${encodeURIComponent(name)}/refresh`, jsonMutation('POST', { if_revision }));
export const checkProjectMcpServer = (projectId: string, name: string) =>
  request<McpCheckResult>(`/projects/${encodeURIComponent(projectId)}/mcp-servers/${encodeURIComponent(name)}/check`, jsonMutation('POST', {}));
