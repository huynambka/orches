import type {
  AgentId, AgentInvocation, AgentLayout, AgentPosition, Connection, ConnectionChanges, LogPage,
  NewConnection, NewProject, PipelineProfile, ProjectChanges, ProjectDetail, ProjectSummary,
  RunDetail, StartedRun,
} from './types';

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, {
    ...init,
    headers: { Accept: 'application/json', ...init?.headers },
    cache: 'no-store',
  });
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

export const getHealth = () => request<{ status: string }>('/health');
export const getProjects = () => request<{ projects: ProjectSummary[] }>('/projects');
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
export const getConnections = () => request<{ connections: Connection[] }>('/connections');
export const createConnection = (connection: NewConnection) =>
  request<Connection>('/connections', jsonMutation('POST', connection));
export const updateConnection = (id: string, changes: ConnectionChanges) =>
  request<Connection>(`/connections/${encodeURIComponent(id)}`, jsonMutation('PATCH', changes));
export const deleteConnection = (id: string) =>
  request<{ deleted: boolean }>(`/connections/${encodeURIComponent(id)}`, { method: 'DELETE' });
export const revealConnectionCredential = (id: string, signal?: AbortSignal) =>
  request<{ cred: string | null }>(`/connections/${encodeURIComponent(id)}/cred`, { signal });
