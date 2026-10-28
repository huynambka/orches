export type RunStatus = 'running' | 'completed' | 'failed' | 'blocked';
export type AgentStatus = 'pending' | 'running' | 'completed' | 'failed' | 'blocked';
export type AgentId = string;
export interface AgentPosition { x: number; y: number; }

export interface PipelineStep {
  id: string;
  name: string;
  model_selector: string;
  prompt: string;
  depends_on: string[];
  position: AgentPosition;
}

export interface PipelineProfile {
  id: string;
  name: string;
  steps: PipelineStep[];
  root_prompt?: string;
  /** Model for the root orchestrator; empty/absent uses the server's [roles.root] default. */
  root_model?: string;
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
