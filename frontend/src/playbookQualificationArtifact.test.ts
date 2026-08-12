import { describe, expect, it } from "vitest";

import {
  playbookQualificationFilename,
  serializePlaybookQualification,
} from "./playbookQualificationArtifact";


describe("playbook qualification artifact", () => {
  it("creates a Windows-safe target-specific filename", () => {
    expect(playbookQualificationFilename(
      "BTC:BREAKOUT/V1",
      "USDM_COPY_LEAD",
      "a".repeat(64),
    )).toBe(
      "halpha-qualification-BTC_BREAKOUT_V1-usdm_copy_lead-aaaaaaaaaaaa.json",
    );
  });

  it("serializes only the artifact with a stable trailing newline", () => {
    const serialized = serializePlaybookQualification({
      artifact: {
        schema: "HALPHA_PLAYBOOK_QUALIFICATION@1",
        source_environment_kind: "DEMO",
      },
      artifact_content_digest: "b".repeat(64),
      save_outside_repository: true,
    } as never);

    expect(serialized).toBe(
      '{\n  "schema": "HALPHA_PLAYBOOK_QUALIFICATION@1",\n  "source_environment_kind": "DEMO"\n}\n',
    );
    expect(serialized).not.toContain("artifact_content_digest");
  });
});
