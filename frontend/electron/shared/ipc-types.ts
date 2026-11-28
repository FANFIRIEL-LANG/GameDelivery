export type CapabilityState = "available" | "not_implemented" | "unavailable";
export type DesktopCapability = { id: string; state: CapabilityState; reason?: string };
