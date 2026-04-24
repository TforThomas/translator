import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

export type UIMode = "default" | "art";

interface UIModeContextValue {
  mode: UIMode;
  isArtMode: boolean;
  setMode: (mode: UIMode) => void;
  toggleMode: () => void;
}

const UIModeContext = createContext<UIModeContextValue | null>(null);

function getInitialMode(): UIMode {
  return "default";
}

export function UIModeProvider({ children }: { children: ReactNode }) {
  const [mode, setMode] = useState<UIMode>(getInitialMode);

  useEffect(() => {
    document.documentElement.setAttribute("data-ui-mode", "default");
    localStorage.setItem("ui-mode", "default");
  }, [mode]);

  const value = useMemo<UIModeContextValue>(
    () => ({
      mode: "default",
      isArtMode: false,
      setMode: () => setMode("default"),
      toggleMode: () => setMode("default"),
    }),
    []
  );

  return <UIModeContext.Provider value={value}>{children}</UIModeContext.Provider>;
}

export function useUIMode() {
  const context = useContext(UIModeContext);
  if (!context) {
    throw new Error("useUIMode must be used within UIModeProvider");
  }
  return context;
}