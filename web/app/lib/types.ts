export type RunStatus = 'running' | 'completed' | 'failed' | 'blocked';
export type AgentStatus = 'pending' | 'running' | 'completed' | 'failed' | 'blocked';
export type AgentId = string;
export interface AgentPosition { x: number; y: number; }

/** omp `--thinking` levels accepted by the backend profile validator. */
export const THINKING_LEVELS = ['off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'auto'] as const;
export type ThinkingLevel = typeof THINKING_LEVELS[number];
export const DEFAULT_THINKING: ThinkingLevel = 'medium';

/** Timeout bounds accepted by the backend profile validator (seconds). */
export const TIMEOUT_SECONDS_MIN = 10;
export const TIMEOUT_SECONDS_MAX = 86400;
/** Default per-agent session timeout; matches the backend default. */
export const DEFAULT_STEP_TIMEOUT_SECONDS = 10800;

export interface PipelineStep {
  id: string;
  name: string;
  model_selector: string;
  prompt: string;
  depends_on: string[];
  position: AgentPosition;
  /** Reasoning effort for this agent's omp session. */
  thinking: ThinkingLevel;
  /** Wall-clock limit for this agent's omp session, in seconds. */
  timeout_seconds: number;
}

export interface PipelineProfile {
  id: string;
  name: string;
  steps: PipelineStep[];
  root_prompt?: string;
  /** Model for the root orchestrator; empty/absent uses the server's [roles.root] default. */
  root_model?: string;
  /** Effort for the root orchestrator; empty/absent uses the server's [roles.root] default. */
  root_thinking?: ThinkingLevel | '';
  /** Bound for the root session (and thus the run); 0/absent uses the server's [roles.root] timeout. */
  pipeline_timeout_seconds?: number;
  revision?: number;
}

export interface ProgressSummary {
  total: number;
  completed: number;
  failed: number;
  blocked: number;
  running: number;
}

export interface ProjectSummary {
  project_id: string;
  name: string;
  status: string;
  created_at: string;
  target: string;
  initial_prompt: string;
  updated_at: string;
  project_directory: string;
  current_run: RunSummary | null;
  progress: ProgressSummary;
  pipeline_profile: string | null;
}

export interface RunSummary {
  run_id: string;
  project_id: string;
  status: RunStatus;
  created_at: string;
  updated_at: string;
  error: string | null;
}

export type AgentLayout = Partial<Record<string, AgentPosition>>;

export interface ProjectDetail extends ProjectSummary {
  agent_layout: AgentLayout;
  pipeline_profile_definition: PipelineProfile | null;
}

export interface ProjectChanges {
  name?: string;
  target?: string;
  initial_prompt?: string;
  project_directory?: string;
  pipeline_profile?: string | null;
}

export interface NewProject extends ProjectChanges {
  name: string;
  project_directory: string;
}

export interface StartedRun {
  run_id: string;
  project_id: string;
  status: 'running';
}

export interface AgentInvocation {
  invocation_id: number;
  role: AgentId;
  model_selector: string | null;
  thinking: string;
  status: AgentStatus;
  started_at: string;
  finished_at: string | null;
  returncode: number | null;
  error: string | null;
  output_artifact_id: string | null;
  stderr_artifact_id: string | null;
  chat_available: boolean;
  /** Completed in an earlier run and carried into this one by Resume. */
  reused: boolean;
  /** Model requests recorded for this invocation; null until one completes (always null when reused). */
  usage: CostUsage | null;
}

/** Aggregated model requests. Token sums and `cost_usd` are null when OMP reported none. */
export interface CostUsage {
  requests: number;
  /** Requests OMP reported without a price; the known `cost_usd` is then a partial total. */
  unpriced_requests: number;
  cost_usd: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  cache_read_tokens: number | null;
  cache_write_tokens: number | null;
  total_tokens: number | null;
}

/** Lifetime usage of one resolved model (`provider/model`) inside one project. */
export interface CostRow extends CostUsage {
  project_id: string;
  project_name: string;
  model: string;
}

export interface Artifact {
  artifact_id: string;
  run_id: string;
  relative_path: string;
  sha256: string;
  size: number;
  media_type: string;
  created_at: string;
}

export interface RunDetail {
  run: RunSummary;
  project: ProjectSummary | null;
  agents: AgentInvocation[];
  artifacts: Artifact[];
  pipeline_profile: PipelineProfile | null;
}

export interface LogPage {
  run_id: string;
  invocation_id: number;
  stream: 'stdout' | 'stderr';
  lines: string[];
  next_offset: number;
  eof: boolean;
}

export interface Connection {
  connection_id: string;
  name: string;
  os: string;
  ip: string | null;
  has_cred: boolean;
  in_use: boolean;
  used_by: { run_id: string; role: string } | null;
  created_at: string;
  updated_at: string;
}

export interface NewConnection {
  name: string;
  os: string;
  ip?: string | null;
  cred?: string | null;
}

export interface ConnectionChanges {
  name?: string;
  os?: string;
  ip?: string | null;
  cred?: string | null;
}

export type McpServerConfig = Record<string, unknown>;
export interface McpCatalogEntry { name: string; transport: string; endpoint: string | null; revision: number; created_at: string; updated_at: string; }
export interface McpCatalogDetail { name: string; config: McpServerConfig; revision: number; created_at: string; updated_at: string; }
export interface ProjectMcpServer { name: string; config: McpServerConfig; source: 'catalog' | 'custom'; catalog_name: string | null; source_revision: number | null; source_missing: boolean; update_available: boolean; modified_locally: boolean; status: string; last_checked: string | null; }
export interface ProjectMcpList { file_revision: string; servers: ProjectMcpServer[]; }
export interface McpCheckResult { name: string; status: string; checked_at: string | null; detail: string; }
