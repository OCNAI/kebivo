// Mirrors kebivo.tokens.VALID_SCOPES; kebivo/tests/test_schema_drift.py fails if they differ.
export const TOKEN_SCOPES = {
  gap: 'feedback:gap',
  correction: 'feedback:correction',
  gapsRead: 'gaps:read',
} as const;
