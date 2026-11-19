"use client";

import { createContext, type ReactNode } from "react";

export const LocalJourneyPreviewContext = createContext(false);

export function LocalJourneyPreview({ children }: { children: ReactNode }) {
  return <LocalJourneyPreviewContext.Provider value={true}>{children}</LocalJourneyPreviewContext.Provider>;
}
