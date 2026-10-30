export type ConversationEntry =
  | { kind: 'user' | 'assistant' | 'output'; text: string; pending?: boolean; rpcMode?: boolean }
  | { kind: 'thinking'; text: string; pending: boolean }
  | { kind: 'tool'; name: string; intent: string; input: string; output: string; callId: string; pending: boolean };

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : null;
}

function contentText(value: unknown): string {
  if (!Array.isArray(value)) return '';
  return value.map((part) => {
    const item = record(part);
    return item?.type === 'text' && typeof item.text === 'string' ? item.text : '';
  }).filter(Boolean).join('\n');
}

function display(value: unknown): string {
  if (typeof value === 'string') return value;
  if (value == null) return '';
  return JSON.stringify(value, null, 2);
}

export function appendConversation(current: ConversationEntry[], lines: string[], maxEntries = 100): ConversationEntry[] {
  const entries = [...current];
  let rpcMode = entries.some((entry) => entry.kind === 'user' && entry.rpcMode);
  for (const line of lines) {
    let event: Record<string, unknown> | null;
    try { event = record(JSON.parse(line)); }
    catch { if (line.trim()) entries.push({ kind: 'output', text: line }); continue; }
    if (!event) continue;
    const message = record(event.message);
    if (event.type === 'stage_input' && typeof event.text === 'string') {
      rpcMode = true;
      entries.push({ kind: 'user', text: event.text, rpcMode: true });
    } else if (event.type === 'user_steer' && typeof event.text === 'string') {
      rpcMode = true;
      entries.push({ kind: 'user', text: event.text, rpcMode: true });
    } else if (event.type === 'message_start' && message?.role === 'user' && !rpcMode) {
      const text = contentText(message.content);
      if (text) entries.push({ kind: 'user', text });
    } else if (event.type === 'message_update') {
      const update = record(event.assistantMessageEvent);
      if (update?.type === 'text_delta' && typeof update.delta === 'string' && update.delta) {
        const last = entries[entries.length - 1];
        if (last?.kind === 'assistant' && last.pending) entries[entries.length - 1] = { ...last, text: last.text + update.delta };
        else entries.push({ kind: 'assistant', text: update.delta, pending: true });
      } else if (update?.type === 'thinking_start') {
        entries.push({ kind: 'thinking', text: '', pending: true });
      } else if (update?.type === 'thinking_delta' && typeof update.delta === 'string' && update.delta) {
        const last = entries[entries.length - 1];
        if (last?.kind === 'thinking' && last.pending) entries[entries.length - 1] = { ...last, text: last.text + update.delta };
        else entries.push({ kind: 'thinking', text: update.delta, pending: true });
      } else if (update?.type === 'thinking_end') {
        const index = entries.findLastIndex((entry) => entry.kind === 'thinking' && entry.pending);
        const text = typeof update.content === 'string' ? update.content : '';
        if (index === -1) {
          if (text) entries.push({ kind: 'thinking', text, pending: false });
        } else {
          const previous = entries[index];
          const finalText = text || (previous.kind === 'thinking' ? previous.text : '');
          // A block that never produced text is noise, not an entry.
          if (finalText) entries[index] = { kind: 'thinking', text: finalText, pending: false };
          else entries.splice(index, 1);
        }
      }
    } else if (event.type === 'message_end' && message?.role === 'assistant') {
      // A dropped stream must not leave a block stuck on "Thinking…".
      for (let i = entries.length - 1; i >= 0; i -= 1) {
        const entry = entries[i];
        if (entry.kind !== 'thinking' || !entry.pending) continue;
        if (entry.text) entries[i] = { ...entry, pending: false };
        else entries.splice(i, 1);
      }
      const text = contentText(message.content);
      const index = entries.findLastIndex((entry) => entry.kind === 'assistant' && entry.pending);
      if (index !== -1) {
        const previous = entries[index];
        entries[index] = { kind: 'assistant', text: text || (previous.kind === 'assistant' ? previous.text : ''), pending: false };
      } else if (text) entries.push({ kind: 'assistant', text });
    } else if (event.type === 'tool_execution_start') {
      entries.push({ kind: 'tool', name: display(event.toolName) || 'Tool', intent: display(event.intent),
        input: display(event.args), output: '', callId: display(event.toolCallId), pending: true });
    } else if (event.type === 'tool_execution_end') {
      const index = entries.findLastIndex((entry) => entry.kind === 'tool' && entry.callId === event.toolCallId);
      if (index !== -1) {
        const previous = entries[index];
        if (previous.kind === 'tool') {
          const result = record(event.result);
          entries[index] = { ...previous, pending: false, output: contentText(result?.content) || display(result?.error) || display(event.result) };
        }
      }
    }
  }
  const trimmed = entries.slice(-maxEntries);
  if (rpcMode && !trimmed.some((entry) => entry.kind === 'user' && entry.rpcMode)) {
    const marker = entries.find((entry) => entry.kind === 'user' && entry.rpcMode);
    if (marker) return [marker, ...trimmed.slice(0, Math.max(0, maxEntries - 1))];
  }
  return trimmed;
}
