import MarkdownIt from 'markdown-it';

// Agent text is untrusted: escape raw HTML and retain markdown-it's URL checks.
// Do not fetch remote images embedded in model output.
const markdown = new MarkdownIt({ html: false, breaks: true }).disable('image');

export const renderMarkdown = (text: string): string => markdown.render(text);
