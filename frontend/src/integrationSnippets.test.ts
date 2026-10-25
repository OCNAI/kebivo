import { describe, expect, it } from 'vitest';
import { mcpConfig, restSnippets, TOKEN_PLACEHOLDER } from './integrationSnippets';

describe('integration snippets', () => {
  it('reads the token from the environment in every REST snippet', () => {
    const snippets = restSnippets('http://127.0.0.1:8000', 'knowledge_gap');
    expect(snippets.cURL).toContain('Bearer $KEBIVO_TOKEN');
    expect(snippets.Python).toContain('os.environ["KEBIVO_TOKEN"]');
    // A template literal, so Node substitutes the token instead of sending the text.
    expect(snippets.TypeScript).toContain('`Bearer ${process.env.KEBIVO_TOKEN}`');
  });

  it('uses the placeholder, never a token, in the MCP configuration', () => {
    const config = JSON.parse(mcpConfig('http://127.0.0.1:8000'));
    expect(config.mcpServers.kebivo.headers.Authorization).toBe(`Bearer ${TOKEN_PLACEHOLDER}`);
  });
});
