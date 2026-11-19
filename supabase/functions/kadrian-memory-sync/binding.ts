// Reviewed public deployment binding. No environment-configured permissions.
// An empty binding fails closed; operator must build a separately reviewed binding.
import type { Registry, Scope } from "./protocol.ts";
export const binding:
  | { url: string; release: string; pilot: Scope; devices: Registry }
  | null = null;
