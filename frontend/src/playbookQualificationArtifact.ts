import type {
  PlaybookQualificationExport,
  PlaybookQualificationExportPayload,
} from "./api/client";


export type PlaybookQualificationTarget = (
  PlaybookQualificationExportPayload["target_venue_account_type"]
);

export function playbookQualificationFilename(
  playbookRef: string | null | undefined,
  target: PlaybookQualificationTarget,
  digest: string,
): string {
  const safePlaybook = (playbookRef?.trim() || "playbook")
    .replace(/[^A-Za-z0-9._-]+/gu, "_")
    .slice(0, 64);
  const safeDigest = /^[0-9a-f]{64}$/u.test(digest)
    ? digest.slice(0, 12)
    : "digest-unknown";
  return `halpha-qualification-${safePlaybook}-${target.toLowerCase()}-${safeDigest}.json`;
}

export function serializePlaybookQualification(
  result: PlaybookQualificationExport,
): string {
  return `${JSON.stringify(result.artifact, null, 2)}\n`;
}
