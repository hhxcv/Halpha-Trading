import { describe, expect, it } from "vitest";

import {
  clearPendingScalpRequest,
  readPendingScalpRequest,
  savePendingScalpRequest,
  scalpExitOutcome,
  type ScalpExitRequest,
  type ScalpTriggerRequest,
} from "./scalpRequests";

class MemoryStorage {
  private readonly values = new Map<string, string>();
  getItem(key: string): string | null { return this.values.get(key) ?? null; }
  setItem(key: string, value: string): void { this.values.set(key, value); }
  removeItem(key: string): void { this.values.delete(key); }
}

const trigger: ScalpTriggerRequest = {
  kind: "TRIGGER", idempotencyKey: "trigger-original", instrumentRef: "BTCUSDT-PERP", direction: "LONG",
};
const exit: ScalpExitRequest = {
  kind: "EXIT", idempotencyKey: "exit-original", activationId: "activation-1", expectedVersion: 3,
};

describe("scalp request responsibility", () => {
  it("retains the original request across reads and rejects a replacement direction or identity", () => {
    const storage = new MemoryStorage();
    expect(savePendingScalpRequest(trigger, "demo", "account", storage)).toBe(true);
    expect(readPendingScalpRequest("TRIGGER", "demo", "account", storage)).toEqual(trigger);
    expect(savePendingScalpRequest({ ...trigger, direction: "SHORT" }, "demo", "account", storage)).toBe(false);
    expect(savePendingScalpRequest({ ...trigger, idempotencyKey: "new-request" }, "demo", "account", storage)).toBe(false);
    expect(readPendingScalpRequest("TRIGGER", "demo", "account", storage)).toEqual(trigger);
  });

  it("isolates recovery by request kind, environment and account", () => {
    const storage = new MemoryStorage();
    savePendingScalpRequest(trigger, "demo", "account", storage);
    savePendingScalpRequest(exit, "demo", "account", storage);
    expect(readPendingScalpRequest("EXIT", "demo", "account", storage)).toEqual(exit);
    expect(readPendingScalpRequest("TRIGGER", "live", "account", storage)).toBeNull();
    expect(readPendingScalpRequest("TRIGGER", "demo", "other", storage)).toBeNull();
    clearPendingScalpRequest({ ...trigger, idempotencyKey: "different" }, "demo", "account", storage);
    expect(readPendingScalpRequest("TRIGGER", "demo", "account", storage)).toEqual(trigger);
    clearPendingScalpRequest(trigger, "demo", "account", storage);
    expect(readPendingScalpRequest("TRIGGER", "demo", "account", storage)).toBeNull();
    expect(readPendingScalpRequest("EXIT", "demo", "account", storage)).toEqual(exit);
  });

  it("requires durable identity storage before submission", () => {
    const failedStorage = { ...new MemoryStorage(), getItem: () => null, setItem: () => { throw new Error("blocked"); }, removeItem: () => undefined };
    expect(savePendingScalpRequest(trigger, "demo", "account", null)).toBe(false);
    expect(savePendingScalpRequest(trigger, "demo", "account", failedStorage)).toBe(false);
  });

  it.each([
    ["APPLIED", "APPLIED"], ["REJECTED", "REJECTED"], ["UNKNOWN", "UNKNOWN"],
    ["ACCEPTED", "UNKNOWN"], [undefined, "UNKNOWN"],
  ])("does not confuse %s with accepted exit responsibility", (state, expected) => {
    expect(scalpExitOutcome({ state })).toBe(expected);
  });
});
