import { DARK_THEME, LIGHT_THEME, ThemeContext, ThemeFactory } from '@skbkontur/react-ui';
import { createContext, useContext, useEffect, useMemo, useState } from 'react';

import {
  readPreference,
  resolveTheme,
  storePreference,
  SYSTEM_DARK_QUERY,
  type ResolvedTheme,
  type ThemePreference,
} from './preference';
import { ThemeBridge } from './ThemeBridge';

interface ThemeChoice {
  preference: ThemePreference;
  resolved: ResolvedTheme;
  setPreference: (preference: ThemePreference) => void;
}

const ThemeChoiceContext = createContext<ThemeChoice | null>(null);

// Единственные переопределения токенов (концепция §2, §7.1): свой шрифт и отмена сдвига базовой линии.
const FONT_TOKENS = {
  baseFontFamily: '"Golos Text", Arial, sans-serif',
  labGrotesqueBaselineCompensation: '0',
};

const KONTUR_THEMES = {
  light: ThemeFactory.create(FONT_TOKENS, LIGHT_THEME),
  dark: ThemeFactory.create(FONT_TOKENS, DARK_THEME),
};

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  const [preference, setPreferenceState] = useState(readPreference);
  const [systemDark, setSystemDark] = useState(() => window.matchMedia(SYSTEM_DARK_QUERY).matches);

  useEffect(() => {
    const query = window.matchMedia(SYSTEM_DARK_QUERY);
    const onChange = (event: MediaQueryListEvent) => setSystemDark(event.matches);
    query.addEventListener('change', onChange);
    return () => query.removeEventListener('change', onChange);
  }, []);

  const resolved = resolveTheme(preference, systemDark);

  const choice = useMemo<ThemeChoice>(
    () => ({
      preference,
      resolved,
      setPreference: (next) => {
        storePreference(next);
        setPreferenceState(next);
      },
    }),
    [preference, resolved],
  );

  return (
    <ThemeChoiceContext.Provider value={choice}>
      <ThemeContext.Provider value={KONTUR_THEMES[resolved]}>
        <ThemeBridge resolved={resolved} />
        {children}
      </ThemeContext.Provider>
    </ThemeChoiceContext.Provider>
  );
}

export function useThemeChoice(): ThemeChoice {
  const choice = useContext(ThemeChoiceContext);
  if (!choice) {
    throw new Error('useThemeChoice вызван вне ThemeProvider');
  }
  return choice;
}
