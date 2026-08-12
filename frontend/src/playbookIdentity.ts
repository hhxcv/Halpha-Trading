export const PLAYBOOK_REF_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$/;

export function normalizePlaybookRef(value: string): string {
  return value.trim();
}

export function isValidPlaybookRef(value: string): boolean {
  return PLAYBOOK_REF_PATTERN.test(normalizePlaybookRef(value));
}
