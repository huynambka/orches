import { describe, expect, it } from 'vitest';
import { appendConversation } from './conversation';

const line = (event: object) => JSON.stringify(event);

const user = { role: 'user', content: [{ type: 'text', text: 'Summarize the folder' }] };
const assistant = (text: string) => ({ role: 'assistant', content: [{ type: 'text', text }] });

describe('agent conversation from JSONL', () => {
  it('shows each prompt once and assembles streaming text across pages without duplicating the final message', () => {
    const first = appendConversation([], [
      line({ type: 'message_start', message: user }),
      line({ type: 'message_end', message: user }),
      line({ type: 'message_start', message: assistant('') }),
      line({ type: 'message_update', assistantMessageEvent: { type: 'thinking_delta', delta: 'private reasoning' } }),
      line({ type: 'message_update', assistantMessageEvent: { type: 'text_delta', delta: 'Hello ' } }),
    ]);
    const final = appendConversation(first, [
      line({ type: 'message_update', assistantMessageEvent: { type: 'text_delta', delta: 'world' } }),
      line({ type: 'message_end', message: assistant('Hello world') }),
      line({ type: 'turn_end', message: assistant('Hello world') }),
      line({ type: 'agent_end', messages: [user, assistant('Hello world')] }),
    ]);
    expect(final).toEqual([
      { kind: 'user', text: 'Summarize the folder' },
      { kind: 'assistant', text: 'Hello world', pending: false },
    ]);
  });

  it('pairs parallel tool results with the correct calls even when results arrive out of order', () => {
    const events = [
      line({ type: 'tool_execution_start', toolCallId: 'a', toolName: 'read', intent: 'Read advisory', args: { path: 'a' } }),
      line({ type: 'tool_execution_start', toolCallId: 'b', toolName: 'read', intent: 'Read patch', args: { path: 'b' } }),
      line({ type: 'tool_execution_end', toolCallId: 'b', result: { content: [{ type: 'text', text: 'patched' }] } }),
      line({ type: 'tool_execution_end', toolCallId: 'a', result: { content: [{ type: 'text', text: 'affected' }] } }),
    ];
    const entries = appendConversation([], events);
    expect(entries.map((entry) => entry.kind === 'tool' ? [entry.intent, entry.output, entry.pending] : [])).toEqual([
      ['Read advisory', 'affected', false], ['Read patch', 'patched', false],
    ]);
  });

  it('keeps plain output visible and ignores unrecognized lifecycle metadata', () => {
    expect(appendConversation([], ['warning: subprocess stopped', line({ type: 'session', cwd: 'hidden path' })])).toEqual([
      { kind: 'output', text: 'warning: subprocess stopped' },
    ]);
  });
});
