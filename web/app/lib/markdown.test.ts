import { describe, expect, it } from 'vitest';
import { renderMarkdown } from './markdown';

describe('agent Markdown', () => {
  it('renders the connection summary as a list with emphasis and inline code', () => {
    const html = renderMarkdown('Observed via `xd:/read_connections` (live call):\n\n- **Docker Desktop**: os Linux, ip none.\n  ID `connection-id`.\n\nTotal: 1 saved connection.');
    expect(html).toContain('<code>xd:/read_connections</code>');
    expect(html).toContain('<ul>\n<li><strong>Docker Desktop</strong>');
    expect(html).toContain('<code>connection-id</code>');
  });

  it('escapes executable HTML and rejects unsafe links and embedded images', () => {
    const html = renderMarkdown('<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>\n\n[bad](javascript:alert%281%29)\n\n[encoded](jav&#x61;script:alert%281%29)\n\n[data](data:text/html,payload)\n\n![remote](https://example.com/tracker.png)\n\n[safe](https://example.com)');
    expect(html).not.toMatch(/<(script|img)\b/i);
    expect(html).not.toMatch(/href="(?:javascript|data):/i);
    expect(html).toContain('&lt;script&gt;');
    expect(html).toContain('<a href="https://example.com">safe</a>');
  });

  it('keeps HTML inside code fences literal and supports incomplete streamed fences', () => {
    const partial = '```html\n<img src=x onerror=alert(1)>';
    for (const text of [partial, `${partial}\n\x60\x60\x60`]) {
      const html = renderMarkdown(text);
      expect(html).toContain('<pre><code class="language-html">&lt;img');
      expect(html).not.toContain('<img');
    }
  });
});
