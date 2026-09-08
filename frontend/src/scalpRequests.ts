export type ScalpTriggerRequest = Readonly<{
  kind: "TRIGGER";
  idempotencyKey: string;
  instrumentRef: string;
  direction: "LONG" | "SHORT";
}>;

export type ScalpExitRequest = Readonly<{
  kind: "EXIT";
  idempotencyKey: string;
  activationId: string;
  expectedVersion: number;
}>;

type ScalpPendingRequest = ScalpTriggerRequest | ScalpExitRequest;
type RequestStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">;
const STORAGE_PREFIX = "halpha.scalp-pending.v1";

function browserStorage(): RequestStorage | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}

function storageKey(kind: ScalpPendingRequest["kind"], environmentId: string, accountId: string): string {
  return `${STORAGE_PREFIX}:${kind}:${encodeURIComponent(environmentId)}:${encodeURIComponent(accountId)}`;
}

function validRequest(value: unknown): value is ScalpPendingRequest {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
  const request = value as Record<string, unknown>;
  if (typeof request.idempotencyKey !== "string"
    || !request.idempotencyKey
    || request.idempotencyKey.length > 160
    || /\s/u.test(request.idempotencyKey)) return false;
  return request.kind === "TRIGGER"
    ? typeof request.instrumentRef === "string" && request.instrumentRef.length > 0
      && (request.direction === "LONG" || request.direction === "SHORT")
    : request.kind === "EXIT"
      && typeof request.activationId === "string" && request.activationId.length > 0
      && Number.isSafeInteger(request.expectedVersion) && Number(request.expectedVersion) > 0;
}

export function readPendingScalpRequest<K extends ScalpPendingRequest["kind"]>(
  kind: K,
  environmentId: string,
  accountId: string,
  storage: RequestStorage | null = browserStorage(),
): Extract<ScalpPendingRequest, { kind: K }> | null {
  try {
    const raw = storage?.getItem(storageKey(kind, environmentId, accountId));
    const request: unknown = raw ? JSON.parse(raw) : null;
    return validRequest(request) && request.kind === kind
      ? request as Extract<ScalpPendingRequest, { kind: K }>
      : null;
  } catch {
    return null;
  }
}

/** Store responsibility before submission; a different unresolved request cannot replace it. */
export function savePendingScalpRequest(
  request: ScalpPendingRequest,
  environmentId: string,
  accountId: string,
  storage: RequestStorage | null = browserStorage(),
): boolean {
  if (storage === null || !validRequest(request)) return false;
  const existing = readPendingScalpRequest(request.kind, environmentId, accountId, storage);
  if (existing !== null && JSON.stringify(existing) !== JSON.stringify(request)) return false;
  try {
    storage.setItem(storageKey(request.kind, environmentId, accountId), JSON.stringify(request));
    return true;
  } catch {
    return false;
  }
}

export function clearPendingScalpRequest(
  request: ScalpPendingRequest,
  environmentId: string,
  accountId: string,
  storage: RequestStorage | null = browserStorage(),
): void {
  if (readPendingScalpRequest(request.kind, environmentId, accountId, storage)?.idempotencyKey
    !== request.idempotencyKey) return;
  try {
    storage?.removeItem(storageKey(request.kind, environmentId, accountId));
  } catch {
    // A failed cleanup remains recoverable through the same read-only lookup.
  }
}

export function scalpExitOutcome(receipt: Record<string, unknown>): "APPLIED" | "REJECTED" | "UNKNOWN" {
  return receipt.state === "APPLIED" || receipt.state === "REJECTED" ? receipt.state : "UNKNOWN";
}
